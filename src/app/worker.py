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
import itertools
import json
import logging
import os
import re
import shutil
import stat
import threading
import time

import requests
import urllib3

from .paths import PART_SUFFIX, SUB_EXT, PathError, abs_path, sidecar_rel
from .plexgw import NotReady
from .store import now

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
        except Exception:  # noqa: BLE001
            log.exception("startup check of copied files failed")
        while not self._stop.is_set():
            try:
                if self.store.get("paused", False):
                    self._sleep(2)
                    continue
                job = self.store.next_queued()
                if job is None:
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
        """Cancel is best-effort: the flag is read right after the claim, while
        the file streams or waits to retry, and just before the final rename.
        After the last of those the job finishes as done."""
        jid = job["id"]
        if not self.store.claim(jid):
            return  # cancelled after it was picked
        final = abs_path(self.media_root, job["dest_rel"])
        partial = final + PART_SUFFIX
        self._begin(job)
        try:
            if jid in self._cancel:
                raise Cancelled()
            os.makedirs(os.path.dirname(final), exist_ok=True)
            note = None
            size = int(job["size"] or 0)
            if size and not os.path.lexists(partial) and _regular_size(final) == size:
                note = ADOPTED.format(self.device)
                self._set(bytes=size)
            else:
                self._download(job, final, partial)
            self._set(state="finishing")
            subs = self._subtitles(job)
            self.store.update(jid, status="done", finished_at=now(), bytes_done=int(job["size"]),
                              sidecars=json.dumps(subs["written"]), subs_total=subs["total"],
                              subs_done=len(subs["written"]), note=note, error=subs["error"])
            log.info("done: %s", self._label(job))
        except Cancelled:
            self._rm(partial)
            self._reset(job)
            self.store.update(jid, status="cancelled", finished_at=now())
            log.info("cancelled: %s", self._label(job))
        except Changed as e:
            self._rm(partial)
            self._reset(job)
            self.store.update(jid, status="failed", finished_at=now(), error=f"{CHANGED}: {e}"[:500])
            log.warning("changed on Plex: %s: %s", self._label(job), e)
        except Exception as e:  # noqa: BLE001
            self.store.update(jid, status="failed", finished_at=now(), error=str(e)[:500])
            log.warning("failed: %s: %s", self._label(job), e)
        finally:
            self._cancel.discard(jid)
            with self._lock:
                self.current = None

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

    def _download(self, job: dict, final: str, partial: str) -> None:
        fd = _open_partial(partial)
        try:
            have = self._start_offset(job, fd)
            high, attempts, restarts = have, 0, 0
            while True:
                try:
                    self._attempt(job, fd, have)
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
            os.replace(partial, final)
            _sync_dir(os.path.dirname(final))
        finally:
            os.close(fd)

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

    def _attempt(self, job: dict, fd: int, have: int) -> None:
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
        url, headers = self.gw.file_request(job["part_key"])
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
            try:
                for chunk in itertools.chain(first, chunks):
                    room = size - pos
                    take = chunk if len(chunk) <= room else chunk[:room]
                    _write_all(fd, take, pos)
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
        try:
            streams = self.gw.subtitle_streams(job["rating_key"], job["part_id"])
        except Exception as e:  # noqa: BLE001
            return {"total": 0, "written": [], "error": f"subtitles not checked: {e}"[:300]}
        written, problems, used = [], [], set()
        for s in streams:
            ext = SUB_EXT.get(s["codec"])
            if not ext:
                problems.append(f"{s['title'] or s['codec']}: image-based, skipped")
                continue
            rel = sidecar_rel(job["dest_rel"], s["lang"], s["forced"], s["hi"], ext, used)
            try:
                data = self.gw.fetch_bytes(s["key"])
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
            except Exception as e:  # noqa: BLE001
                problems.append(f"{s['title'] or rel}: {e}")
        return {"total": len(streams), "written": written,
                "error": ("subtitles: " + "; ".join(problems))[:500] if problems else None}
