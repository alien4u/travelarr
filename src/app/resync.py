"""Re-sync: compare the videos in a Local Files folder with the same folder on Plex,
and queue what changed.

The NAS decides what is compared: every folder in the scope is listed next to Plex's
folder of the same name (Plex's folder view mirrors the library folders), and videos
are matched by file name and size. Folders that exist only on Plex are left alone.
Of a title Plex has in several versions, only the one the version setting picks is
compared, at the path a copy of it gets (an optimized version under the original's name).

A check never changes anything. Apply recomputes the check and acts only on items
that are still out of date: a new version replaces the old file once it is in place,
a video Plex has that is missing from the folder is copied, and on request every
video that is up to date gets its subtitles fetched again, without touching the video."""
from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

from .paths import SUB_EXT, PathError, abs_path, dest_rel_for, local_rel, safe_name
from .plexgw import chosen_parts
from .store import label

LOCK = threading.Lock()  # one check or apply at a time; selecting a server waits for it
ACTIONABLE = ("upgraded", "changed", "absent")
OTHER_VERSION = "another version of this title on Plex; the version setting copies a different one. The file stays"
VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".mts", ".mpg", ".mpeg",
             ".webm", ".flv", ".vob", ".divx", ".ogm", ".3gp", ".asf", ".iso"}
EPISODE = re.compile(r"S(\d{1,3})E(\d{1,4})", re.I)
BUSY = "being copied, or a re-sync of it is unfinished (see the Queue)"
FAILED = "its copy failed; retry or remove it in the Queue"
SAMPLE = 64 << 10  # bytes compared at the start and at the end of a file with Plex's name and size
READERS = 4
NEW_BYTES = "new version on Plex: same name and size, different content"
log = logging.getLogger("travelarr.resync")


class ResyncError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


#------------------------------------------------------------------ the NAS
def _nas_dir(media_root: str, rel: str) -> tuple[dict[str, int], list[str]] | None:
    """Videos (name -> size) and subfolders of a NAS folder; None for a folder
    that is missing or reached through a link."""
    root = os.path.realpath(media_root)
    full = os.path.join(root, *rel.split("/")) if rel else root
    if os.path.realpath(full) != full or not os.path.isdir(full):
        return None
    files: dict[str, int] = {}
    dirs: list[str] = []
    with os.scandir(full) as it:
        for e in it:
            if e.name.startswith("."):
                continue
            if e.is_dir(follow_symlinks=False):
                dirs.append(e.name)
            elif e.is_file(follow_symlinks=False) and posixpath.splitext(e.name)[1].lower() in VIDEO_EXT:
                files[e.name] = e.stat(follow_symlinks=False).st_size
    return files, sorted(dirs)


def _sidecars(media_root: str, video_rel: str) -> list[str]:
    """Subtitle files next to a video that carry its name: 'Movie.mkv' -> 'Movie.en.srt'."""
    folder, stem = posixpath.split(posixpath.splitext(video_rel)[0])
    names = os.listdir(os.path.join(os.path.realpath(media_root), *folder.split("/")))
    exts = {"." + e for e in SUB_EXT.values()}
    return sorted(posixpath.join(folder, n) for n in names
                  if n.startswith(stem + ".") and posixpath.splitext(n)[1].lower() in exts)


def _libraries(srv) -> dict[str, tuple]:
    """NAS library folder name -> (section, key of its Plex folder view), the same
    name the copies are placed under (see `dest_rel_for`)."""
    out = {}
    for sec in srv.library.sections():
        locs = [x.replace("\\", "/").rstrip("/") for x in sec.locations]
        if sec.type in ("movie", "show") and len(locs) == 1:
            out[posixpath.basename(locs[0]) or safe_name(sec.title)] = (sec, f"/library/sections/{sec.key}/folder")
    return out


def _plex_dir(srv, key: str | None) -> tuple[list, dict[str, str]]:
    """Videos and subfolders (name -> key) of a folder in Plex's folder view."""
    if key is None:
        return [], {}
    data = srv.query(key)
    dirs = {e.attrib["title"]: e.attrib["key"] for e in data
            if e.tag == "Directory" and e.attrib.get("title") and e.attrib.get("key")}
    videos = [v for v in srv.findItems(data, initpath=key) if getattr(v, "type", None) in ("movie", "episode")]
    for v in videos:
        v._autoReload = False
    return videos, dirs


def _item(kind: str, path: str, job: dict | None = None, **fields) -> dict:
    return {"ratingKey": job["rating_key"] if job else None,
            "title": (job["show_title"] or job["title"]) if job else posixpath.basename(path),
            "label": label(job) if job else posixpath.basename(path),
            "kind": kind, "reason": None, "video": None, "videoBytes": 0, "watched": False, "path": path,
            "_job": job, **fields}


def _folder(rel: str, files: dict[str, int], videos: list, sec, sid: str, gw, root: bool) -> list[dict]:
    """Compare one NAS folder with its Plex folder. `root` is a library folder,
    where only what is on the NAS is compared (Plex has every title there)."""
    out, plex = [], []
    claimed: set[str] = set()
    others: set[str] = set()  # this folder's files of the versions not picked
    pref = gw.version_pref()
    for v in videos:
        parts = chosen_parts(v, sec, pref)
        names = {posixpath.basename(dest) for _, dest in parts}
        others |= {posixpath.basename(d) for p in v.iterParts()
                   if posixpath.dirname(d := dest_rel_for(p.file, sec.locations, sec.title)) == rel} - names
        if len(parts) != 1:
            here = sorted(names & set(files))
            claimed |= names
            if here:
                out.append(_item("multipart", f"{rel}/{here[0]}", reason="videos split into several files "
                                                                         "are not re-synced"))
            continue
        job = gw.job(v, parts[0][0], parts[0][1], sid, "resync")
        if posixpath.dirname(job["dest_rel"]) == rel:
            plex.append((v, job))
    waiting = []
    for v, job in plex:  # the same name: up to date, or a new version of the same file
        name = posixpath.basename(job["dest_rel"])
        if name not in files:
            waiting.append((v, job))
            continue
        claimed.add(name)
        if files[name] == job["size"]:
            out.append(_item("unchanged", job["dest_rel"], job))
        else:
            out.append(_item("changed", job["dest_rel"], job, videoBytes=job["size"], video={
                "from": {"file": name, "size": files[name]}, "to": {"file": name, "size": job["size"]}}))
    for v, job in waiting:  # a new name: the old file is the same episode, or the movie folder's only video
        free = [n for n in files if n not in claimed]
        if job["kind"] == "episode":
            same = [n for n in free if (m := EPISODE.search(n))
                    and (int(m.group(1)), int(m.group(2))) == (job["season"], job["episode"])]
        else:
            same = free if not root and len(waiting) == 1 else []
        if len(same) == 1:
            old = same[0]
            claimed.add(old)
            out.append(_item("upgraded", job["dest_rel"], job, videoBytes=job["size"], _old=f"{rel}/{old}", video={
                "from": {"file": old, "size": files[old]},
                "to": {"file": posixpath.basename(job["dest_rel"]), "size": job["size"]}}))
        elif not root:
            out.append(_item("absent", job["dest_rel"], job, videoBytes=job["size"], reason="not on the NAS",
                             watched=bool(getattr(v, "isWatched", False))))
    for name in sorted(set(files) - claimed):
        if name in others:
            out.append(_item("otherversion", f"{rel}/{name}", reason=OTHER_VERSION))
        else:
            out.append(_item("notonplex", f"{rel}/{name}", reason="not on Plex; the file stays"))
    return out


def _same_bytes(gw, srv, media_root: str, job: dict) -> bool | None:
    """The first and last SAMPLE bytes of the NAS file against Plex's; None when
    Plex's could not be read."""
    size = job["size"]
    n = min(SAMPLE, size)
    try:
        with open(os.path.join(os.path.realpath(media_root), *job["dest_rel"].split("/")), "rb") as fh:
            for start in sorted({0, size - n}):
                fh.seek(start)
                theirs = gw.read_range(srv, job["part_key"], size, start, n)
                if theirs is None:
                    return None
                if theirs != fh.read(n):
                    return False
    except (OSError, requests.RequestException) as e:
        log.warning("re-sync could not compare %s: %s", job["dest_rel"], e)
        return None
    return True


def _verify(gw, srv, media_root: str, items: list[dict], only: set | None) -> None:
    """Videos with Plex's name and size whose bytes differ are a new version."""
    todo = [i for i in items if i["kind"] == "unchanged" and i["_job"]["size"]
            and (only is None or i["ratingKey"] in only)]
    with ThreadPoolExecutor(READERS) as pool:
        for i, same in zip(todo, pool.map(lambda i: _same_bytes(gw, srv, media_root, i["_job"]), todo)):
            if same is False:
                i.update(kind="changed", reason=NEW_BYTES, videoBytes=i["_job"]["size"])
            elif same is None:
                i["_unverified"] = True


def _compute(store, gw, media_root: str, path: str, only: set | None = None) -> dict:
    """`only`: the rating keys whose bytes are compared (all when None)."""
    srv, sid = gw.snapshot()
    if not sid:
        raise ResyncError(409, "Choose a Plex server first")
    libs = _libraries(srv)
    parts = path.split("/") if path else []
    if parts:
        tops = [parts[0]] if parts[0] in libs else []
    else:
        tops = [n for n in (_nas_dir(media_root, "") or ({}, []))[1] if n in libs]
    items: list[dict] = []
    checked = 0

    def walk(rel: str, key: str | None, sec, root: bool) -> None:
        nonlocal checked
        nas = _nas_dir(media_root, rel)
        if nas is None:  # only on Plex: never copied, or deleted on purpose
            return
        files, subdirs = nas
        videos, pdirs = _plex_dir(srv, key)
        checked += len(files)
        items.extend(_folder(rel, files, videos, sec, sid, gw, root))
        for d in subdirs:
            walk(f"{rel}/{d}", pdirs.get(d), sec, False)

    for top in tops:
        sec, key = libs[top]
        rel = top
        for name in parts[1:]:  # Plex's folder for the scope, one level at a time
            key = _plex_dir(srv, key)[1].get(name)
            rel = f"{rel}/{name}"
        walk(rel, key, sec, rel == top)
    _rows(store, sid, media_root, items)
    _verify(gw, srv, media_root, items, only)
    return {"serverId": sid, "items": items, "checked": checked}


def _rows(store, sid: str, media_root: str, items: list[dict]) -> None:
    """The done rows of each item's files (`_row`, `_oldrow`, and `_stale`: the row
    of the same Plex file at a path whose video is gone). Items with an unfinished
    or failed job are left to the Queue."""
    rows = store.rows("SELECT * FROM jobs WHERE status='done'")
    done = {r["dest_rel"]: r for r in rows}
    by_part = {r["part_id"]: r for r in rows if r["server_id"] == sid}
    active, failed, locked = set(), set(), set()
    for r in store.rows("SELECT * FROM jobs WHERE status IN ('queued','downloading','failed')"):
        mine = r["server_id"] == sid
        if r["status"] != "failed":
            active |= {r["dest_rel"]} | ({("rk", r["rating_key"])} if mine else set())
        else:
            failed |= {r["dest_rel"]} | ({("rk", r["rating_key"])} if mine else set())
        if r["mode"] == "upgrade" and (r["phase"] or r["status"] == "failed"):
            locked |= {int(o) for o in json.loads(r["replaces"] or "[]")}
    for i in items:
        if not i["ratingKey"]:
            continue
        paths = {i["path"]} | ({i["_old"]} if i.get("_old") else set())
        mine = [done[p] for p in paths if p in done]
        i["_row"] = done.get(i["path"])
        i["_oldrow"] = done.get(i.get("_old"))
        stale = by_part.get(i["_job"]["part_id"])
        i["_stale"] = stale if i["kind"] == "absent" and stale and not _exists(media_root, stale["dest_rel"]) else None
        if paths & active or ("rk", i["ratingKey"]) in active or any(r["id"] in locked for r in mine):
            i.update(kind="busy", reason=BUSY)
        elif paths & failed or ("rk", i["ratingKey"]) in failed:
            i.update(kind="busy", reason=FAILED)


def _exists(media_root: str, rel: str) -> bool:
    try:
        return os.path.lexists(abs_path(media_root, rel))
    except PathError:
        return True


def title_folder(media_root: str, d: dict) -> str | None:
    """The NAS folder a movie or show page re-syncs: the show's folder, or the
    movie's own folder (not a library folder); None when it is not on the NAS."""
    if d["type"] == "show":
        dests = [e["dest"] for s in d["seasons"] for e in s["episodes"] if e.get("dest")]
        rel = "/".join(dests[0].split("/")[:2]) if dests else None
    elif d["type"] == "movie":
        rel = posixpath.dirname(d["parts"][0]["dest"]) if d.get("parts") else None
    else:
        return None
    if not rel or "/" not in rel:
        return None
    try:
        full = abs_path(media_root, rel)
    except PathError:
        return None
    return rel if os.path.isdir(full) and os.path.realpath(full) == full else None


def _public(item: dict) -> dict:
    return {k: v for k, v in item.items() if not k.startswith("_")}


def scope_path(path) -> str:
    if not isinstance(path, str):
        raise ResyncError(400, "path must be a folder in Local Files")
    try:
        return local_rel(path)
    except PathError as e:
        raise ResyncError(400, str(e)) from None


def check(store, gw, media_root: str, path: str) -> dict:
    if not LOCK.acquire(blocking=False):
        raise ResyncError(409, "A re-sync check is already running")
    try:
        d = _compute(store, gw, media_root, path)
    finally:
        LOCK.release()
    return {"serverId": d["serverId"], "checked": d["checked"],
            "unchanged": sum(1 for i in d["items"] if i["kind"] == "unchanged"),
            "unverified": sum(1 for i in d["items"] if i.get("_unverified")),
            "items": [_public(i) for i in d["items"] if i["kind"] != "unchanged"]}


CURRENT = ("rating_key", "show_key", "title", "show_title", "season", "episode", "year", "part_id", "part_key",
           "size", "server_id", "guid")


def apply(store, gw, media_root: str, path: str, server_id, rating_keys, subtitles: bool = False) -> dict:
    """rating_keys are the out-of-date items to update; with `subtitles`, every
    video in the folder that is up to date gets its subtitles fetched again."""
    if not (isinstance(rating_keys, list)
            and all(isinstance(k, str) and re.fullmatch(r"[0-9]{1,12}", k) for k in rating_keys)
            and len(set(rating_keys)) == len(rating_keys)):
        raise ResyncError(400, "ratingKeys must be different rating keys")
    if not LOCK.acquire(blocking=False):
        raise ResyncError(409, "A re-sync check is already running")
    try:
        d = _compute(store, gw, media_root, path, set(rating_keys))  # bytes compared again only for the ticked
        if server_id != d["serverId"]:
            raise ResyncError(409, "The Plex server changed since the check; check again")
        by_rk = {i["ratingKey"]: i for i in d["items"] if i["ratingKey"]}
        outside = [k for k in rating_keys if k not in by_rk]
        if outside:
            raise ResyncError(400, f"Not part of this re-sync: {', '.join(outside[:5])}")
        out = {"upgrades": 0, "redownloads": 0, "copies": 0, "refreshes": 0, "videoBytes": 0, "skipped": 0,
               "warning": None}
        picked = set(rating_keys)
        for rk, item in by_rk.items():
            if rk not in picked and not (subtitles and item["kind"] == "unchanged"):
                continue
            if item["kind"] not in ACTIONABLE + ("unchanged",):
                out["skipped"] += 1
                continue
            key = _act(store, media_root, item)
            out[key or "skipped"] += 1
            if key and key != "refreshes":
                out["videoBytes"] += item["videoBytes"]
        return out
    finally:
        LOCK.release()


def _adopt(store, media_root: str, job: dict, rel: str, current: bool) -> dict:
    """A file travelarr never copied, recorded with its subtitles as a done copy so
    it is refreshed or replaced like any other. `current`: it is Plex's file."""
    fields = {**job, "dest_rel": rel}
    if not current:
        size = os.path.getsize(os.path.join(os.path.realpath(media_root), *rel.split("/")))
        fields.update(part_id=0, part_key="", size=size)
    fields["id"] = store.adopt(fields, _sidecars(media_root, rel))
    return fields


def _act(store, media_root: str, item: dict) -> str | None:
    """Queue one item; returns the counter it adds to, None when it was skipped."""
    job, kind = item["_job"], item["kind"]
    current = {k: job[k] for k in CURRENT}
    done = lambda r: r["status"] == "done"  # noqa: E731
    if kind == "absent":
        row = item["_row"] or item["_stale"]
        if row is None:
            return "copies" if store.add_jobs([job])[0] else None
        # travelarr's copy is gone: the same row copies Plex's file again, at Plex's path
        ok = store.transition(row["id"], done, status="queued", mode="redownload", bytes_done=0, validator=None,
                              note="re-sync: copying again", dest_rel=job["dest_rel"], **current)
        return "redownloads" if ok else None
    if kind == "upgraded":
        old = item["_oldrow"] or _adopt(store, media_root, job, item["_old"], False)
    else:
        old = item["_row"] or _adopt(store, media_root, job, job["dest_rel"], kind == "unchanged")
    if kind == "unchanged":
        ok = store.transition(old["id"], done, status="queued", mode="refresh", note="re-sync: subtitles", **current)
        return "refreshes" if ok else None
    if kind == "changed" and (old["part_id"], old["size"]) == (job["part_id"], job["size"]):
        # travelarr's copy of Plex's current file is damaged: the same row copies it again
        ok = store.transition(old["id"], done, status="queued", mode="redownload", bytes_done=0, validator=None,
                              note="re-sync: copying again", **current)
        return "redownloads" if ok else None
    added, _ = store.add_jobs([{**job, "mode": "upgrade", "replaces": json.dumps([old["id"]])}])
    return "upgrades" if added else None
