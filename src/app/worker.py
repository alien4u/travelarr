"""Background downloader: one file at a time, resumable, with subtitles.

Resume rules, so a dropped connection or a crash never corrupts a file:
* progress (bytes_done) is only recorded after the partial file is fsynced;
  anything past it is discarded on the next start;
* a resume is only made against the same strong ETag the earlier bytes came
  from, with the Content-Range checked, and the last 64 KB before the resume
  point are downloaded again and compared with the disk;
* a file that changed on Plex restarts from zero (same size) or fails clearly.
"""
from __future__ import annotations

import collections
import errno
import hashlib
import itertools
import json
import logging
import os
import posixpath
import re
import shutil
import stat
import threading
import time

import requests
import urllib3

from .paths import PART_SUFFIX, SUB_EXT, PathError, abs_path, real_rel, remove_files, sidecar_rel
from .plexgw import NotReady, OtherServer
from .store import now, protected, staging_rel

log = logging.getLogger("travelarr.worker")

CHUNK = 1 << 20  # bytes per read
MAX_ATTEMPTS = 12  # consecutive failures without new progress: ~7 minutes of backoff
OVERLAP = 64 << 10  # re-read before every resume and compared with the disk
CHECKPOINT_SECS = 10
CHECKPOINT_BYTES = 256 << 20
RATE_WINDOW = 5.0  # seconds of history behind the displayed speed
MAX_RESTARTS = 2
CHANGED = "The file changed on Plex since it was queued"
ADOPTED = "already on {}; adopted"
WAITING = "waiting for its Plex server to be selected again"
UNCONFIRMED = "could not confirm the file was placed; run Re-sync again"
CLEANUP_EVERY = 1800  # seconds between retries of a row's leftover cleanup
NOTES = {"upgrade": "re-synced: new version", "redownload": "re-synced: was missing",
         "refresh": "re-synced: subtitles"}
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")
_dir_sync_warned = False


class Cancelled(Exception):
    pass


class JobError(Exception):
    pass


class DiskError(JobError):
    """Data could not be confirmed on disk; never retried."""


class Changed(JobError):
    """The file on Plex is not the one that was queued."""


class Retry(Exception):
    """A malformed response worth another attempt."""


class Restart(Exception):
    """The partial cannot be trusted; start again from byte 0."""


class PlaceFailed(JobError):
    """The final rename raised: nothing moved."""


class Superseded(Exception):
    """An upgrade whose old copy was removed before it could replace it."""


RETRYABLE = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
             NotReady, Retry)


def looks_like_subtitle(data: bytes, ext: str) -> bool:
    head = data[:65536].lstrip(b"\xef\xbb\xbf \r\n\t")
    if not head or head[:5].lower() in (b"<?xml", b"<medi", b"<html", b"<!doc"):
        return False
    if ext in ("srt", "vtt"):
        return b"-->" in head
    if ext in ("ass", "ssa"):
        return b"[script info]" in head.lower() or b"[events]" in head.lower()
    return True


def _why(e: Exception) -> str:
    """A short reason for the Queue; the log keeps the full exception."""
    if isinstance(e, requests.exceptions.ChunkedEncodingError):
        return "the connection dropped mid-file"
    # a read timeout inside the body arrives as a ConnectionError wrapping urllib3's
    if isinstance(e, requests.ReadTimeout) or isinstance(e.args[0] if e.args else None,
                                                         urllib3.exceptions.ReadTimeoutError):
        return "Plex stopped sending data"
    if isinstance(e, requests.ConnectionError) and e.request is not None:
        return "cannot reach Plex"
    return str(e)


def _validator(r) -> str | None:
    """Strong ETag plus Last-Modified; None when the response cannot back a resume."""
    etag = (r.headers.get("ETag") or "").strip()
    if not etag or etag.startswith("W/"):
        return None
    return f"{etag}|{(r.headers.get('Last-Modified') or '').strip()}"


def _int_header(r, name: str) -> int | None:
    v = (r.headers.get(name) or "").strip()
    return int(v) if v.isdigit() else None


def _regular_size(path: str) -> int | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return st.st_size if stat.S_ISREG(st.st_mode) else None


def _open_partial(path: str) -> int:
    """fd of the partial file, created if missing. A link or anything but a plain
    single-link file is removed first (the entry, never what it points to)."""
    for _ in range(2):
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o666)
        except OSError as e:
            if e.errno != errno.ELOOP:
                raise
            log.warning("removing a link in place of %s", path)
            os.unlink(path)
            continue
        st = os.fstat(fd)
        if stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
            return fd
        os.close(fd)
        log.warning("removing an unexpected file in place of %s", path)
        os.unlink(path)
    raise JobError(f"could not create {os.path.basename(path)}")


def _write_all(fd: int, data: bytes, offset: int) -> None:
    view = memoryview(data)
    while view:
        n = os.pwrite(fd, view, offset)
        if n <= 0:
            raise DiskError("could not write to the partial file")
        view, offset = view[n:], offset + n


def _read_all(fd: int, n: int, offset: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        b = os.pread(fd, n - len(out), offset + len(out))
        if not b:
            break
        out += b
    return bytes(out)


def _sync_dir(path: str) -> None:
    """Best effort, as SQLite does: some filesystems (FUSE) cannot sync a folder."""
    global _dir_sync_warned
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as e:
        if not _dir_sync_warned:
            _dir_sync_warned = True
            log.warning("cannot sync folders on this share (%s); renames rely on the filesystem", e)


class Downloader:
    def __init__(self, store, gw, media_root: str, device: str = "NAS"):
        self.store, self.gw, self.media_root, self.device = store, gw, media_root, device
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._cancel: set[int] = set()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()  # current, _received, _samples, transitions
        self.current: dict | None = None
        self._received = 0
        self._samples: collections.deque = collections.deque()
        self.transitions: collections.deque = collections.deque(maxlen=64)
        self.last_error: str | None = None
        self.chunk = CHUNK
        self.max_attempts = MAX_ATTEMPTS
        self.backoff = 2.0  # seconds, doubled per attempt, capped at 60
        self._later: dict[int, tuple[float, float]] = {}  # job id -> (not before, last delay)
        self._cleaned: dict[int, float] = {}  # row id -> last cleanup attempt

    # ------------------------------------------------------------ control
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="downloader", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def cancel(self, job_id: int) -> None:
        self._cancel.add(job_id)

    def reserve_bytes(self) -> int:
        return int(float(self.store.get("reserve_gb", 10)) * (1 << 30))

    # ------------------------------------------------------- live status
    def _begin(self, job: dict) -> None:
        with self._lock:
            self._received = 0
            self._samples = collections.deque([(time.monotonic(), 0)])
            self.current = {"id": job["id"], "title": self._label(job), "size": int(job["size"] or 0),
                            "bytes": int(job["bytes_done"] or 0), "state": "connecting", "attempt": 0,
                            "max_attempts": self.max_attempts, "retry_at": None, "error": None}
            self.transitions.append("connecting")

    def _set(self, **fields) -> None:
        with self._lock:
            if self.current is None:
                return
            state = fields.get("state")
            if state and state != self.current["state"]:
                self.transitions.append(state)
            self.current.update(fields)

    def _note(self, n: int) -> None:
        """n bytes arrived from the network."""
        with self._lock:
            t = time.monotonic()
            self._received += n
            self._samples.append((t, self._received))
            self._trim(t)
            if self.current is not None and self.current["state"] == "connecting":
                self.current["state"] = "downloading"
                self.transitions.append("downloading")

    def _trim(self, now: float) -> None:
        # keep the newest sample at or before the window start as the baseline
        s = self._samples
        while len(s) > 1 and s[1][0] <= now - RATE_WINDOW:
            s.popleft()

    def _rate(self, now: float) -> float:
        """Bytes per second over the last RATE_WINDOW seconds (or since the job
        started). The window edge is interpolated, so a stall makes it fall
        steadily to zero."""
        self._trim(now)
        s = self._samples
        if not s:
            return 0.0
        edge = now - RATE_WINDOW
        t0, b0 = s[0]
        if edge <= t0:
            return (self._received - b0) / (now - t0) if now > t0 else 0.0
        if len(s) > 1 and s[1][0] > t0:
            t1, b1 = s[1]
            b0 += (b1 - b0) * (edge - t0) / (t1 - t0)
        return (self._received - b0) / RATE_WINDOW

    def snapshot(self) -> dict | None:
        with self._lock:
            if self.current is None:
                return None
            d = dict(self.current)
            t = time.monotonic()
            d["speed"] = self._rate(t)
            retry_at = d.pop("retry_at")
        d["eta"] = (d["size"] - d["bytes"]) / d["speed"] if d["speed"] > 0 and d["size"] else None
        d["retry_in"] = max(0, round(retry_at - t)) if d["state"] == "retrying" and retry_at else None
        return d

    # --------------------------------------------------------------- loop
    def _loop(self) -> None:
        try:
            self._reconcile_done()
            self.cleanup_pass(force=True)
        except Exception:  # noqa: BLE001
            log.exception("startup check of copied files failed")
        while not self._stop.is_set():
            try:
                if self.store.get("paused", False):
                    self._sleep(2)
                    continue
                t = time.monotonic()
                skip = frozenset(j for j, (at, _) in self._later.items() if at > t)
                job = self.store.next_queued(self.store.get("server_id") or "", skip)
                if job is None:
                    self.cleanup_pass()
                    self._sleep(5)
                    continue
                try:
                    self.gw.server()
                except NotReady as e:
                    self.last_error = str(e)
                    self._sleep(15)
                    continue
                self.last_error = None
                self._process(job)
            except Exception as e:  # noqa: BLE001 - never let the thread die
                log.exception("downloader loop error")
                self.last_error = str(e)
                self._sleep(5)

    def _sleep(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def _reconcile_done(self) -> None:
        """A copied file whose final rename was lost (power cut) but whose partial
        survived goes back in the queue, to be verified and renamed. Whatever sits at
        the final path then (nothing, or an older file) is replaced."""
        t, n = time.monotonic(), 0
        for j in self.store.rows("SELECT id, dest_rel FROM jobs WHERE status='done'"):
            try:
                final = abs_path(self.media_root, j["dest_rel"])
            except PathError:
                continue
            if os.path.lexists(final + PART_SUFFIX):
                self.store.update(j["id"], status="queued", note="copied file was missing; checking it again")
                n += 1
        log.info("checked copied files in %.1fs; %d queued again", time.monotonic() - t, n)

    # ------------------------------------------------------------ one job
    def _process(self, job: dict) -> None:
        """Cancel is best-effort until the file is being placed: the flag is read
        right after the claim, while the file streams or waits to retry, and just
        before placement. From then on the job finishes."""
        jid = job["id"]
        if not self.store.claim(jid, self.store.get("server_id")):
            return  # cancelled after it was picked
        job = self.store.job(jid)
        self._begin(job)
        try:
            self._run(job)
            self._later.pop(jid, None)
        except OtherServer:
            # before anything was placed: wait, keeping the partial (never a failure)
            self.store.update(jid, status="queued", note=WAITING)
            log.info("waiting for its server: %s", self._label(job))
        except Superseded:
            log.info("cancelled, the copy it would replace was removed: %s", self._label(job))
            self.cleanup(jid)
        except (_Handled, PlaceFailed):
            pass  # outcome already recorded
        except Cancelled:
            self.store.transition(jid, lambda r: True, **self.store._cancel_fields(job))
            if job["mode"] != "refresh":
                self._reset(job)
            self.cleanup(jid)
            log.info("cancelled: %s", self._label(job))
        except Changed as e:
            self._rm(self._temp(job))
            self._reset(job)
            self.store.update(jid, status="failed", finished_at=now(), error=f"{CHANGED}: {e}"[:500])
            log.warning("changed on Plex: %s: %s", self._label(job), e)
        except Exception as e:  # noqa: BLE001
            cur = self.store.job(jid)
            if cur and cur["phase"]:
                # never failed while placing or placed: try again later, then recover
                at, delay = self._later.get(jid, (0.0, 15.0))
                delay = min(900.0, delay * 2)
                self._later[jid] = (time.monotonic() + delay, delay)
                try:
                    self.store.update(jid, status="queued", note=f"finishing, retrying after: {e}"[:300])
                except Exception:  # noqa: BLE001
                    log.exception("could not record the retry of %s", self._label(job))
                log.warning("finishing %s failed, retrying in %.0fs: %s", self._label(job), delay, e)
            else:
                self.store.update(jid, status="failed", finished_at=now(), error=str(e)[:500])
                log.warning("failed: %s: %s", self._label(job), e)
        finally:
            self._cancel.discard(jid)
            with self._lock:
                self.current = None

    def _temp(self, job: dict) -> str:
        """The partial (a copy) or the staging file (an upgrade)."""
        if job["mode"] == "upgrade":
            return abs_path(self.media_root, staging_rel(job))
        return abs_path(self.media_root, job["dest_rel"]) + PART_SUFFIX

    def _run(self, job: dict) -> None:
        jid, mode, phase = job["id"], job["mode"], job["phase"]
        final = abs_path(self.media_root, job["dest_rel"])
        temp = self._temp(job)
        size = int(job["size"] or 0)
        if jid in self._cancel and not phase:
            raise Cancelled()
        os.makedirs(os.path.dirname(final), exist_ok=True)
        note = NOTES.get(mode)
        if mode == "refresh" or phase == "placed":
            if _regular_size(final) != size:
                raise JobError("the copied file changed on the NAS; run Re-sync again")
            self._set(bytes=size)
        elif phase == "placing":
            self._recover(job, final, temp)
        elif mode is None and size and not os.path.lexists(temp) and _regular_size(final) == size:
            note = ADOPTED.format(self.device)
            self._set(bytes=size)
            self._place(job, None, final)
        else:
            mark = self._download(job, temp)
            self._place(job, temp, final, mark)
        self._set(state="finishing")
        self._finish(job, self._subtitles(job), note)

    def _place(self, job: dict, src: str | None, final: str, mark: str | None = None) -> None:
        """Serialized with server selection (gateway lock, then store lock): the
        server check, `placing`, the rename and `placed` happen as one step."""
        jid = job["id"]
        with self.gw._lock:
            self.gw.server_for(job["server_id"])
            if job["phase"] != "placing":
                if jid in self._cancel:
                    raise Cancelled()
                if job["mode"] == "upgrade":
                    old = json.loads(job["replaces"] or "[]")
                    ok = self.store.begin_upgrade(jid, old, mark)
                    if not ok:
                        raise Superseded()
                else:
                    self.store.update(jid, phase="placing")
                job["phase"] = "placing"
            if src is not None:
                try:
                    os.replace(src, final)
                except OSError as e:
                    self.store.update(jid, status="failed", phase=None, finished_at=now(),
                                      error=f"could not move the file into place: {e}"[:500])
                    job["phase"] = None
                    raise PlaceFailed(str(e)) from None
                _sync_dir(os.path.dirname(final))
            self.store.update(jid, phase="placed")
            job["phase"] = "placed"

    def _recover(self, job: dict, final: str, temp: str) -> None:
        """A run interrupted while placing: finish the rename, or prove it happened."""
        if os.path.lexists(temp):
            self._place(job, temp, final)
            return
        if job["mode"] == "upgrade":
            self._set(state="verifying")
            placed = os.path.isfile(final) and _sha256(final, self._check_stop) == job["staging_mark"]
        else:
            placed = _regular_size(final) == int(job["size"] or 0)
        if placed:
            self._place(job, None, final)
            return
        self.store.update(job["id"], status="failed", phase=None, finished_at=now(), error=UNCONFIRMED)
        for old in json.loads(job["replaces"] or "[]"):
            self.store.update(int(old), note="unverified after an interrupted re-sync")
        raise _Handled()

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise JobError("shutting down")

    def _finish(self, job: dict, subs: dict, note: str | None) -> None:
        jid = job["id"]
        fields = {"status": "done", "phase": None, "finished_at": now(), "bytes_done": int(job["size"]),
                  "sidecars": json.dumps(subs["sidecars"]), "subs_total": subs["total"],
                  "subs_done": len(subs["written"]), "note": subs["note"] or note, "error": subs["error"]}
        if subs["guid"] and not job["guid"]:
            fields["guid"] = subs["guid"]
        if job["mode"] in ("refresh", "redownload"):
            fields["mode"] = None
        if job["mode"] == "upgrade":
            old = self.store.finish_upgrade(jid, json.loads(job["replaces"] or "[]"), fields)
            for o in old:
                self.cleanup(o)
        else:
            self.store.transition(jid, lambda r: True, **fields)
        self.cleanup(jid)
        log.info("done: %s", self._label(job))

    @staticmethod
    def _label(job: dict) -> str:
        if job["kind"] == "episode":
            return f"{job['show_title']} S{(job['season'] or 0):02d}E{(job['episode'] or 0):02d} {job['title']}"
        return f"{job['title']} ({job['year']})" if job.get("year") else job["title"]

    @staticmethod
    def _rm(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    def _reset(self, job: dict, fd: int | None = None) -> None:
        """Forget the partial: recorded progress first, then the bytes."""
        self.store.update(job["id"], bytes_done=0, validator=None)
        job["bytes_done"], job["validator"] = 0, None
        if fd is not None:
            os.ftruncate(fd, 0)
        self._set(bytes=0)

    def _check_space(self, size: int, have: int) -> None:
        if size and (size - have) > shutil.disk_usage(self.media_root).free - self.reserve_bytes():
            raise JobError(f"not enough free space on {self.device}")

    def _fsync(self, fd: int) -> None:
        try:
            os.fsync(fd)
        except OSError as e:
            raise DiskError(f"could not confirm data on disk: {e}") from None

    def _checkpoint(self, job: dict, fd: int, pos: int) -> None:
        self._fsync(fd)
        try:
            self.store.update(job["id"], bytes_done=pos)
        except Exception as e:  # noqa: BLE001
            raise DiskError(f"could not record progress: {e}") from None
        job["bytes_done"] = pos

    def _download(self, job: dict, partial: str) -> str | None:
        """Download into partial (a copy's partial or an upgrade's staging file).
        For an upgrade, returns the sha256 of the whole file, kept up to date while
        writing; a resume first rebuilds it from the bytes already there."""
        fd = _open_partial(partial)
        hashing = {"h": hashlib.sha256(), "n": 0} if job["mode"] == "upgrade" else None
        try:
            have = self._start_offset(job, fd)
            high, attempts, restarts = have, 0, 0
            while True:
                try:
                    self._attempt(job, fd, have, hashing)
                    break
                except Restart as e:
                    restarts += 1
                    if restarts > MAX_RESTARTS:
                        raise JobError(f"the file content differs between requests: {e}") from None
                    log.warning("starting %s again from the beginning: %s", self._label(job), e)
                    self._reset(job, fd)
                    have = 0
                except RETRYABLE as e:
                    have = int(job["bytes_done"] or 0)
                    if have > high:  # only new ground resets the failure budget
                        high, attempts = have, 0
                    attempts += 1
                    why = _why(e)
                    self.store.update(job["id"], note=f"retrying after: {why}"[:300])
                    if attempts >= self.max_attempts:
                        raise JobError(f"gave up after {attempts} attempts: {why}") from None
                    self.gw.invalidate()
                    wait = min(60.0, self.backoff * 2 ** (attempts - 1))
                    log.info("retry %d for %s in %.0fs: %s", attempts, self._label(job), wait, e)
                    self._set(state="retrying", retry_at=time.monotonic() + wait, attempt=attempts,
                              error=why[:300])
                    if self._stop.wait(wait):
                        raise JobError("shutting down") from None
                    if job["id"] in self._cancel:
                        raise Cancelled() from None
            size = int(job["size"])
            if os.fstat(fd).st_size != size:
                raise JobError(f"size mismatch: got {os.fstat(fd).st_size}, expected {size}")
            self._fsync(fd)
            if job["id"] in self._cancel:
                raise Cancelled()
            if hashing is None:
                return None
            self._hash_upto(fd, hashing, size, job["id"])
            return hashing["h"].hexdigest()
        finally:
            os.close(fd)

    def _hash_upto(self, fd: int, hashing: dict, pos: int, jid: int) -> None:
        """Make the running hash cover exactly bytes [0, pos) of the file."""
        if hashing["n"] == pos:
            return
        if hashing["n"] > pos:
            hashing["h"], hashing["n"] = hashlib.sha256(), 0
        self._set(state="verifying")
        while hashing["n"] < pos:
            if jid in self._cancel:
                raise Cancelled()
            b = os.pread(fd, min(CHUNK, pos - hashing["n"]), hashing["n"])
            if not b:
                raise JobError("the staging file is shorter than expected")
            hashing["h"].update(b)
            hashing["n"] += len(b)

    def _start_offset(self, job: dict, fd: int) -> int:
        """Where a resume may start: only fsynced bytes from a validated response."""
        have = os.fstat(fd).st_size
        size, durable = int(job["size"] or 0), int(job["bytes_done"] or 0)
        if not have:
            if durable or job["validator"]:
                self._reset(job)
            return 0
        if (size and have > size) or have < durable:
            log.info("not resuming %s: the partial file cannot be trusted", self._label(job))
            self._reset(job, fd)
            return 0
        if have > durable:
            log.info("discarding %d unconfirmed bytes of %s", have - durable, self._label(job))
            os.ftruncate(fd, durable)
            have = durable
        self._set(bytes=have)
        return have

    def _attempt(self, job: dict, fd: int, have: int, hashing: dict | None = None) -> None:
        """One request; returns only when the file is complete and the response
        ended cleanly at exactly its size."""
        jid = job["id"]
        self._set(state="connecting", retry_at=None)
        if have and not job["validator"]:
            self._reset(job, fd)  # bytes from an unvalidated response are never resumed
            have = 0
        size = int(job["size"] or 0)
        self._check_space(size, have)
        start = max(0, have - OVERLAP)
        url, headers = self.gw.file_request(job["part_key"], self.gw.server_for(job["server_id"]))
        headers["Accept-Encoding"] = "identity"
        if have:
            headers["Range"] = f"bytes={start}-"
        with self.gw.session.get(url, headers=headers, stream=True, timeout=(15, 60)) as r:
            size, body_start, have = self._accept(job, fd, r, have, start, size)
            chunks = self._chunks(r, jid)
            first = []
            if body_start < have:
                rest = self._overlap(fd, chunks, body_start, have)
                first = [rest] if rest else []
            pos, failed = have, False
            synced, synced_t = have, time.monotonic()
            if hashing is not None:
                self._hash_upto(fd, hashing, have, jid)
                self._set(state="connecting")
            try:
                for chunk in itertools.chain(first, chunks):
                    room = size - pos
                    take = chunk if len(chunk) <= room else chunk[:room]
                    _write_all(fd, take, pos)
                    if hashing is not None:
                        hashing["h"].update(take)
                        hashing["n"] += len(take)
                    pos += len(take)
                    self._set(bytes=pos)
                    if len(chunk) > room:
                        raise Retry(f"Plex sent more data than the {size}-byte file")
                    if pos - synced >= CHECKPOINT_BYTES or time.monotonic() - synced_t >= CHECKPOINT_SECS:
                        self._checkpoint(job, fd, pos)
                        synced, synced_t = pos, time.monotonic()
            except (Cancelled, DiskError):
                failed = True  # nothing more is recorded after these
                raise
            finally:
                if not failed:
                    self._checkpoint(job, fd, pos)
        if pos != size:
            raise requests.ConnectionError(f"stream ended early at {pos} of {size} bytes")

    def _accept(self, job: dict, fd: int, r, have: int, start: int, size: int) -> tuple[int, int, int]:
        """Check status and framing before any byte is written. Returns (size, the
        offset the body starts at, have)."""
        code = r.status_code
        if code == 416:
            raise Changed("Plex no longer has the requested range (HTTP 416)")
        if code >= 500:
            raise Retry(f"Plex answered HTTP {code}")
        if code not in (200, 206):
            raise JobError(f"Plex answered HTTP {code}")
        encoding = (r.headers.get("Content-Encoding") or "").strip().lower()
        if encoding not in ("", "identity"):
            raise JobError(f"Plex sent the file {encoding}-encoded")
        ctype = r.headers.get("Content-Type", "")
        if "xml" in ctype.lower() or "html" in ctype.lower():
            raise JobError(f"Plex returned {ctype} instead of the file")
        length = _int_header(r, "Content-Length")
        validator = _validator(r)
        if code == 200:
            if length is None:
                if size:
                    raise Changed("Plex no longer reports the file size")
                raise JobError("Plex did not report the file size")
            if size and length != size:
                raise Changed(f"Plex now reports {length} bytes; {size} were queued")
            size = self._learn_size(job, size, length)
            if have:  # Range ignored: this response starts at byte 0
                self._reset(job, fd)
                have = 0
            self._remember(job, validator)
            return size, 0, have
        m = _CONTENT_RANGE.fullmatch((r.headers.get("Content-Range") or "").strip())
        if not m:
            raise Retry("Plex sent part of the file without a valid Content-Range")
        a, b, total = map(int, m.groups())
        if size and total != size:
            raise Changed(f"Plex now reports {total} bytes; {size} were queued")
        if a != start or b != total - 1 or (length is not None and length != b - a + 1):
            raise Retry(f"Plex sent bytes {a}-{b}/{total} for a request from byte {start}")
        size = self._learn_size(job, size, total)
        if have:
            if validator is None or validator != job["validator"]:
                raise Restart("the file on Plex has a different version tag")
        else:
            self._remember(job, validator)
        return size, a, have

    def _learn_size(self, job: dict, size: int, reported: int) -> int:
        if size:
            return size
        self.store.update(job["id"], size=reported)
        job["size"] = reported
        self._set(size=reported)
        self._check_space(reported, 0)
        return reported

    def _remember(self, job: dict, validator: str | None) -> None:
        if validator != job["validator"]:
            self.store.update(job["id"], validator=validator)
            job["validator"] = validator

    def _chunks(self, r, jid: int):
        for chunk in r.iter_content(self.chunk):
            if jid in self._cancel:
                raise Cancelled()
            if chunk:
                self._note(len(chunk))
                yield chunk

    @staticmethod
    def _overlap(fd: int, chunks, start: int, have: int) -> bytes:
        """Compare the re-downloaded bytes before the resume point with the disk;
        returns whatever came after them."""
        need = have - start
        got = bytearray()
        for chunk in chunks:
            got += chunk
            if len(got) >= need:
                break
        if len(got) < need:
            raise requests.ConnectionError("stream ended during the resume check")
        if bytes(got[:need]) != _read_all(fd, need, start):
            raise Restart("the bytes before the resume point differ from Plex")
        return bytes(got[need:])

    def _subtitles(self, job: dict) -> dict:
        """The subtitle step, the same for every kind of job. `recorded` is every
        name the copy owns (for an upgrade, the old copy's too). Names are recorded
        before their file is written; old names are retired only when every
        expected subtitle was written; otherwise everything recorded is kept."""
        jid = job["id"]
        recorded = list(json.loads(self.store.job(jid)["sidecars"] or "[]"))
        for o in json.loads(job["replaces"] or "[]"):
            old = self.store.job(int(o))
            if old:
                recorded += [n for n in json.loads(old["sidecars"] or "[]") if n not in recorded]
        out = {"sidecars": recorded, "written": [], "total": 0, "guid": None, "note": None, "error": None}
        try:
            streams, out["guid"] = self.gw.subtitle_streams(job["rating_key"], job["part_id"],
                                                            self.gw.server_for(job["server_id"]))
        except OtherServer:
            out["note"] = "subtitles not refreshed: the Plex server changed"
            return out
        except Exception as e:  # noqa: BLE001
            out["error"] = f"subtitles not checked: {e}"[:300]
            return out
        out["total"] = len(streams)
        written, failures, skipped, used = [], [], [], set()
        for s in streams:
            ext = SUB_EXT.get(s["codec"])
            if not ext:
                skipped.append(f"{s['title'] or s['codec']}: image-based, skipped")
                continue
            rel = sidecar_rel(job["dest_rel"], s["lang"], s["forced"], s["hi"], ext, used)
            self.store.add_sidecar(jid, rel)
            try:
                data = self.gw.fetch_bytes(s["key"], srv=self.gw.server_for(job["server_id"]))
                if not looks_like_subtitle(data, ext):
                    raise ValueError("response is not a subtitle file")
                path = abs_path(self.media_root, rel)
                tmp = path + PART_SUFFIX
                fd = _open_partial(tmp)
                try:
                    os.ftruncate(fd, 0)
                    _write_all(fd, data, 0)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(tmp, path)
                written.append(rel)
            except OtherServer:
                failures.append(f"{s['title'] or rel}: the Plex server changed")
            except Exception as e:  # noqa: BLE001
                failures.append(f"{s['title'] or rel}: {e}")
        mine = list(json.loads(self.store.job(jid)["sidecars"] or "[]"))
        recorded += [n for n in mine if n not in recorded]
        if failures:
            # keep everything recorded: old names stay owned by the copy
            out["sidecars"] = recorded
            if any(n not in written for n in recorded):
                out["note"] = "old subtitles kept: some subtitles could not be fetched"
        else:
            out["sidecars"] = written  # the rest of `recorded` is retired by the finishing write
        out["written"] = written
        problems = failures + skipped
        out["error"] = ("subtitles: " + "; ".join(problems))[:500] if problems else None
        return out

    # ------------------------------------------------------------- cleanup
    def cleanup(self, row_id: int) -> list[str]:
        """Delete what a row gave up (its cleanup list). A path still owned by a
        copy on the NAS is left alone and cleared; one still needed by an unfinished
        job is kept for later; a failed delete is kept and noted. Returns what is left."""
        with self.store.lock:
            row = self.store.job(row_id)
            paths = json.loads((row or {}).get("cleanup") or "[]")
            if not paths:
                return []
            self._cleaned[row_id] = time.monotonic()
            names = {posixpath.basename(p) for p in paths}
            owners: dict[str, bool] = {}  # real path -> owned by a done row
            for r in self.store.rows("SELECT * FROM jobs WHERE id != ? AND status != 'removed'", (row_id,)):
                for p in protected(r):
                    if posixpath.basename(p) in names:
                        rp = self._real(p)
                        owners[rp] = owners.get(rp, False) or r["status"] == "done"
            left, errors = [], []
            for p in paths:
                rp = self._real(p)
                if rp in owners:
                    if not owners[rp]:
                        left.append(p)
                    continue
                try:
                    remove_files(self.media_root, [p])
                except (OSError, PathError) as e:
                    if getattr(e, "errno", None) != errno.ENOENT:
                        left.append(p)
                        errors.append(f"{p}: {getattr(e, 'strerror', None) or e}")
            fields = {"cleanup": json.dumps(left)}
            if errors:
                fields["note"] = ("could not delete old file: " + "; ".join(errors))[:300]
            self.store.update(row_id, **fields)
            return left

    def _real(self, rel: str) -> str:
        try:
            return real_rel(self.media_root, rel)
        except Exception:  # noqa: BLE001
            return rel

    def cleanup_pass(self, force: bool = False) -> None:
        t = time.monotonic()
        for r in self.store.rows("SELECT id FROM jobs WHERE cleanup != '[]'"):
            if force or t - self._cleaned.get(r["id"], -CLEANUP_EVERY) >= CLEANUP_EVERY:
                try:
                    self.cleanup(r["id"])
                except Exception:  # noqa: BLE001
                    log.exception("cleanup of row %s failed", r["id"])


class _Handled(Exception):
    """The job's outcome is already recorded."""


def _sha256(path: str, check) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            check()
            b = fh.read(CHUNK)
            if not b:
                return h.hexdigest()
            h.update(b)
