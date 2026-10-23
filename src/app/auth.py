"""Web UI sign-in: one user, a salted scrypt hash in the settings table, and
sessions in their own table (only a hash of each token is stored)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import math
import os
import re
import secrets
import threading
import time

from .store import now

COOKIE = "travelarr_session"
SESSION = 30 * 24 * 3600          # seconds; renewed when a used session is past half its life
MAX_FAILS, FAIL_WINDOW, LOCK = 5, 15 * 60, 5 * 60
USERNAME_RE = re.compile(r"[A-Za-z0-9._@-]{1,64}")
SCRYPT = (2 ** 14, 8, 1)
log = logging.getLogger("travelarr")


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    n, r, p = SCRYPT
    salt = os.urandom(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(key)}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        _, n, r, p, salt, key = (stored or "").split("$")
        want = base64.b64decode(key)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(want))
    except ValueError:
        return False
    return hmac.compare_digest(got, want)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def problem(user, password, repeat) -> str | None:
    """Why a new user name / password pair is not acceptable, or None."""
    if not isinstance(user, str) or not USERNAME_RE.fullmatch(user):
        return "User name: 1 to 64 letters, digits, '.', '_', '@' or '-'"
    if not isinstance(password, str) or not 8 <= len(password) <= 256:
        return "Password: 8 to 256 characters"
    if repeat != password:
        return "The passwords do not match"
    return None


class Auth:
    def __init__(self, store):
        self.store = store
        self._fails: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}
        self._lock = threading.Lock()

    # -- settings ----------------------------------------------------------------
    def config(self) -> dict:
        return self.store.get("auth") or {}

    def enabled(self) -> bool:
        return bool(self.config().get("enabled"))

    def user(self) -> str | None:
        return self.config().get("user")

    def check(self, user, password) -> bool:
        c = self.config()
        if not isinstance(user, str) or not isinstance(password, str):
            return False
        pw_ok = verify_password(password, c.get("hash"))   # always runs, so timing does not tell which was wrong
        return hmac.compare_digest(user.encode(), (c.get("user") or "").encode()) and pw_ok

    def enable(self, user: str, password: str) -> None:
        self.store.set("auth", {"enabled": True, "user": user, "hash": hash_password(password), "changed": now()})
        self.store.exec("DELETE FROM sessions")

    def change(self, password: str, keep: str) -> None:
        c = self.config()
        self.store.set("auth", {**c, "hash": hash_password(password), "changed": now()})
        self.store.exec("DELETE FROM sessions WHERE token_hash != ?", (_digest(keep),))

    def disable(self) -> None:
        self.store.set("auth", {**self.config(), "enabled": False, "hash": None, "changed": now()})
        self.store.exec("DELETE FROM sessions")

    # -- sessions ----------------------------------------------------------------
    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        t = int(time.time())
        self.store.exec("INSERT INTO sessions(token_hash, created, expires) VALUES(?,?,?)", (_digest(token), t, t + SESSION))
        return token

    def session(self, token: str | None) -> str | None:
        """'ok', 'renewed' (expiry moved on; send the cookie again) or None."""
        if not token:
            return None
        h, t = _digest(token), int(time.time())
        r = self.store.rows("SELECT expires FROM sessions WHERE token_hash=?", (h,))
        if not r or r[0]["expires"] <= t:
            return None
        if r[0]["expires"] - t < SESSION // 2:
            self.store.exec("UPDATE sessions SET expires=? WHERE token_hash=?", (t + SESSION, h))
            return "renewed"
        return "ok"

    def end_session(self, token: str) -> None:
        self.store.exec("DELETE FROM sessions WHERE token_hash=?", (_digest(token),))

    # -- wrong-password lock, per client IP ----------------------------------------
    def locked_for(self, ip: str) -> int:
        with self._lock:
            left = self._locked.get(ip, 0) - time.monotonic()
            return math.ceil(left) if left > 0 else 0

    def failed(self, ip: str) -> None:
        with self._lock:
            t = time.monotonic()
            fails = [f for f in self._fails.get(ip, []) if t - f < FAIL_WINDOW] + [t]
            if len(fails) >= MAX_FAILS:
                self._locked[ip], fails = t + LOCK, []
            self._fails[ip] = fails
            if len(self._fails) > 1000:
                for k in [k for k, v in self._fails.items() if not v or t - v[-1] >= FAIL_WINDOW]:
                    del self._fails[k]
                for k in [k for k, v in self._locked.items() if v <= t]:
                    del self._locked[k]

    def succeeded(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)

    # -- startup -----------------------------------------------------------------
    def startup(self, env=os.environ) -> None:
        """TRAVELARR_RESET_LOGIN=yes turns sign-in off; otherwise a 2.x password
        (TRAVELARR_PASSWORD / TRAVELARR_USERNAME) is imported once."""
        if (env.get("TRAVELARR_RESET_LOGIN") or "").strip().lower() == "yes":
            self.store.set("auth", {**self.config(), "user": self.user() or "admin", "enabled": False,
                                    "hash": None, "changed": now()})
            self.store.exec("DELETE FROM sessions")
            log.warning("TRAVELARR_RESET_LOGIN is set: sign-in is off. Remove the variable, then turn sign-in "
                        "on again in Settings > Authentication")
        elif not self.store.get("auth") and env.get("TRAVELARR_PASSWORD"):
            user = (env.get("TRAVELARR_USERNAME") or "").strip() or "admin"
            if not USERNAME_RE.fullmatch(user):
                log.warning("TRAVELARR_USERNAME is not a valid user name; imported the password for user admin")
                user = "admin"
            self.store.set("auth", {"enabled": True, "user": user, "hash": hash_password(env["TRAVELARR_PASSWORD"]),
                                    "changed": now()})
            log.info("imported the web UI password for user %s; TRAVELARR_USERNAME and TRAVELARR_PASSWORD are no "
                     "longer used and can be removed", user)
        self.store.exec("DELETE FROM sessions WHERE expires <= ?", (int(time.time()),))
        if self.enabled():
            log.info("web UI sign-in is on for %s", self.user())
        else:
            log.warning("web UI sign-in is off: anyone on your network can open travelarr. "
                        "Turn it on in Settings > Authentication")
