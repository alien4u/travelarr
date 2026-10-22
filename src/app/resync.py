"""Re-sync: compare copies on the NAS with what Plex has now, and queue what changed.

A check never changes anything. Apply recomputes the check and acts only on items
that are still out of date: an upgraded video gets a new job that replaces the old
copy once it is in place; a missing or damaged copy is copied again; changed
subtitles are fetched again without touching the video."""
from __future__ import annotations

import json
import posixpath
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from plexapi.exceptions import NotFound

from .paths import SUB_EXT, PathError, abs_path, dest_rel_for, sidecar_rel
from .worker import _regular_size

LOCK = threading.Lock()  # one check or apply at a time; selecting a server waits for it
MAX_ROWS = 500
HEAD_WORKERS = 4
BUDGET = 60.0  # seconds a check may spend asking Plex for subtitle sizes
ACTIONABLE = ("upgraded", "missing", "subtitles")


class ResyncError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _label(row: dict) -> str:
    if row["kind"] == "episode":
        return f"S{(row['season'] or 0):02d}E{(row['episode'] or 0):02d} {row['title']}"
    return f"{row['title']} ({row['year']})" if row.get("year") else row["title"]


def _scope(store, srv, sid: str, key: str) -> tuple[str, list[dict]]:
    try:
        root = srv.fetchItem(int(key))
    except (NotFound, ValueError):
        raise ResyncError(400, "That item is not on the selected Plex server") from None
    # done copies, plus unfinished jobs of the same items so those show as busy
    done = ("SELECT * FROM jobs WHERE status IN ('done','queued','downloading') "
            "AND (server_id IS NULL OR server_id=?) AND ")
    if root.type in ("movie", "episode"):
        rows = store.rows(done + "rating_key=?", (sid, str(key)))
    elif root.type == "show":
        rows = store.rows(done + "show_key=?", (sid, str(key)))
    elif root.type == "season":
        rows = store.rows(done + "show_key=? AND season=?", (sid, str(root.parentRatingKey), root.index))
    else:
        raise ResyncError(400, "Only movies, shows, seasons and episodes can be re-synced")
    if len(rows) > MAX_ROWS:
        raise ResyncError(400, "Too many copies to check at once; re-sync one season at a time")
    return root.type, rows


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


def _compute(store, gw, media_root: str, key: str) -> dict:
    srv, sid = gw.snapshot()
    if not sid:
        raise ResyncError(409, "Choose a Plex server first")
    _, rows = _scope(store, srv, sid, key)
    by_rk: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_rk[r["rating_key"]].append(r)
    plex = gw.batch(srv, list(by_rk))
    busy = _busy(store, sid, by_rk)
    items, heads = [], []
    for rk, rws in by_rk.items():
        row = rws[0]
        it = plex.get(rk)
        item = {"ratingKey": rk, "title": row["show_title"] or row["title"], "label": _label(row),
                "kind": "unchanged", "reason": None, "video": None, "subtitles": None, "videoBytes": 0,
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
            continue
        expected, used = [], set()
        for s in gw.text_streams(p):
            ext = SUB_EXT.get(s["codec"])
            if ext:
                expected.append((sidecar_rel(row["dest_rel"], s["lang"], s["forced"], s["hi"], ext, used), s["key"]))
        item["_expected"] = expected
        heads += [(item, rel, k) for rel, k in expected]

    deadline = time.monotonic() + BUDGET

    def head(job):
        _, _, k = job
        if time.monotonic() > deadline:
            return "late"
        try:
            return gw.head_length(srv, k)
        except Exception:  # noqa: BLE001
            return None
    with ThreadPoolExecutor(HEAD_WORKERS) as ex:
        sizes = list(ex.map(head, heads))
    plexsize: dict[int, dict] = defaultdict(dict)
    for (item, rel, _), n in zip(heads, sizes):
        plexsize[id(item)][rel] = n
    for item in items:
        if item["kind"] != "unchanged" or "_expected" not in item:
            continue
        row = item["_rows"][0]
        got = plexsize.get(id(item), {})
        if any(v is None or v == "late" for v in got.values()):
            late = any(v == "late" for v in got.values())
            item.update(kind="unchecked", reason="time limit reached before its subtitles were checked" if late
                        else "Plex did not report its subtitle sizes")
            continue
        recorded = json.loads(row["sidecars"] or "[]")
        names = [rel for rel, _ in item["_expected"]]
        added, changed = [], []
        for rel in names:
            try:
                local = _regular_size(abs_path(media_root, rel))
            except PathError:
                local = None
            if rel not in recorded or local is None:
                added.append(rel)
            elif local != got.get(rel):
                changed.append(rel)
        removed = [rel for rel in recorded if rel not in names]
        if added or changed or removed:
            base = lambda xs: [posixpath.basename(x) for x in xs]  # noqa: E731
            item.update(kind="subtitles", subtitles={"added": base(added), "changed": base(changed),
                                                     "removed": base(removed)})
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
        if _same_item(r, it, sid, dest):
            store.update(r["id"], server_id=sid, guid=r["guid"] or getattr(it, "guid", None))
            ok.add(r["id"])
    return ok


def _public(item: dict) -> dict:
    return {k: v for k, v in item.items() if not k.startswith("_")}


def check(store, gw, media_root: str, key: str) -> dict:
    if not LOCK.acquire(blocking=False):
        raise ResyncError(409, "A re-sync check is already running")
    try:
        d = _compute(store, gw, media_root, str(key))
    finally:
        LOCK.release()
    return {"serverId": d["serverId"], "checked": d["checked"],
            "items": [_public(i) for i in d["items"] if i["kind"] != "unchanged"]}


def apply(store, gw, media_root: str, key: str, server_id, rating_keys) -> dict:
    if not (isinstance(rating_keys, list) and 1 <= len(rating_keys) <= MAX_ROWS
            and all(isinstance(k, str) and re.fullmatch(r"[0-9]{1,12}", k) for k in rating_keys)
            and len(set(rating_keys)) == len(rating_keys)):
        raise ResyncError(400, "ratingKeys must be 1 to 500 different rating keys")
    if not LOCK.acquire(blocking=False):
        raise ResyncError(409, "A re-sync check is already running")
    try:
        d = _compute(store, gw, media_root, str(key))
        if server_id != d["serverId"]:
            raise ResyncError(409, "The Plex server changed since the check; check again")
        by_rk = {i["ratingKey"]: i for i in d["items"]}
        outside = [k for k in rating_keys if k not in by_rk]
        if outside:
            raise ResyncError(400, f"Not part of this re-sync: {', '.join(outside[:5])}")
        out = {"upgrades": 0, "redownloads": 0, "refreshes": 0, "videoBytes": 0, "skipped": 0, "warning": None}
        sid = d["serverId"]
        for rk in rating_keys:
            item = by_rk[rk]
            if item["kind"] not in ACTIONABLE:
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
