"""Re-sync: compare the videos copied into a Local Files folder with what Plex has
now, and queue what changed.

A check never changes anything. Apply recomputes the check and acts only on items
that are still out of date: an upgraded video gets a new job that replaces the old
copy once it is in place; a missing or damaged copy is copied again. Every other
copy in the folder gets its subtitles fetched again, without touching the video."""
from __future__ import annotations

import json
import posixpath
import re
import threading
from collections import defaultdict

from .paths import PathError, abs_path, dest_rel_for, local_rel
from .worker import _regular_size

LOCK = threading.Lock()  # one check or apply at a time; selecting a server waits for it
ACTIONABLE = ("upgraded", "missing")


class ResyncError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _label(row: dict) -> str:
    if row["kind"] == "episode":
        return f"{row['show_title']} S{(row['season'] or 0):02d}E{(row['episode'] or 0):02d} {row['title']}"
    return f"{row['title']} ({row['year']})" if row.get("year") else row["title"]


def _scope(store, sid: str, path: str) -> list[dict]:
    """Copies inside a Local Files folder ('' is the whole media folder), plus
    unfinished jobs of the same items so those show as busy."""
    sql = ("SELECT * FROM jobs WHERE status IN ('done','queued','downloading') "
           "AND (server_id IS NULL OR server_id=?)")
    if not path:
        return store.rows(sql, (sid,))
    return store.rows(sql + " AND (dest_rel=? OR substr(dest_rel, 1, ?)=?)", (sid, path, len(path) + 1, path + "/"))


def _same_item(row: dict, it, sid: str, dest: str | None) -> bool:
    if row["server_id"] and row["server_id"] != sid:
        return False
    if row["guid"]:
        return row["guid"] == getattr(it, "guid", None)
    # a copy from before 2.5.0: same title, and Plex's file is in the same folder
    if row["kind"] == "episode":
        same = (getattr(it, "grandparentTitle", None) == row["show_title"]
                and getattr(it, "parentIndex", None) == row["season"] and getattr(it, "index", None) == row["episode"])
    else:
        same = it.title == row["title"] and (row["year"] is None or getattr(it, "year", None) == row["year"])
    return same and dest is not None and posixpath.dirname(dest) == posixpath.dirname(row["dest_rel"])


def _busy(store, sid: str, rows_by_rk: dict) -> set[str]:
    rks = list(rows_by_rk)
    busy: set[str] = set()
    for i in range(0, len(rks), 500):
        chunk = rks[i:i + 500]
        marks = ",".join("?" * len(chunk))
        busy |= {r["rating_key"] for r in store.rows(
            f"SELECT rating_key FROM jobs WHERE status IN ('queued','downloading') AND rating_key IN ({marks}) "
            "AND (server_id IS NULL OR server_id=?)", chunk + [sid])}
    ids = {r["id"]: rk for rk, rows in rows_by_rk.items() for r in rows}
    for u in store.rows("SELECT * FROM jobs WHERE mode='upgrade' AND status IN ('queued','downloading','failed')"):
        for o in json.loads(u["replaces"] or "[]"):
            if int(o) in ids and (u["phase"] or u["status"] == "failed"):
                busy.add(ids[int(o)])
    return busy


def _compute(store, gw, media_root: str, path: str) -> dict:
    srv, sid = gw.snapshot()
    if not sid:
        raise ResyncError(409, "Choose a Plex server first")
    rows = _scope(store, sid, path)
    by_rk: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_rk[r["rating_key"]].append(r)
    plex = gw.batch(srv, list(by_rk))
    busy = _busy(store, sid, by_rk)
    items = []
    for rk, rws in by_rk.items():
        row = rws[0]
        it = plex.get(rk)
        item = {"ratingKey": rk, "title": row["show_title"] or row["title"], "label": _label(row),
                "kind": "unchanged", "reason": None, "video": None, "videoBytes": 0,
                "_rows": rws, "_item": it}
        items.append(item)
        if rk in busy or any(r["status"] != "done" for r in rws):
            item.update(kind="busy", reason="being copied, or a re-sync of it is unfinished (see the Queue)")
            continue
        if it is None:
            item.update(kind="gone", reason="no longer on Plex; the copy stays")
            continue
        parts = list(it.iterParts())
        sec = gw._section(it.librarySectionID, srv)
        dests = [dest_rel_for(p.file, sec.locations, sec.title) for p in parts]
        if not all(_same_item(r, it, sid, dests[0] if dests else None) for r in rws):
            item.update(kind="mismatch", reason="this copy came from a different item or Plex server")
            continue
        if not parts:
            item.update(kind="unavailable", reason="Plex has no file for it right now")
            continue
        if len(parts) > 1 or len(rws) > 1:
            item.update(kind="multipart", reason="videos split into several files are not re-synced; "
                                                 "remove and copy again")
            continue
        p, dest = parts[0], dests[0]
        item["_part"], item["_dest"] = p, dest
        if p.id != row["part_id"] or dest != row["dest_rel"] or (p.size or 0) != row["size"]:
            item.update(kind="upgraded", videoBytes=p.size or 0, video={
                "from": {"file": posixpath.basename(row["dest_rel"]), "size": row["size"]},
                "to": {"file": posixpath.basename(dest), "size": p.size or 0}})
            continue
        try:
            here = _regular_size(abs_path(media_root, row["dest_rel"]))
        except PathError:
            here = None
        if here != row["size"]:
            item.update(kind="missing", videoBytes=row["size"], reason="missing on the NAS" if here is None
                        else "the file on the NAS has the wrong size")
    return {"serverId": sid, "srv": srv, "items": items, "checked": sum(1 for r in rows if r["status"] == "done")}


def bind_legacy(store, gw, rows: list[dict], expected_sid: str) -> set[int]:
    """Rows from before 2.5.0 (no server) that are verifiably the selected server's
    item get bound to it; returns their ids. Unverifiable rows are left alone."""
    legacy = [r for r in rows if not r["server_id"]]
    if not legacy:
        return set()
    srv, sid = gw.snapshot()
    if sid != expected_sid:
        raise ResyncError(409, "The Plex server changed; reload and try again")
    plex = gw.batch(srv, sorted({r["rating_key"] for r in legacy}))
    ok = set()
    for r in legacy:
        it = plex.get(r["rating_key"])
        if it is None:
            continue
        parts = list(it.iterParts())
        sec = gw._section(it.librarySectionID, srv)
        dest = dest_rel_for(parts[0].file, sec.locations, sec.title) if parts else None
        if _same_item(r, it, sid, dest) and store.bind_server(r["id"], sid, getattr(it, "guid", None)):
            ok.add(r["id"])
    return ok


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
            "items": [_public(i) for i in d["items"] if i["kind"] != "unchanged"]}


def apply(store, gw, media_root: str, path: str, server_id, rating_keys) -> dict:
    """rating_keys are the out-of-date items to update; every unchanged copy in
    the folder gets its subtitles fetched again."""
    if not (isinstance(rating_keys, list)
            and all(isinstance(k, str) and re.fullmatch(r"[0-9]{1,12}", k) for k in rating_keys)
            and len(set(rating_keys)) == len(rating_keys)):
        raise ResyncError(400, "ratingKeys must be different rating keys")
    if not LOCK.acquire(blocking=False):
        raise ResyncError(409, "A re-sync check is already running")
    try:
        d = _compute(store, gw, media_root, path)
        if server_id != d["serverId"]:
            raise ResyncError(409, "The Plex server changed since the check; check again")
        by_rk = {i["ratingKey"]: i for i in d["items"]}
        outside = [k for k in rating_keys if k not in by_rk]
        if outside:
            raise ResyncError(400, f"Not part of this re-sync: {', '.join(outside[:5])}")
        out = {"upgrades": 0, "redownloads": 0, "refreshes": 0, "videoBytes": 0, "skipped": 0, "warning": None}
        sid = d["serverId"]
        picked = set(rating_keys)
        for rk, item in by_rk.items():
            if rk not in picked and item["kind"] != "unchanged":
                continue
            if item["kind"] not in ACTIONABLE + ("unchanged",):
                out["skipped"] += 1
                continue
            row, it = item["_rows"][0], item["_item"]
            if not row["server_id"] or not row["guid"]:  # a verified copy from before 2.5.0
                store.update(row["id"], server_id=row["server_id"] or sid,
                             guid=row["guid"] or getattr(it, "guid", None))
            if item["kind"] == "upgraded":
                p = item["_part"]
                job = {k: row[k] for k in ("rating_key", "show_key", "kind", "show_title", "season", "episode", "year")}
                job.update(title=it.title, part_id=p.id, part_key=p.key, size=p.size or 0, dest_rel=item["_dest"],
                           source="resync", server_id=sid, guid=getattr(it, "guid", None), mode="upgrade",
                           replaces=json.dumps([row["id"]]))
                added, _ = store.add_jobs([job])
                if added:
                    out["upgrades"] += 1
                    out["videoBytes"] += p.size or 0
                else:
                    out["skipped"] += 1
            elif item["kind"] == "missing":
                ok = store.transition(row["id"], lambda r: r["status"] == "done", status="queued",
                                      mode="redownload", bytes_done=0, validator=None,
                                      note="re-sync: copying again")
                out["redownloads" if ok else "skipped"] += 1
                if ok:
                    out["videoBytes"] += row["size"]
            else:
                ok = store.transition(row["id"], lambda r: r["status"] == "done", status="queued",
                                      mode="refresh", note="re-sync: subtitles")
                out["refreshes" if ok else "skipped"] += 1
        return out
    finally:
        LOCK.release()
