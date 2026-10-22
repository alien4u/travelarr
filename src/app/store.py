"""SQLite store: settings and one row per copied file (the sync history)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time

from .paths import touches

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
"""

PRESENT = ("queued", "downloading", "done")
RANK = {"downloading": 5, "queued": 4, "done": 3, "failed": 2, "cancelled": 1, "removed": 0}

JOB_FIELDS = ("rating_key", "show_key", "kind", "title", "show_title", "season", "episode",
              "year", "part_id", "part_key", "size", "dest_rel", "source")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Store:
    def __init__(self, path: str):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(jobs)")}
            if "validator" not in cols:  # databases from before resume validation
                self.db.execute("ALTER TABLE jobs ADD COLUMN validator TEXT")
            # A restart mid-download resumes from the partial file.
            self.db.execute("UPDATE jobs SET status='queued' WHERE status='downloading'")

    def close(self) -> None:
        with self.lock:
            self.db.close()

    # -- low level -------------------------------------------------------------
    def rows(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def exec(self, sql: str, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    # -- settings --------------------------------------------------------------
    def get(self, key: str, default=None):
        r = self.rows("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(r[0]["value"]) if r else default

    def set(self, key: str, value) -> None:
        if value is None:
            self.exec("DELETE FROM settings WHERE key=?", (key,))
        else:
            self.exec("INSERT INTO settings(key,value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    # -- jobs ------------------------------------------------------------------
    def add_jobs(self, jobs: list[dict]) -> tuple[list[dict], list[dict]]:
        added, skipped = [], []
        with self.lock:
            self.db.execute("BEGIN")
            try:
                for j in jobs:
                    dup = self.db.execute(
                        "SELECT id FROM jobs WHERE (part_id=? OR dest_rel=?) AND status IN "
                        "('queued','downloading','done')", (j["part_id"], j["dest_rel"])).fetchone()
                    if dup:
                        skipped.append(j)
                        continue
                    cols = [f for f in JOB_FIELDS if f in j]
                    self.db.execute(
                        f"INSERT INTO jobs({','.join(cols)},status,queued_at) "
                        f"VALUES({','.join('?' * len(cols))},'queued',?)",
                        [j[c] for c in cols] + [now()])
                    added.append(j)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return added, skipped

    def job(self, job_id: int) -> dict | None:
        r = self.rows("SELECT * FROM jobs WHERE id=?", (job_id,))
        return r[0] if r else None

    def next_queued(self) -> dict | None:
        r = self.rows("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1")
        return r[0] if r else None

    # Every way out of 'queued' is one conditional UPDATE, so the worker's claim
    # and a cancel can never both win.
    def claim(self, job_id: int) -> bool:
        """queued -> downloading; False when the job left the queue first."""
        return self.exec("UPDATE jobs SET status='downloading', started_at=?, error=NULL, note=NULL "
                         "WHERE id=? AND status='queued'", (now(), job_id)).rowcount == 1

    def cancel_queued(self, job_id: int) -> bool:
        return self.exec("UPDATE jobs SET status='cancelled', finished_at=? WHERE id=? AND status='queued'",
                         (now(), job_id)).rowcount == 1

    def mark_removed(self, job_id: int, note: str) -> bool:
        return self.exec("UPDATE jobs SET status='removed', removed_at=?, note=? "
                         "WHERE id=? AND status IN ('done','failed')", (now(), note, job_id)).rowcount == 1

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
                hit = {j["id"]: {touches(t, p) for t in targets for p in (j["dest_rel"], where(j["dest_rel"]))}
                       for j in rows}
                busy = next((j for j in rows if j["status"] == "downloading" and hit[j["id"]] - {None}), None)
                n = 0
                if busy is None:
                    for j in rows:
                        if j["status"] == "queued" and "video" in hit[j["id"]]:
                            n += self.db.execute(
                                "UPDATE jobs SET status='cancelled', finished_at=?, note=? "
                                "WHERE id=? AND status='queued'", (now(), note, j["id"])).rowcount
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

    def jobs_for(self, rating_key: str | None = None, show_key: str | None = None) -> list[dict]:
        if show_key:
            return self.rows("SELECT * FROM jobs WHERE show_key=? ORDER BY id", (show_key,))
        return self.rows("SELECT * FROM jobs WHERE rating_key=? ORDER BY id", (rating_key,))

    def status_map(self, rating_keys: list[str]) -> dict[str, str]:
        """Best current status per item (downloading > queued > done > failed...)."""
        if not rating_keys:
            return {}
        marks = ",".join("?" * len(rating_keys))
        out: dict[str, str] = {}
        for r in self.rows(f"SELECT rating_key, status FROM jobs WHERE rating_key IN ({marks})",
                           list(rating_keys)):
            cur = out.get(r["rating_key"])
            if cur is None or RANK[r["status"]] > RANK[cur]:
                out[r["rating_key"]] = r["status"]
        return {k: v for k, v in out.items() if v not in ("removed", "cancelled")}

    def show_counts(self, show_keys: list[str]) -> dict[str, dict]:
        if not show_keys:
            return {}
        marks = ",".join("?" * len(show_keys))
        out: dict[str, dict] = {}
        for r in self.rows(
                f"SELECT show_key, status, COUNT(DISTINCT rating_key) n FROM jobs "
                f"WHERE show_key IN ({marks}) AND status IN ('queued','downloading','done','failed') "
                f"GROUP BY show_key, status", list(show_keys)):
            d = out.setdefault(r["show_key"], {"done": 0, "active": 0, "failed": 0})
            key = {"done": "done", "failed": "failed"}.get(r["status"], "active")
            d[key] += r["n"]
        return out

    def totals(self) -> dict:
        r = self.rows("SELECT status, COUNT(*) n, COALESCE(SUM(size),0) b FROM jobs GROUP BY status")
        return {x["status"]: {"count": x["n"], "bytes": x["b"]} for x in r}

    def pending_bytes(self) -> int:
        r = self.rows("SELECT COALESCE(SUM(size - bytes_done),0) b FROM jobs "
                      "WHERE status IN ('queued','downloading')")
        return int(r[0]["b"])
