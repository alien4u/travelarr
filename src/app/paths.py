"""Destination path rules. Every path travelarr writes or deletes goes through
here, so these are the only places that need to be right about safety."""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
import shutil
import stat
import threading
import time
from pathlib import PurePosixPath

PART_SUFFIX = ".travelarr-part"
SIZE_BUDGET = 5.0  # seconds a Local Files listing may spend sizing folders
_sizing = threading.BoundedSemaphore(2)  # sizing walks running at once


class PathError(Exception):
    pass


class ListingChanged(Exception):
    """The folder is not the one the previous page came from."""


def depth(rel: str) -> int:
    return len(PurePosixPath(rel).parts)


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", name or "").strip(" .")
    return name or "Unknown"


def clean_rel(rel: str) -> str:
    """A relative path under the media root, at least 'Library/item'."""
    rel = (rel or "").replace("\\", "/")
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts or "." in p.parts or depth(rel) < 2:
        raise PathError(f"unsafe destination path: {rel!r}")
    return str(p)


def dest_rel_for(part_file: str, locations: list[str], section_title: str) -> str:
    """Rebuild the server-side layout under the library folder's own name.

    Plex file  /data/Media/TV-Shows/Show/Season 01/ep.mkv
    Library    /data/Media/TV-Shows
    Result     TV-Shows/Show/Season 01/ep.mkv
    """
    f = posixpath.normpath((part_file or "").replace("\\", "/"))
    for loc in sorted(locations or [], key=len, reverse=True):
        root = posixpath.normpath(loc.replace("\\", "/")).rstrip("/")
        if root and f.startswith(root + "/"):
            folder = posixpath.basename(root) or safe_name(section_title)
            return clean_rel(posixpath.join(folder, f[len(root) + 1:]))
    # File outside the library's known folders: keep it findable anyway.
    parent = posixpath.basename(posixpath.dirname(f)) or "Unknown"
    return clean_rel(posixpath.join(safe_name(section_title), parent, posixpath.basename(f)))


VERSIONS_DIR = "Plex Versions"  # where Plex keeps the files Optimize makes, by default


def in_versions(file: str) -> bool:
    return VERSIONS_DIR in PurePosixPath((file or "").replace("\\", "/")).parts[:-1]


def unversioned(file: str) -> str:
    """The path an optimized file would have without Plex's folders for it:
    .../Heat (1995)/Plex Versions/Optimized for TV/Heat (1995).mp4
    -> .../Heat (1995)/Heat (1995).mp4"""
    parts = list(PurePosixPath((file or "").replace("\\", "/")).parts)
    if VERSIONS_DIR not in parts[:-1]:
        return file
    i = parts.index(VERSIONS_DIR)
    rest = parts[i + 1:]
    return str(PurePosixPath(*parts[:i], *(rest[1:] if len(rest) > 1 else rest)))


def version_rel(original_rel: str, file: str) -> str:
    """Where another version of a video goes: the original's name with the
    version's own extension, so 'Heat (1995).mkv' -> 'Heat (1995).mp4'."""
    ext = posixpath.splitext(posixpath.basename((file or "").replace("\\", "/")))[1]
    return clean_rel(posixpath.splitext(original_rel)[0] + ext)


def abs_path(media_root: str, rel: str) -> str:
    rel = clean_rel(rel)
    root = os.path.realpath(media_root)
    path = os.path.join(root, *PurePosixPath(rel).parts)
    real_parent = os.path.realpath(os.path.dirname(path))
    if not (real_parent == root or real_parent.startswith(root + os.sep)):
        raise PathError(f"destination escapes the media folder: {rel!r}")
    return path


def remove_files(media_root: str, rels: list[str]) -> list[str]:
    """Delete exactly these files, then any folders they leave empty.
    Never removes a library root (depth 1) or the media root itself."""
    removed = []
    parents: set[str] = set()
    for rel in rels:
        if not rel:
            continue
        p = abs_path(media_root, rel)
        if os.path.isfile(p) or os.path.islink(p):
            os.remove(p)
            removed.append(rel)
        parent = posixpath.dirname(clean_rel(rel))
        while depth(parent) >= 2:
            parents.add(parent)
            parent = posixpath.dirname(parent)
    for rel_dir in sorted(parents, key=depth, reverse=True):
        try:
            os.rmdir(os.path.join(os.path.realpath(media_root), *PurePosixPath(rel_dir).parts))
        except OSError:
            pass  # not empty or already gone
    return removed


# Paths here are exact POSIX entry paths, so a listed path always names the
# same entry again: unlike clean_rel, a backslash is an ordinary character.

def local_rel(rel: str, min_depth: int = 0) -> str:
    """'' for the media root, else 'Library/...'; '.' segments and extra
    slashes are canonicalized away, '..' and absolute paths refused."""
    if "\x00" in rel:
        raise PathError(f"unsafe path: {rel!r}")
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts:
        raise PathError(f"unsafe path: {rel!r}")
    if len(p.parts) < min_depth:
        raise PathError("the media folder and library folders cannot be deleted here")
    return "/".join(p.parts)


def _inside(root: str, real: str) -> bool:
    return real == root or real.startswith(root + os.sep)


def local_dir(media_root: str, rel: str) -> tuple[str, str]:
    """A folder to list. The whole path is resolved, so a link leading out of
    the media folder is refused wherever it sits."""
    rel = local_rel(rel)
    root = os.path.realpath(media_root)
    path = os.path.join(root, *rel.split("/")) if rel else root
    if not _inside(root, os.path.realpath(path)):
        raise PathError(f"outside the media folder: {rel!r}")
    return rel, path


def local_entry(media_root: str, rel: str) -> tuple[str, str]:
    """An entry to delete, inside a library folder. Its folders must not go
    through a link, so the path is the entry's one real name and the depth
    rule holds; the entry itself is never followed."""
    rel = local_rel(rel, 2)
    root = os.path.realpath(media_root)
    path = os.path.join(root, *rel.split("/"))
    if os.path.realpath(os.path.dirname(path)) != os.path.dirname(path):
        raise PathError(f"the path goes through a link: {rel!r}")
    return rel, path


def real_rel(media_root: str, rel: str) -> str:
    """rel with links in its folders resolved, relative to the real media
    folder: where a job's file really lands. rel itself if that is outside."""
    root = os.path.realpath(media_root)
    parent = os.path.realpath(os.path.join(root, *posixpath.dirname(rel).split("/")))
    if not _inside(root, parent):
        return rel
    folder = os.path.relpath(parent, root)
    name = posixpath.basename(rel)
    return name if folder == "." else "/".join(folder.split(os.sep) + [name])


def tree_size(path: str, deadline: float | None = None) -> tuple[int, int, bool]:
    """Logical bytes and file count under a folder, never following links.
    False when something could not be read or the deadline passed, so the
    numbers are only a lower bound."""
    size = files = 0
    complete = True
    stack = [path]
    while stack:
        if deadline is not None and time.monotonic() >= deadline:
            return size, files, False
        try:
            it = os.scandir(stack.pop())
        except OSError:
            complete = False
            continue
        with it:
            for e in it:
                if deadline is not None and time.monotonic() >= deadline:
                    return size, files, False
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    else:
                        size += e.stat(follow_symlinks=False).st_size
                        files += 1
                except OSError:
                    complete = False
    return size, files, complete


def _natural(name: str) -> list:
    """'Season 2' before 'Season 10'."""
    return [int(t) if i % 2 else t.casefold() for i, t in enumerate(re.split(r"([0-9]+)", name))]


def list_dir(media_root: str, rel: str, offset: int, limit: int, token: str | None) -> dict:
    """One page of a folder: folders first, natural order, dotfiles hidden.
    token identifies the folder's contents; a page continuing an earlier one
    passes it and gets ListingChanged if the folder changed since."""
    deadline = time.monotonic() + SIZE_BUDGET
    rel, path = local_dir(media_root, rel)
    direct = os.path.realpath(path) == path  # entries reached through a link cannot be deleted
    rows = []
    with os.scandir(path) as it:
        for e in it:
            if e.name.startswith("."):
                continue
            try:
                e.name.encode()
            except UnicodeEncodeError:
                continue
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            rows.append((not is_dir, _natural(e.name), e.name, e))
    rows.sort(key=lambda r: r[:3])
    digest = hashlib.sha1("\0".join(("f" if r[0] else "d") + r[2] for r in rows).encode()).hexdigest()
    if token is not None and token != digest:
        raise ListingChanged()
    entries, slot = [], False
    try:
        for not_dir, _, name, e in rows[offset:offset + limit]:
            erel = f"{rel}/{name}" if rel else name
            item = {"name": name, "path": erel, "dir": not not_dir, "link": False, "partial": False,
                    "size": 0, "files": 1, "complete": True, "deletable": direct and depth(erel) >= 2}
            entries.append(item)
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                item.update(files=0, complete=False)
                continue
            if not_dir:
                item.update(link=stat.S_ISLNK(st.st_mode), partial=name.endswith(PART_SUFFIX), size=st.st_size)
                continue
            item["files"] = 0
            if not slot:
                slot = _sizing.acquire(timeout=max(0.0, deadline - time.monotonic()))
            if slot:
                item["size"], item["files"], item["complete"] = tree_size(e.path, deadline)
            else:
                item["complete"] = False
    finally:
        if slot:
            _sizing.release()
    return {"path": rel, "total": len(rows), "offset": offset, "token": digest, "entries": entries,
            "complete": all(i["complete"] for i in entries)}


def delete_entry(media_root: str, rel: str) -> tuple[int, int]:
    """Delete one entry; returns its logical bytes and file count. A real
    folder goes with everything in it. Anything else, links included, is
    removed as an entry: a link's target is never touched."""
    _, path = local_entry(media_root, rel)
    st = os.lstat(path)
    if stat.S_ISDIR(st.st_mode):
        size, files, _ = tree_size(path)
        shutil.rmtree(path)
        return size, files
    os.unlink(path)
    return st.st_size, 1


def touches(target: str, dest_rel: str) -> str | None:
    """What deleting target takes from a job writing dest_rel: 'video' when it
    is the video, its partial or a folder above them; 'sidecar' when it is a
    sibling named '<video stem>.*' (subtitles and their partials)."""
    for p in (dest_rel, dest_rel + PART_SUFFIX):
        if p == target or p.startswith(target + "/"):
            return "video"
    folder, name = posixpath.split(target)
    vfolder, vname = posixpath.split(dest_rel)
    if folder == vfolder and name.startswith(posixpath.splitext(vname)[0] + "."):
        return "sidecar"
    return None


SUB_EXT = {"srt": "srt", "subrip": "srt", "ass": "ass", "ssa": "ssa",
           "vtt": "vtt", "webvtt": "vtt", "smi": "smi"}


def sidecar_rel(video_rel: str, lang: str | None, forced: bool, hi: bool,
                ext: str, used: set[str]) -> str:
    """'Show - S01E01.mkv' + es, forced -> 'Show - S01E01.es.forced.srt'
    (the naming Infuse, Plex and Jellyfin all read)."""
    stem = posixpath.splitext(video_rel)[0]
    tag = re.sub(r"[^A-Za-z0-9-]", "", lang or "") or "und"
    bits = [stem, tag]
    if forced:
        bits.append("forced")
    if hi:
        bits.append("sdh")
    base = ".".join(bits)
    name, n = f"{base}.{ext}", 2
    while name in used:
        name = f"{base}.{n}.{ext}"
        n += 1
    used.add(name)
    return clean_rel(name)
