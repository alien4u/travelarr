"""travelarr web app: pick titles from your home Plex and copy the original
files (plus subtitles) to this box."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import socket
from contextlib import asynccontextmanager
from typing import Any

import requests
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from plexapi.exceptions import NotFound, Unauthorized

from . import auth as auth_mod
from . import resync
from .paths import (ListingChanged, PathError, abs_path, delete_entry, list_dir, local_entry, real_rel,
                    touches)
from .plexgw import VERSION_PREFS, NotReady, OtherServer, PlexGateway
from .resync import ResyncError
from .store import Store, label, now
from .worker import CHANGED, Downloader

VERSION = "3.5.1"
STATIC = os.path.join(os.path.dirname(__file__), "static")
LOCAL_NOTE = "deleted in Local Files"
MAX_QUEUE_KEYS = 200  # bounds the Plex requests one queue call makes
log = logging.getLogger("travelarr")


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
               gateway_factory=None, start_worker: bool = True, device: str | None = None,
               env=None) -> FastAPI:
    data_dir = data_dir or os.environ.get("TRAVELARR_DATA", "/config")
    media_root = media_root or os.environ.get("TRAVELARR_MEDIA", "/media")
    device = device or device_name()
    os.makedirs(data_dir, exist_ok=True)
    thumbs = os.path.join(data_dir, "cache", "thumbs")
    os.makedirs(thumbs, exist_ok=True)

    store = Store(os.path.join(data_dir, "travelarr.db"))
    store.media_root = media_root
    auth = auth_mod.Auth(store)
    auth.startup(os.environ if env is None else env)
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

    @app.exception_handler(ResyncError)
    async def _re(_r, e):
        return err(e.status, str(e), "resync")

    @app.exception_handler(requests.RequestException)
    async def _rq(_r, e):
        gw.invalidate()
        return err(502, f"Plex did not answer: {e}", "network")

    def disk() -> dict | None:
        if not os.path.isdir(media_root):
            return None
        u = shutil.disk_usage(media_root)
        return {"total": u.total, "free": u.free, "used": u.used}

    def annotate(items: list[dict]) -> list[dict]:
        movies = [i["ratingKey"] for i in items if i.get("ratingKey") and i["type"] in ("movie", "episode")]
        shows = [i["ratingKey"] for i in items if i.get("ratingKey") and i["type"] == "show"]
        sid = store.get("server_id")
        sm, sc = store.status_map(movies, sid), store.show_counts(shows, sid)
        for i in items:
            if i.get("type") in ("movie", "episode"):
                i["copy"] = sm.get(i.get("ratingKey"))
            elif i.get("type") == "show":
                i["copy"] = sc.get(i.get("ratingKey"))
        return items

    def remove_job(job: dict) -> str:
        """Removal gives up everything the row owns (deleted through its cleanup
        list). A row whose file is being placed, or a copy an upgrade is replacing
        right now, is busy."""
        jid = job["id"]
        upgrades = store.upgrades_of(jid)
        if any(u["phase"] for u in upgrades):
            return "busy"
        job = store.job(jid)
        if job["phase"]:
            if job["phase"] == "placed" and job["mode"] != "upgrade" and store.remove(jid, ("queued",)):
                return "removed" if not dl.cleanup(jid) else "deferred"
            return "busy"
        if job["status"] == "queued":
            if job["mode"] in ("redownload", "recover"):
                # an existing copy waiting to be copied again: removal deletes it, not just the job
                if store.remove(jid, ("queued",)):
                    return "removed" if not dl.cleanup(jid) else "deferred"
            elif job["mode"] == "refresh":
                store.cancel_queued(jid)  # back to done, then removed below
            elif store.cancel_queued(jid):
                dl.cleanup(jid)
                return "cancelled"
            job = store.job(jid)  # the worker claimed it meanwhile
        st = job["status"]
        if st == "downloading":
            dl.cancel(jid)  # best-effort: a file past its last check finishes
            return "cancelling"
        if st == "removed":
            return "removed"
        for u in upgrades:  # the removal wins over an upgrade that has not started placing
            if u["status"] == "queued" and store.cancel_queued(u["id"]):
                dl.cleanup(u["id"])
            elif u["status"] == "failed" and store.remove(u["id"], ("failed",)):
                dl.cleanup(u["id"])
            elif u["status"] == "downloading":
                dl.cancel(u["id"])
        if store.remove(jid) is None:
            return "busy"
        return "removed" if not dl.cleanup(jid) else "deferred"

    # Open while sign-in is off. When it is on, every /api/ path but these needs a session.
    public = {"/api/session", "/api/login", "/api/logout", "/api/security"}

    def set_session_cookie(resp: Response, token: str, request: Request) -> None:
        resp.set_cookie(auth_mod.COOKIE, token, max_age=auth_mod.SESSION, path="/", httponly=True,
                        samesite="strict", secure=request.url.scheme == "https")

    @app.middleware("http")
    async def own_page_only(request: Request, call_next):
        """Changes need a header another site cannot send without a CORS preflight,
        which travelarr never answers."""
        if (request.method != "GET" and request.url.path.startswith("/api/")
                and request.headers.get("x-requested-with") != "travelarr"):
            return err(403, "Reload the page: it is out of date", "reload")
        return await call_next(request)

    @app.middleware("http")
    async def require_session(request: Request, call_next):
        request.state.session = None
        renewed = False
        if request.url.path.startswith("/api/") and auth.enabled():
            token = request.cookies.get(auth_mod.COOKIE)
            ok = auth.session(token)
            if not ok and request.url.path not in public:
                return err(401, "Sign in to travelarr", "signin")
            request.state.session = token if ok else None
            renewed = ok == "renewed"
        response = await call_next(request)
        if renewed:
            set_session_cookie(response, request.state.session, request)
        return response

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def locked(wait: int) -> JSONResponse:
        r = err(429, f"Too many wrong passwords; try again in {math.ceil(wait / 60)} min", "throttled")
        r.headers["Retry-After"] = str(wait)
        return r

    def password_ok(request: Request, user, password) -> JSONResponse | None:
        """None when right; otherwise the response to send (wrong, or locked out)."""
        ip = client_ip(request)
        wait = auth.locked_for(ip)
        if wait:
            return locked(wait)
        if auth.check(user, password):
            auth.succeeded(ip)
            return None
        auth.failed(ip)
        log.warning("wrong web UI password from %s", ip)
        return err(401 if request.url.path == "/api/login" else 403, "Wrong user name or password", "wrong")

    @app.get("/api/session")
    def session(request: Request):
        signed = auth.enabled() and request.state.session is not None
        return {"authRequired": auth.enabled(), "signedIn": signed, "user": auth.user() if signed else None}

    @app.post("/api/login")
    def login(request: Request, body: dict = Body(...)):
        if not auth.enabled():
            return err(409, "Sign-in is off", "signin_off")
        bad = password_ok(request, body.get("user"), body.get("password"))
        if bad:
            return bad
        resp = JSONResponse({"user": auth.user()})
        set_session_cookie(resp, auth.new_session(), request)
        return resp

    @app.post("/api/logout")
    def logout(request: Request):
        token = request.cookies.get(auth_mod.COOKIE)
        if token:
            auth.end_session(token)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(auth_mod.COOKIE, path="/")
        return resp

    @app.post("/api/security")
    def security(request: Request, body: dict = Body(...)):
        action = body.get("action")
        if action == "enable":
            if auth.enabled():
                return err(409, "Sign-in is already on", "signin_on")
            why = auth_mod.problem(body.get("user"), body.get("password"), body.get("repeat"))
            if why:
                return err(400, why, "invalid")
            auth.enable(body["user"], body["password"])
            log.info("web UI sign-in turned on for %s", body["user"])
            resp = JSONResponse({"user": body["user"]})
            set_session_cookie(resp, auth.new_session(), request)
            return resp
        if action not in ("change", "disable"):
            return err(400, "Unknown action", "bad_request")
        if not auth.enabled():
            return err(409, "Sign-in is off", "signin_off")
        if request.state.session is None:
            return err(401, "Sign in to travelarr", "signin")
        if action == "change":
            why = auth_mod.problem(auth.user(), body.get("password"), body.get("repeat"))
            if why:
                return err(400, why, "invalid")
        bad = password_ok(request, auth.user(), body.get("current"))
        if bad:
            return bad
        if action == "change":
            auth.change(body["password"], keep=request.state.session)
            log.info("web UI password changed")
            return {"ok": True}
        auth.disable()
        log.warning("web UI sign-in turned off")
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(auth_mod.COOKIE, path="/")
        return resp

    @app.get("/api/state")
    def state():
        return {
            "version": VERSION,
            "device": device,
            "signedIn": bool(gw.token()),
            "user": store.get("username"),
            "authEnabled": auth.enabled(),
            "authUser": auth.user() if auth.enabled() else None,
            "serverId": store.get("server_id"),
            "serverName": store.get("server_name"),
            "preferredUrl": store.get("preferred_url"),
            "allowRelay": bool(store.get("allow_relay", False)),
            "conn": gw.conn,
            "connError": gw._fail[1] if getattr(gw, "_fail", None) else None,
            "paused": bool(store.get("paused", False)),
            "reserveGb": store.get("reserve_gb", 10),
            "mediaVersion": gw.version_pref(),
            "mediaRoot": media_root,
            "disk": disk(),
            "current": dl.snapshot(),
            "workerError": dl.last_error,
            "totals": store.totals(),
            "pendingBytes": store.pending_bytes(),
        }

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

    @app.get("/api/servers")
    def servers():
        return {"servers": gw.servers()}

    @app.post("/api/server")
    def choose_server(body: dict = Body(...)):
        with resync.LOCK:  # never in the middle of a re-sync check
            gw.select_server(body["id"], body.get("name"), body.get("preferredUrl"),
                             bool(body.get("allowRelay")))
        dl.wake()  # jobs waiting for this server can run again
        gw.server()
        return {"conn": gw.conn}

    @app.post("/api/connect")
    def connect():
        gw.reset()
        gw.server()
        return {"conn": gw.conn}

    @app.post("/api/settings")
    def settings(body: dict = Body(...)):
        if "reserveGb" in body:
            try:
                gb = float(body["reserveGb"])
            except (TypeError, ValueError):
                gb = math.nan
            if not math.isfinite(gb):
                return err(400, "The free space to keep must be a number of GB", "bad_request")
            store.set("reserve_gb", max(0.0, gb))
        if "mediaVersion" in body:
            if body["mediaVersion"] not in VERSION_PREFS:
                return err(400, f"mediaVersion must be one of {', '.join(VERSION_PREFS)}", "bad_request")
            store.set("media_version", body["mediaVersion"])
        if "paused" in body:
            store.set("paused", bool(body["paused"]))
            dl.wake()
        return state()

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
    def item(rk: str, mediaVersion: str | None = None):  # noqa: N803 - the name the page sends
        if mediaVersion is not None and mediaVersion not in VERSION_PREFS:
            return err(400, f"mediaVersion must be one of {', '.join(VERSION_PREFS)}", "bad_request")
        d = gw.item(rk, mediaVersion)
        if d["type"] == "show":
            eps = [e for s in d["seasons"] for e in s["episodes"]]
            sid = store.get("server_id")
            sm = store.status_map([e["ratingKey"] for e in eps], sid)
            for e in eps:
                e["copy"] = sm.get(e["ratingKey"])
            d["copy"] = store.show_counts([rk], sid).get(rk)
        else:
            d["copy"] = store.status_map([rk], store.get("server_id")).get(rk)
        sid = store.get("server_id")
        jobs = (store.jobs_for(show_key=rk, server_id=sid) if d["type"] == "show"
                else store.jobs_for(rating_key=rk, server_id=sid))
        d["history"] = jobs[-50:]
        d["nasFolder"] = resync.title_folder(media_root, d)
        return d

    @app.post("/api/queue")
    def queue(body: dict = Body(...)):
        keys = [str(k) for k in body.get("keys", []) if k]
        if not keys:
            return err(400, "nothing selected")
        if len(keys) > MAX_QUEUE_KEYS:
            return err(400, f"Too many titles at once (at most {MAX_QUEUE_KEYS})")
        mode = body.get("mode", "unwatched")
        only = {str(k) for k in body.get("only") or []} or None
        version = body.get("mediaVersion")  # this transfer only; the setting otherwise
        if version is not None and version not in VERSION_PREFS:
            return err(400, f"mediaVersion must be one of {', '.join(VERSION_PREFS)}", "bad_request")
        try:
            jobs = gw.plan(keys, mode=mode, source=body.get("source", "browse"), only=only,
                           server_id=body.get("serverId"), version=version)
        except OtherServer:
            return err(409, "The Plex server changed; reload and try again", "other_server")
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
        sid = store.get("server_id")
        rows = store.jobs(view, q, min(max(limit, 1), 1000), max(offset, 0))
        for j in rows:
            j["waiting"] = j["status"] == "queued" and j["server_id"] != sid
        return {"jobs": rows, "current": dl.snapshot(),
                "paused": bool(store.get("paused", False)), "workerError": dl.last_error}

    @app.post("/api/resync/check")
    def resync_check(body: dict = Body(...)):
        return resync.check(store, gw, media_root, resync.scope_path(body.get("path", "")))

    @app.post("/api/resync/apply")
    def resync_apply(body: dict = Body(...)):
        out = resync.apply(store, gw, media_root, resync.scope_path(body.get("path", "")),
                           body.get("serverId"), body.get("ratingKeys"), body.get("subtitles") is True)
        dl.wake()
        d = disk()
        if d and store.pending_bytes() > d["free"] - dl.reserve_bytes():
            out["warning"] = f"The queue is larger than the free space on {device}; some downloads will fail."
        return out

    def _job(job_id: int) -> dict:
        j = store.job(job_id)
        if j is None:
            raise HTTPException(404, f"no job {job_id}")
        return j

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: int):
        _job(job_id)
        if store.cancel_queued(job_id):
            dl.cleanup(job_id)
        else:
            j = store.job(job_id)
            if j["status"] == "downloading" and not j["phase"]:
                dl.cancel(job_id)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/retry")
    def retry(job_id: int):
        j = _job(job_id)
        if j["server_id"] != store.get("server_id"):
            return err(409, "This job belongs to another Plex server; select it first.", "other_server")
        if j["mode"] in ("redownload", "recover") and (j["error"] or "").startswith(CHANGED):
            # an existing copy: it stays a copy, and replacing it with Plex's new file is a
            # re-sync (download first, then retire the old copy), never a retry that gives it up first
            if store.transition(job_id, lambda r: r["status"] == "failed" and r["mode"] in ("redownload", "recover")
                                and (r["error"] or "").startswith(CHANGED),
                                status="done", mode=None, error=None, bytes_done=int(j["size"]), validator=None,
                                note="Plex changed the file; run Re-sync"):
                dl.cleanup(job_id)
            return err(409, "Plex changed the file; run Re-sync to replace the copy safely", "changed")
        if j["mode"] == "upgrade" and j["status"] in ("failed", "cancelled"):
            if (j["error"] or "").startswith(CHANGED):
                store.transition(job_id, lambda r: r["status"] == "failed", status="cancelled",
                                 note="Plex changed the file again; run Re-sync")
                dl.cleanup(job_id)
                return err(409, "Plex changed the file again; run Re-sync", "changed")
            olds = json.loads(j["replaces"] or "[]")
            if not all((store.job(int(o)) or {}).get("status") == "done" for o in olds) or \
                    any(u["id"] != job_id for o in olds for u in store.upgrades_of(int(o))):
                return err(409, "The copy this re-sync would replace changed; run Re-sync again", "changed")
        if j["status"] == "failed" and (j["error"] or "").startswith(CHANGED):
            # the old file is gone from Plex: queue the item again as it is now
            try:
                planned = gw.plan([j["rating_key"]], mode="all", source=j["source"] or "browse",
                                  server_id=j["server_id"])
            except OtherServer:
                return err(409, "This job belongs to another Plex server; select it first.", "other_server")
            if len(planned) != 1:
                return err(409, "Plex now has this title in several files; remove it and copy it again", "changed")
            if not store.requeue_changed(job_id, planned[0]):  # a Remove first, or a clash: nothing changes
                return err(409, "This job changed meanwhile, or the new file is already copied; reload", "changed")
            dl.cleanup(job_id)
            dl.wake()
            return {"ok": True, "requeued": 1}
        if j["status"] in ("failed", "cancelled"):
            if not store.transition(job_id, lambda r: r["status"] in ("failed", "cancelled"), status="queued",
                                    error=None, note=None, finished_at=None):
                return err(409, "This job changed meanwhile; reload", "changed")
            dl.wake()
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/remove")
    def remove(job_id: int):
        return {"result": remove_job(_job(job_id))}

    @app.post("/api/remove")
    def remove_many(body: dict = Body(...)):
        """Remove by rating/show key. Keys belong to one Plex server, so the request
        names the server it was made for."""
        sid = store.get("server_id")
        if not sid:
            return err(409, "Choose a Plex server first", "no_server")
        if body.get("serverId") != sid:
            return err(409, "The Plex server changed; reload and try again", "other_server")
        results: dict[str, int] = {}
        with resync.LOCK:  # selecting another server waits until this removal is done
            if store.get("server_id") != sid:
                return err(409, "The Plex server changed; reload and try again", "other_server")
            targets = []
            for rk in body.get("ratingKeys", []):
                targets += store.jobs_for(rating_key=str(rk), server_id=sid)
            for sk in body.get("showKeys", []):
                targets += store.jobs_for(show_key=str(sk), server_id=sid)
            for j in {j["id"]: j for j in targets}.values():
                r = remove_job(j)
                results[r] = results.get(r, 0) + 1
        return {"results": results}

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
            return err(409, f"{label(busy)} is downloading into this folder. Cancel it in the Queue first.",
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
                dl.cleanup(j["id"])
        return out

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

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as fh:
        # a new version is a new URL, so browsers never run an old app.js after an update
        page = re.sub(r'"(/static/[\w.-]+\.(?:js|css))"', rf'"\1?v={VERSION}"', fh.read())

    @app.get("/")
    def index():
        return Response(page, media_type="text/html", headers={"Cache-Control": "no-cache"})

    return app
