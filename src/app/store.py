"""SQLite store: settings, sign-in sessions and one row per copied file (the sync history)."""
from __future__ import annotations

import json
import os
import posixpath
import sqlite3
import threading
import time

from .paths import PART_SUFFIX, PathError, abs_path, touches

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS jobs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  rating_key  TEXT NOT NULL,
  show_key    TEXT,
  kind        TEXT NOT NULL,          -- movie | episode
  title       TEXT NOT NULL,
  show_title  TEXT,
  season      INTEGER,
  episode     INTEGER,
  year        INTEGER,
  part_id     INTEGER NOT NULL,
  part_key    TEXT NOT NULL,
  size        INTEGER NOT NULL DEFAULT 0,
  dest_rel    TEXT NOT NULL,
  status      TEXT NOT NULL,          -- queued | downloading | done | failed | cancelled | removed
  bytes_done  INTEGER NOT NULL DEFAULT 0,
  error       TEXT,
  note        TEXT,
  sidecars    TEXT NOT NULL DEFAULT '[]',
  subs_total  INTEGER NOT NULL DEFAULT 0,
  subs_done   INTEGER NOT NULL DEFAULT 0,
  source      TEXT,
  queued_at   TEXT,
  started_at  TEXT,
  finished_at TEXT,
  removed_at  TEXT,
  validator   TEXT                    -- ETag|Last-Modified the partial's bytes came from
);
CREATE INDEX IF NOT EXISTS jobs_rk     ON jobs(rating_key);
CREATE INDEX IF NOT EXISTS jobs_show   ON jobs(show_key);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_part   ON jobs(part_id);
CREATE INDEX IF NOT EXISTS jobs_dest   ON jobs(dest_rel);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash  TEXT PRIMARY KEY,       -- sha256 of the cookie value
  created     INTEGER NOT NULL,       -- Unix seconds
  expires     INTEGER NOT NULL
);
"""

RANK = {"downloading": 5, "queued": 4, "done": 3, "failed": 2, "cancelled": 1, "removed": 0}

JOB_FIELDS = ("rating_key", "show_key", "kind", "title", "show_title", "season", "episode",
              "year", "part_id", "part_key", "size", "dest_rel", "source", "server_id", "guid", "mode",
              "replaces")

# Columns added after the first release, created on start when missing.
#   server_id / guid: the Plex server and item a job's data came from
#   mode: NULL (a copy), 'redownload' / 'refresh' (an existing copy re-queued by a
#     re-sync), 'upgrade' (a new version replacing the rows in `replaces`)
#   phase: NULL, 'placing' (the final rename may have happened), 'placed'
#   staging_mark: sha256 of an upgrade's staging file, proof of its placement
#   cleanup: paths this row gave up and that are still to be deleted
NEW_COLUMNS = {"validator": "TEXT", "server_id": "TEXT", "guid": "TEXT", "mode": "TEXT",
               "replaces": "TEXT NOT NULL DEFAULT '[]'", "phase": "TEXT", "staging_mark": "TEXT",
               "cleanup": "TEXT NOT NULL DEFAULT '[]'"}


def staging_rel(row: dict) -> str:
    """Where an upgrade downloads before it replaces the old copy: hidden, one per job."""
    return posixpath.join(posixpath.dirname(row["dest_rel"]), f".travelarr-resync-{row['id']}.part")


def _sidecars(row: dict) -> set[str]:
    out: set[str] = set()
    for rel in json.loads(row.get("sidecars") or "[]"):
        out |= {rel, rel + PART_SUFFIX}
    return out


def owned(row: dict) -> set[str]:
    """Paths a row may delete once it gives them up (see `Store.transition`)."""
    st, mode, phase, dest = row["status"], row.get("mode"), row.get("phase"), row["dest_rel"]
    temp = staging_rel(row) if mode == "upgrade" else dest + PART_SUFFIX
    if st == "removed":
        return set()
    if st == "done":
        return {dest} | _sidecars(row)
    if phase == "placed":
        return {dest, temp} | _sidecars(row)
    if phase == "placing":
        # the destination is proven only once placed; a re-queued copy owned it already
        return {temp} | _sidecars(row) | ({dest} if mode in ("redownload", "recover") else set())
    if mode in ("refresh", "redownload", "recover"):  # an existing copy, re-queued
        if st == "cancelled":
            return {dest} | _sidecars(row)
        return {dest, temp} | _sidecars(row)
    if st == "cancelled":
        return set()
    return {temp}


def protected(row: dict) -> set[str]:
    """owned() plus paths no cleanup may delete while this row exists."""
    out = owned(row)
    if row.get("phase") == "placing":
        out.add(row["dest_rel"])
    return out


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def label(row: dict) -> str:
    if row["kind"] == "episode":
        return f"{row['show_title']} S{(row['season'] or 0):02d}E{(row['episode'] or 0):02d} {row['title']}"
    return f"{row['title']} ({row['year']})" if row.get("year") else row["title"]


class Store:
    def __init__(self, path: str):
        self.media_root: str | None = None  # set by the app; hand-offs skip paths not on disk
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(jobs)")}
            for col, decl in NEW_COLUMNS.items():
                if col not in cols:
                    self.db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
            # A restart mid-download resumes from the partial file.
            self.db.execute("UPDATE jobs SET status='queued' WHERE status='downloading'")

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def rows(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def exec(self, sql: str, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    def get(self, key: str, default=None):
        r = self.rows("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(r[0]["value"]) if r else default

    def set(self, key: str, value) -> None:
        if value is None:
            self.exec("DELETE FROM settings WHERE key=?", (key,))
        else:
            self.exec("INSERT INTO settings(key,value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def add_jobs(self, jobs: list[dict]) -> tuple[list[dict], list[dict]]:
        with self.lock:
            self.db.execute("BEGIN")
            try:
                out = self._add_jobs(jobs)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return out

    def requeue_changed(self, job_id: int, job: dict) -> bool:
        """Retry of a job whose file changed on Plex: the same row takes the item's
        current file and goes back to the queue, in one conditional transaction
        (False if the row is no longer failed, or the new file collides with another
        copy). One row, so Retry, Remove and the worker's claim stay mutually exclusive."""
        with self.lock:
            self.db.execute("BEGIN")
            try:
                dup = self.db.execute(
                    "SELECT id FROM jobs WHERE status IN ('queued','downloading','done') AND id != ? "
                    "AND (dest_rel=? OR (part_id=? AND server_id IS ?))",
                    (job_id, job["dest_rel"], job["part_id"], job.get("server_id"))).fetchone()
                ok = dup is None and self._transition(job_id, lambda r: r["status"] == "failed", {
                    "status": "queued", "part_id": job["part_id"], "part_key": job["part_key"], "size": job["size"],
                    "dest_rel": job["dest_rel"], "title": job["title"], "server_id": job.get("server_id"),
                    "guid": job.get("guid"), "bytes_done": 0, "validator": None, "error": None,
                    "finished_at": None, "note": "queued again: the file changed on Plex"}) is not None
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return ok

    def _add_jobs(self, jobs: list[dict]) -> tuple[list[dict], list[dict]]:
        """Inside a transaction."""
        added, skipped = [], []
        for j in jobs:
            replaces = json.loads(j.get("replaces") or "[]")
            ignore = ",".join(str(int(i)) for i in replaces) or "0"
            dup = self.db.execute(
                f"SELECT id FROM jobs WHERE status IN ('queued','downloading','done') AND id NOT IN ({ignore}) "
                "AND (dest_rel=? OR (part_id=? AND server_id IS ?))",
                (j["dest_rel"], j["part_id"], j.get("server_id"))).fetchone()
            if dup is None and j.get("rating_key"):
                # another version of this video under the same name: its subtitles would be shared;
                # Re-sync replaces it instead
                stem = posixpath.splitext(j["dest_rel"])[0]
                dup = next((r for r in self.db.execute(
                    f"SELECT dest_rel FROM jobs WHERE status IN ('queued','downloading','done') "
                    f"AND id NOT IN ({ignore}) AND rating_key=? AND server_id IS ?",
                    (j["rating_key"], j.get("server_id"))) if posixpath.splitext(r["dest_rel"])[0] == stem), None)
            if dup or (replaces and self._replaced_by_other(replaces)):
                skipped.append(j)
                continue
            cols = [f for f in JOB_FIELDS if f in j]
            self.db.execute(
                f"INSERT INTO jobs({','.join(cols)},status,queued_at) "
                f"VALUES({','.join('?' * len(cols))},'queued',?)",
                [j[c] for c in cols] + [now()])
            added.append(j)
        return added, skipped

    def job(self, job_id: int) -> dict | None:
        r = self.rows("SELECT * FROM jobs WHERE id=?", (job_id,))
        return r[0] if r else None

    def _replaced_by_other(self, replaces: list) -> bool:
        """Another unfinished upgrade already replaces one of these rows."""
        want = {int(i) for i in replaces}
        for r in self.db.execute("SELECT replaces FROM jobs WHERE mode='upgrade' "
                                 "AND status IN ('queued','downloading','failed')"):
            if want & {int(i) for i in json.loads(r[0] or "[]")}:
                return True
        return False

    def _locked(self, job_id: int) -> bool:
        """An upgrade that is placing or placed replaces this row (inside a transaction)."""
        for r in self.db.execute("SELECT replaces FROM jobs WHERE mode='upgrade' AND phase IS NOT NULL "
                                 "AND status != 'removed'"):
            if job_id in {int(i) for i in json.loads(r[0] or "[]")}:
                return True
        return False

    def upgrades_of(self, job_id: int) -> list[dict]:
        """Unfinished upgrades that replace this row."""
        return [r for r in self.rows("SELECT * FROM jobs WHERE mode='upgrade' "
                                     "AND status IN ('queued','downloading','failed')")
                if job_id in {int(i) for i in json.loads(r["replaces"] or "[]")}]

    def next_queued(self, server_id: str | None = None, skip: frozenset = frozenset()) -> dict | None:
        """The oldest queued job; with server_id, only jobs of that server. Jobs of
        another server wait."""
        # a job whose file is being placed or is in place finishes first, whatever
        # the selected server: it needs no Plex request to be in a consistent state
        for r in self.rows("SELECT * FROM jobs WHERE status='queued' AND phase IS NOT NULL ORDER BY id LIMIT 200"):
            if r["id"] not in skip:
                return r
        sql, args = "SELECT * FROM jobs WHERE status='queued'", []
        if server_id is not None:
            sql += " AND server_id=?"
            args.append(server_id)
        for r in self.rows(sql + " ORDER BY id LIMIT 200", args):
            if r["id"] not in skip:
                return r
        return None

    # Every way out of 'queued' is one conditional UPDATE, so the worker's claim
    # and a cancel or removal can never both win.
    def claim(self, job_id: int) -> bool:
        """queued -> downloading; False when the job left the queue first."""
        return self.exec("UPDATE jobs SET status='downloading', started_at=?, error=NULL, note=NULL "
                         "WHERE id=? AND status='queued'", (now(), job_id)).rowcount == 1

    def _exists(self, rel: str) -> bool:
        if self.media_root is None:
            return True
        try:
            return os.path.lexists(abs_path(self.media_root, rel))
        except PathError:
            return False

    def _transition(self, job_id: int, when, fields: dict) -> dict | None:
        """Inside a transaction: apply fields if when(row) holds. Whatever the row
        owned before and no longer owns goes to its cleanup list in the same write
        (skipping paths not on disk), so a crash never leaves a file nobody owns."""
        cur = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if cur is None:
            return None
        row = dict(cur)
        if not when(row):
            return None
        after = {**row, **fields}
        mine = owned(after)
        pending = json.loads(row.get("cleanup") or "[]")
        gone = owned(row) - mine
        if gone or any(p in mine for p in pending):
            # a path the row owns again (a retry placed it anew) is no longer to be deleted
            pending = [p for p in pending if p not in mine]
            pending += sorted(p for p in gone if p not in pending and self._exists(p))
            after["cleanup"] = fields["cleanup"] = json.dumps(pending)
        keys = list(fields)
        self.db.execute(f"UPDATE jobs SET {', '.join(k + '=?' for k in keys)} WHERE id=?",
                        [fields[k] for k in keys] + [job_id])
        return after

    def transition(self, job_id: int, when, **fields) -> dict | None:
        with self.lock:
            self.db.execute("BEGIN")
            try:
                out = self._transition(job_id, when, fields)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return out

    @staticmethod
    def _cancel_fields(row: dict) -> dict:
        if row.get("mode") == "refresh":  # the copy itself is fine: back to done
            return {"status": "done", "mode": None, "note": "re-sync cancelled"}
        return {"status": "cancelled", "finished_at": now()}

    def cancel_queued(self, job_id: int) -> bool:
        """queued -> cancelled (a refresh goes back to done). Never a row whose
        file is being placed or is in place."""
        with self.lock:
            row = self.job(job_id)
            if row is None:
                return False
            return self.transition(job_id, lambda r: r["status"] == "queued" and not r["phase"],
                                   **self._cancel_fields(row)) is not None

    def remove(self, job_id: int, statuses=("done", "failed", "cancelled"), note: str | None = None) -> dict | None:
        """-> removed; everything the row owned goes to its cleanup list. Refused
        (None) for a copy an upgrade is placing: checked in the same transaction,
        so it is ordered with the upgrade's T1."""
        fields = {"status": "removed", "removed_at": now(), "phase": None}
        if note is not None:
            fields["note"] = note
        return self.transition(job_id, lambda r: r["status"] in statuses and not self._locked(r["id"]), **fields)

    def requeue_lost(self, job_id: int) -> bool:
        """A done copy whose final rename was lost: check it again, keeping
        ownership of whatever is at its destination."""
        return self.transition(job_id, lambda r: r["status"] == "done" and not self._locked(r["id"]),
                               status="queued", mode="recover",
                               note="copied file was missing; checking it again") is not None

    def mark_removed(self, job_id: int, note: str) -> bool:
        return self.remove(job_id, ("done", "failed"), note) is not None

    def begin_upgrade(self, job_id: int, old_ids: list, mark: str | None) -> bool:
        """T1: the old copy must still be done; then the upgrade is placing (it can
        no longer be cancelled and its old copy is locked). Otherwise the upgrade is
        cancelled, giving up its staging file."""
        with self.lock:
            self.db.execute("BEGIN")
            try:
                olds = [self.db.execute("SELECT status FROM jobs WHERE id=?", (int(o),)).fetchone() for o in old_ids]
                if all(o is not None and o[0] == "done" for o in olds):
                    self._transition(job_id, lambda r: True, {"phase": "placing", "staging_mark": mark})
                    ok = True
                else:
                    self._transition(job_id, lambda r: True, {
                        "status": "cancelled", "finished_at": now(),
                        "note": "the copy it would replace was removed"})
                    ok = False
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return ok

    def finish_upgrade(self, job_id: int, old_ids: list, fields: dict) -> list[int]:
        """T3: the upgrade is done and the copies it replaces are removed, giving
        up their files (what the new copy owns stays protected)."""
        retired = []
        with self.lock:
            self.db.execute("BEGIN")
            try:
                # done: it no longer replaces anything (a later refresh must not reach the old copy)
                self._transition(job_id, lambda r: True, {**fields, "replaces": "[]"})
                for o in old_ids:
                    if self._transition(int(o), lambda r: r["status"] == "done", {
                            "status": "removed", "removed_at": now(), "phase": None,
                            "note": "replaced by a re-sync"}) is not None:
                        retired.append(int(o))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return retired

    def adopt(self, job: dict, sidecars: list[str]) -> int:
        """A file on the NAS that no row knows, recorded as a done copy (with its
        subtitles) so a re-sync can replace it like any other copy."""
        cols = [f for f in JOB_FIELDS if f in job]
        cur = self.exec(
            f"INSERT INTO jobs({','.join(cols)},status,bytes_done,sidecars,note,queued_at,finished_at) "
            f"VALUES({','.join('?' * len(cols))},'done',?,?,?,?,?)",
            [job[c] for c in cols] + [job["size"], json.dumps(sidecars), "found on the NAS by a re-sync", now(), now()])
        return cur.lastrowid

    def add_sidecar(self, job_id: int, rel: str) -> None:
        """Recorded before the file is written, so a written file is never unowned."""
        with self.lock:
            row = self.job(job_id)
            names = json.loads(row["sidecars"] or "[]")
            if rel not in names:
                self.update(job_id, sidecars=json.dumps(names + [rel]))

    def release_paths(self, targets: list[str], note: str, where=lambda rel: rel) -> tuple[dict | None, int]:
        """Before deleting targets: the downloading job they would hit (then
        nothing changes), else cancel the queued jobs whose video they take.
        A job is matched by its dest_rel and by where() it really writes
        (links in its folders resolved). Runs as one transaction, ordered with
        the worker's claim."""
        with self.lock:
            self.db.execute("BEGIN")
            try:
                rows = [dict(r) for r in self.db.execute(
                    "SELECT * FROM jobs WHERE status IN ('queued','downloading') ORDER BY id")]

                def paths(j):
                    out = [j["dest_rel"], where(j["dest_rel"])]
                    if j["mode"] == "upgrade":
                        out.append(staging_rel(j))
                    return out
                hit = {j["id"]: {touches(t, p) for t in targets for p in paths(j)} for j in rows}
                busy = next((j for j in rows if (j["status"] == "downloading" or j["phase"])
                             and hit[j["id"]] - {None}), None)
                for j in rows:  # a copy being replaced right now is as busy as its upgrade
                    if busy is None and j["mode"] == "upgrade" and j["phase"]:
                        for o in json.loads(j["replaces"] or "[]"):
                            old = self.db.execute("SELECT * FROM jobs WHERE id=?", (int(o),)).fetchone()
                            if old and any(touches(t, p) for t in targets
                                           for p in [old["dest_rel"], where(old["dest_rel"])]
                                           + json.loads(old["sidecars"] or "[]")):
                                busy = j
                n = 0
                if busy is None:
                    for j in rows:
                        if j["status"] == "queued" and "video" in hit[j["id"]]:
                            n += self._transition(j["id"], lambda r: r["status"] == "queued" and not r["phase"],
                                                  {**self._cancel_fields(j), "note": note}) is not None
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return busy, n

    def update(self, job_id: int, **fields) -> None:
        if not fields:
            return
        keys = list(fields)
        self.exec(f"UPDATE jobs SET {', '.join(k + '=?' for k in keys)} WHERE id=?",
                  [fields[k] for k in keys] + [job_id])

    def jobs(self, view: str = "active", q: str | None = None,
             limit: int = 200, offset: int = 0) -> list[dict]:
        where, args = [], []
        if view == "active":
            where.append("status IN ('queued','downloading','failed')")
            order = "CASE status WHEN 'downloading' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, id"
        else:
            order = "COALESCE(removed_at, finished_at, queued_at) DESC, id DESC"
            if view != "all":
                where.append("status IN ('done','removed','failed','cancelled')")
        if q:
            where.append("(title LIKE ? OR show_title LIKE ? OR dest_rel LIKE ?)")
            args += [f"%{q}%"] * 3
        sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "")
        sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        return self.rows(sql, args + [limit, offset])

    def jobs_for(self, rating_key: str | None = None, show_key: str | None = None,
                 server_id: str | None = None) -> list[dict]:
        """Rows of an item; with server_id only that server's (rating keys belong to one server)."""
        col, key = ("show_key", show_key) if show_key else ("rating_key", rating_key)
        return self.rows(f"SELECT * FROM jobs WHERE {col}=? AND (? IS NULL OR server_id=?) "
                         "ORDER BY id", (key, server_id, server_id))

    def status_map(self, rating_keys: list[str], server_id: str | None = None) -> dict[str, str]:
        """Best current status per item (downloading > queued > done > failed...),
        counting rows of server_id."""
        if not rating_keys:
            return {}
        marks = ",".join("?" * len(rating_keys))
        out: dict[str, str] = {}
        for r in self.rows(f"SELECT rating_key, status FROM jobs WHERE rating_key IN ({marks}) "
                           "AND (? IS NULL OR server_id=?)",
                           list(rating_keys) + [server_id, server_id]):
            cur = out.get(r["rating_key"])
            if cur is None or RANK[r["status"]] > RANK[cur]:
                out[r["rating_key"]] = r["status"]
        return {k: v for k, v in out.items() if v not in ("removed", "cancelled")}

    def show_counts(self, show_keys: list[str], server_id: str | None = None) -> dict[str, dict]:
        if not show_keys:
            return {}
        marks = ",".join("?" * len(show_keys))
        out: dict[str, dict] = {}
        for r in self.rows(
                f"SELECT show_key, status, COUNT(DISTINCT rating_key) n FROM jobs "
                f"WHERE show_key IN ({marks}) AND status IN ('queued','downloading','done','failed') "
                f"AND (? IS NULL OR server_id=?) "
                f"GROUP BY show_key, status", list(show_keys) + [server_id, server_id]):
            d = out.setdefault(r["show_key"], {"done": 0, "active": 0, "failed": 0})
            key = {"done": "done", "failed": "failed"}.get(r["status"], "active")
            d[key] += r["n"]
        return out

    def totals(self) -> dict:
        r = self.rows("SELECT status, COUNT(*) n, COALESCE(SUM(size),0) b FROM jobs GROUP BY status")
        return {x["status"]: {"count": x["n"], "bytes": x["b"]} for x in r}

    def pending_bytes(self) -> int:
        r = self.rows("SELECT COALESCE(SUM(size - bytes_done),0) b FROM jobs "
                      "WHERE status IN ('queued','downloading') AND COALESCE(mode,'') != 'refresh'")
        return int(r[0]["b"])
