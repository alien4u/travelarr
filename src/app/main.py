"""travelarr web app: pick titles from your home Plex and copy the original
files (plus subtitles) to this box over HTTPS."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
from contextlib import asynccontextmanager
from typing import Any

import requests
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from plexapi.exceptions import NotFound, Unauthorized

from .paths import (PART_SUFFIX, ListingChanged, PathError, abs_path, delete_entry, list_dir, local_entry,
                    real_rel, remove_files, touches)
from .plexgw import NotReady, PlexGateway
from .store import Store, now
from .worker import CHANGED, Downloader

VERSION = "2.4.0"
STATIC = os.path.join(os.path.dirname(__file__), "static")
LOCAL_NOTE = "deleted in Local Files"
MAX_QUEUE_KEYS = 200  # bounds the Plex requests one queue call makes
USERNAME_RE = re.compile(r"[A-Za-z0-9._@-]{1,64}")  # no ':' (impossible in Basic auth), safe to log
log = logging.getLogger("travelarr")


def ui_username(value: str | None = None) -> str:
    """The web UI user name: value, else TRAVELARR_USERNAME; empty means admin."""
    if value is None:
        value = os.environ.get("TRAVELARR_USERNAME")
    return (value or "").strip() or "admin"


def device_name() -> str:
    """What the UI calls this box: TRAVELARR_NAME, else the Unraid server name
    (Unraid passes HOST_HOSTNAME to every container it creates), else the host
    name. Docker's default host name is the container id, which is not a name."""
    for var in ("TRAVELARR_NAME", "HOST_HOSTNAME"):
        name = (os.environ.get(var) or "").strip()
        if name:
            return name
    name = socket.gethostname().split(".")[0]
    if re.fullmatch(r"[0-9a-f]{12}", name):
        name = ""
    return name or "NAS"


def create_app(data_dir: str | None = None, media_root: str | None = None,
               password: str | None = None, gateway_factory=None,
               start_worker: bool = True, device: str | None = None,
               username: str | None = None) -> FastAPI:
    data_dir = data_dir or os.environ.get("TRAVELARR_DATA", "/config")
    media_root = media_root or os.environ.get("TRAVELARR_MEDIA", "/media")
    if password is None:
        password = os.environ.get("TRAVELARR_PASSWORD") or None
    username = ui_username(username)
    if password and not USERNAME_RE.fullmatch(username):
        raise ValueError("TRAVELARR_USERNAME must be 1 to 64 characters: letters, digits, '.', '_', '@' or '-'")
    device = device or device_name()
    os.makedirs(data_dir, exist_ok=True)
    thumbs = os.path.join(data_dir, "cache", "thumbs")
    os.makedirs(thumbs, exist_ok=True)

    store = Store(os.path.join(data_dir, "travelarr.db"))
    gw = (gateway_factory or (lambda s: PlexGateway(s, VERSION)))(store)
    dl = Downloader(store, gw, media_root, device)

    @asynccontextmanager
    async def lifespan(_app):
        if not os.path.isdir(media_root):
            log.error("media folder %s does not exist; mount the media share there", media_root)
        if start_worker:
            dl.start()
        yield
        dl.stop()
        if dl._thread is None:
            store.close()

    app = FastAPI(title="travelarr", version=VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.gw, app.state.dl = store, gw, dl

    # ------------------------------------------------------------- security
    if password:
        want_user, want_pw = username.encode(), password.encode()

        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            ok = False
            if scheme.lower() == "basic":
                try:
                    user, sep, pw = base64.b64decode(token.strip(), validate=True).decode().partition(":")
                    # Both comparisons always run, so timing does not tell which one failed.
                    user_ok = secrets.compare_digest(user.encode(), want_user)
                    pw_ok = secrets.compare_digest(pw.encode(), want_pw)
                    ok = bool(sep) and user_ok and pw_ok
                except Exception:  # noqa: BLE001
                    ok = False
            if not ok:
                return Response("Authentication required", status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="travelarr", charset="UTF-8"'})
            return await call_next(request)

    # --------------------------------------------------------------- errors
    def err(status: int, msg: str, code: str = "error") -> JSONResponse:
        return JSONResponse({"error": msg, "code": code}, status_code=status)

    @app.exception_handler(NotReady)
    async def _nr(_r, e):
        return err(409, str(e), "not_ready")

    @app.exception_handler(Unauthorized)
    async def _ua(_r, e):
        return err(409, "Plex rejected the sign-in; sign in again", "auth")

    @app.exception_handler(NotFound)
    async def _nf(_r, e):
        return err(404, f"Not found on the Plex server: {e}")

    @app.exception_handler(PathError)
    async def _pe(_r, e):
        return err(400, str(e))

    @app.exception_handler(requests.RequestException)
    async def _rq(_r, e):
        gw.invalidate()
        return err(502, f"Plex did not answer: {e}", "network")

    # -------------------------------------------------------------- helpers
    def disk() -> dict | None:
        if not os.path.isdir(media_root):
            return None
        u = shutil.disk_usage(media_root)
        return {"total": u.total, "free": u.free, "used": u.used}

    def annotate(items: list[dict]) -> list[dict]:
        movies = [i["ratingKey"] for i in items if i.get("ratingKey") and i["type"] in ("movie", "episode")]
        shows = [i["ratingKey"] for i in items if i.get("ratingKey") and i["type"] == "show"]
        sm, sc = store.status_map(movies), store.show_counts(shows)
        for i in items:
            if i.get("type") in ("movie", "episode"):
                i["copy"] = sm.get(i.get("ratingKey"))
            elif i.get("type") == "show":
                i["copy"] = sc.get(i.get("ratingKey"))
        return items

    def remove_job(job: dict) -> str:
        if job["status"] == "queued":
            if store.cancel_queued(job["id"]):
                return "cancelled"
            job = store.job(job["id"])  # the worker claimed it meanwhile
        st = job["status"]
        if st == "downloading":
            dl.cancel(job["id"])  # best-effort: a file past its last check finishes
            return "cancelling"
        if st == "removed":
            return "removed"
        rels = [job["dest_rel"], job["dest_rel"] + PART_SUFFIX] + json.loads(job["sidecars"] or "[]")
        remove_files(media_root, rels)
        store.update(job["id"], status="removed", removed_at=now())
        return "removed"

    # ---------------------------------------------------------------- state
    @app.get("/api/state")
    def state():
        return {
            "version": VERSION,
            "device": device,
            "signedIn": bool(gw.token()),
            "user": store.get("username"),
            "serverId": store.get("server_id"),
            "serverName": store.get("server_name"),
            "preferredUrl": store.get("preferred_url"),
            "allowRelay": bool(store.get("allow_relay", False)),
            "conn": gw.conn,
            "connError": gw._fail[1] if getattr(gw, "_fail", None) else None,
            "paused": bool(store.get("paused", False)),
            "reserveGb": store.get("reserve_gb", 10),
            "mediaRoot": media_root,
            "disk": disk(),
            "current": dl.snapshot(),
            "workerError": dl.last_error,
            "totals": store.totals(),
            "pendingBytes": store.pending_bytes(),
        }

    # ----------------------------------------------------------------- auth
    @app.post("/api/auth/start")
    def auth_start():
        return {"url": gw.start_login()}

    @app.post("/api/auth/check")
    def auth_check():
        return {"signedIn": gw.check_login()}

    @app.post("/api/auth/signout")
    def auth_signout():
        gw.sign_out()
        return {"ok": True}

    # --------------------------------------------------------------- server
    @app.get("/api/servers")
    def servers():
        return {"servers": gw.servers()}

    @app.post("/api/server")
    def choose_server(body: dict = Body(...)):
        gw.select_server(body["id"], body.get("name"), body.get("preferredUrl"),
                         bool(body.get("allowRelay")))
        gw.server()
        return {"conn": gw.conn}

    @app.post("/api/connect")
    def connect():
        gw.reset()
        gw.server()
        return {"conn": gw.conn}

    @app.post("/api/settings")
    def settings(body: dict = Body(...)):
        if "paused" in body:
            store.set("paused", bool(body["paused"]))
            dl.wake()
        if "reserveGb" in body:
            store.set("reserve_gb", max(0.0, float(body["reserveGb"])))
        return state()

    # --------------------------------------------------------------- browse
    @app.get("/api/libraries")
    def libraries():
        return {"libraries": gw.libraries()}

    @app.get("/api/library/{key}")
    def library(key: str, q: str | None = None, start: int = 0, size: int = 60, sort: str = "title"):
        if sort not in ("title", "recent"):
            return err(400, "sort must be title or recent", "bad_request")
        d = gw.list_items(key, q, max(0, start), min(max(1, size), 200), sort)
        annotate(d["items"])
        return d

    @app.get("/api/watchlist")
    def watchlist():
        return {"items": annotate(gw.watchlist())}

    @app.get("/api/item/{rk}")
    def item(rk: str):
        d = gw.item(rk)
        if d["type"] == "show":
            eps = [e for s in d["seasons"] for e in s["episodes"]]
            sm = store.status_map([e["ratingKey"] for e in eps])
            for e in eps:
                e["copy"] = sm.get(e["ratingKey"])
            d["copy"] = store.show_counts([rk]).get(rk)
        else:
            d["copy"] = store.status_map([rk]).get(rk)
        jobs = store.jobs_for(show_key=rk) if d["type"] == "show" else store.jobs_for(rating_key=rk)
        d["history"] = jobs[-50:]
        return d

    # ---------------------------------------------------------------- queue
    @app.post("/api/queue")
    def queue(body: dict = Body(...)):
        keys = [str(k) for k in body.get("keys", []) if k]
        if not keys:
            return err(400, "nothing selected")
        if len(keys) > MAX_QUEUE_KEYS:
            return err(400, f"Too many titles at once (at most {MAX_QUEUE_KEYS})")
        mode = body.get("mode", "unwatched")
        only = {str(k) for k in body.get("only") or []} or None
        jobs = gw.plan(keys, mode=mode, source=body.get("source", "browse"), only=only)
        added, skipped = store.add_jobs(jobs)
        dl.wake()
        d = disk()
        need = store.pending_bytes()
        warn = None
        reserve = dl.reserve_bytes()
        if d and need > d["free"] - reserve:
            warn = f"The queue is larger than the free space on {device}; some downloads will fail."
        if jobs and not added:
            warn = warn or f"Everything selected is already on {device} or in the queue."
        return {"added": len(added), "skipped": len(skipped),
                "bytes": sum(j["size"] for j in added), "warning": warn}

    @app.get("/api/jobs")
    def jobs(view: str = "active", q: str | None = None, limit: int = 200, offset: int = 0):
        return {"jobs": store.jobs(view, q, min(limit, 1000), offset), "current": dl.snapshot(),
                "paused": bool(store.get("paused", False)), "workerError": dl.last_error}

    def _job(job_id: int) -> dict:
        j = store.job(job_id)
        if j is None:
            raise HTTPException(404, f"no job {job_id}")
        return j

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: int):
        _job(job_id)
        if not store.cancel_queued(job_id) and store.job(job_id)["status"] == "downloading":
            dl.cancel(job_id)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/retry")
    def retry(job_id: int):
        j = _job(job_id)
        if j["status"] == "failed" and (j["error"] or "").startswith(CHANGED):
            # the old file is gone from Plex: queue the item again as it is now
            added, _ = store.add_jobs(gw.plan([j["rating_key"]], mode="all", source=j["source"] or "browse"))
            store.update(job_id, status="cancelled", note="queued again", finished_at=now())
            dl.wake()
            return {"ok": True, "requeued": len(added)}
        if j["status"] in ("failed", "cancelled"):
            store.update(job_id, status="queued", error=None, note=None, finished_at=None)
            dl.wake()
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/remove")
    def remove(job_id: int):
        return {"result": remove_job(_job(job_id))}

    @app.post("/api/remove")
    def remove_many(body: dict = Body(...)):
        results: dict[str, int] = {}
        targets = []
        for rk in body.get("ratingKeys", []):
            targets += store.jobs_for(rating_key=str(rk))
        for sk in body.get("showKeys", []):
            targets += store.jobs_for(show_key=str(sk))
        for j in {j["id"]: j for j in targets}.values():
            r = remove_job(j)
            results[r] = results.get(r, 0) + 1
        return {"results": results}

    # ---------------------------------------------------------- local files
    @app.get("/api/files")
    def files(path: str = "", offset: str = "0", limit: str = "200", token: str | None = None):
        if not (re.fullmatch(r"[0-9]{1,9}", offset) and re.fullmatch(r"[0-9]{1,3}", limit) and 1 <= int(limit) <= 500):
            return err(400, "offset and limit must be whole numbers, limit 1 to 500", "bad_request")
        if not os.path.isdir(media_root):
            return err(409, "The media folder is missing.", "no_media")
        try:
            d = list_dir(media_root, path, int(offset), int(limit), token)
        except ListingChanged:
            return err(409, "The folder changed since the last page.", "changed")
        except FileNotFoundError:
            return err(404, f"{path or 'The media folder'} no longer exists.", "missing")
        except NotADirectoryError:
            return err(400, f"{path} is not a folder.", "not_folder")
        except PermissionError:
            return err(403, f"{path or 'The media folder'} cannot be read.", "unreadable")
        d["disk"] = disk()
        return d

    @app.post("/api/files/delete")
    def files_delete(body: Any = Body(None)):
        given = body.get("paths") if isinstance(body, dict) else None
        if not (isinstance(given, list) and 1 <= len(given) <= 500
                and all(isinstance(p, str) and p for p in given)):
            return err(400, "paths must be a list of 1 to 500 file or folder paths", "bad_request")
        targets = set()
        for p in given:
            rel, full = local_entry(media_root, p)
            if not os.path.lexists(full):
                return err(404, f"{rel} no longer exists.", "missing")
            targets.add(rel)
        kept: list[str] = []
        for t in sorted(targets, key=lambda t: t.count("/")):
            if not any(t.startswith(k + "/") for k in kept):
                kept.append(t)  # a path inside another selected one goes with it
        where = lambda rel: real_rel(media_root, rel)  # noqa: E731
        busy, cancelled = store.release_paths(kept, LOCAL_NOTE, where)
        if busy:
            return err(409, f"{dl._label(busy)} is downloading into this folder. Cancel it in the Queue first.",
                       "busy")
        out = {"deleted": 0, "bytes": 0, "files": 0, "cancelled": cancelled, "removed": 0, "errors": []}
        for t in kept:
            try:
                size, n = delete_entry(media_root, t)
            except OSError as e:
                out["errors"].append({"path": t, "error": e.strerror or str(e)})
                continue
            out["deleted"] += 1
            out["bytes"] += size
            out["files"] += n
        for j in store.rows("SELECT id, dest_rel FROM jobs WHERE status IN ('done','failed')"):
            if not any(touches(t, p) == "video" for t in kept for p in (j["dest_rel"], where(j["dest_rel"]))):
                continue
            try:
                gone = not os.path.lexists(abs_path(media_root, j["dest_rel"]))
            except PathError:
                continue
            if gone and store.mark_removed(j["id"], LOCAL_NOTE):
                out["removed"] += 1
        return out

    # --------------------------------------------------------------- images
    @app.get("/api/thumb")
    def thumb(path: str, w: int = 240, h: int = 360):
        if not path.startswith("/library/"):
            return err(400, "bad image path")
        w, h = min(max(w, 40), 800), min(max(h, 40), 1200)
        name = hashlib.sha1(f"{store.get('server_id')}|{path}|{w}x{h}".encode()).hexdigest()
        cached = os.path.join(thumbs, name + ".jpg")
        hdrs = {"Cache-Control": "max-age=604800"}
        if os.path.isfile(cached):
            return FileResponse(cached, media_type="image/jpeg", headers=hdrs)
        data, ctype = gw.thumb(path, w, h)
        with open(cached + ".tmp", "wb") as fh:
            fh.write(data)
        os.replace(cached + ".tmp", cached)
        return Response(data, media_type=ctype, headers=hdrs)

    # --------------------------------------------------------------- static
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(os.path.join(STATIC, "index.html"),
                            headers={"Cache-Control": "no-cache"})

    if password:
        log.info("web UI sign-in: user name %s", username)
    else:
        log.warning("TRAVELARR_PASSWORD is not set: the web UI has no password")
    return app
