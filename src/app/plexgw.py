"""Everything that talks to Plex: sign-in, server connection, browsing,
turning a selection into file jobs, and the URLs the downloader fetches."""
from __future__ import annotations

import http.client
import json
import logging
import socket
import ssl
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import Future

import plexapi
import plexapi.config
import requests
from plexapi.exceptions import Unauthorized
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer
from requests.certs import where as ca_bundle

from .paths import dest_rel_for, in_versions, unversioned, version_rel

log = logging.getLogger("travelarr.plex")
PLEX_TV = "https://plex.tv"

# Picking a server address: every local/remote candidate is asked for /identity
# at once, without the token, and the best one that answers as this server wins.
PROBE_CONNECT = 3
PROBE_READ = 3
PROBE_DEADLINE = 5.0
PROBE_MAX_BYTES = 64 << 10
PROBE_MAX_CANDIDATES = 8
# Process-wide cap on probes still running, including any stuck in a DNS lookup.
_probe_slots = threading.BoundedSemaphore(32)
_TLS = ssl.create_default_context(cafile=ca_bundle())

RELAY_MAX_CANDIDATES = 3
RELAY_DEADLINE = 20.0


class NotReady(Exception):
    """Sign-in or server choice missing, or the server is unreachable."""


class OtherServer(Exception):
    """The job's data came from a Plex server that is not the selected one."""


def _is_https(url: str) -> bool:
    return url.lower().startswith("https://")


def _redact(url: str) -> str:
    """scheme://host[:port] for logs: never userinfo, path or query."""
    try:
        u = urllib.parse.urlsplit(url or "")
        host = u.hostname
    except ValueError:
        host = None
    if not host:
        return "<unparseable address>"
    try:
        port = f":{u.port}" if u.port else ""
    except ValueError:
        port = ""
    return f"{u.scheme.lower()}://{f'[{host}]' if ':' in host else host}{port}"


def _origin(url: str) -> tuple[str | None, str | None]:
    """A fixed address reduced to scheme://host[:port], or (None, why it was rejected)."""
    try:
        u = urllib.parse.urlsplit((url or "").strip())
        u.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return None, "invalid address or port"
    scheme = u.scheme.lower()
    if scheme not in ("http", "https"):
        return None, "it must start with http:// or https://"
    if not u.hostname:
        return None, "it has no host"
    if u.username is not None or u.password is not None:
        return None, "it must not contain a user name or password"
    if u.path not in ("", "/") or u.query or u.fragment:
        return None, "it must be just the address, without a path or query"
    return f"{scheme}://{u.netloc}", None


def _left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("probe deadline passed")
    return left


def _abort(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)  # wakes a blocked read in the probe thread
    except OSError:
        pass


def _open(host: str, port: int, deadline: float) -> socket.socket:
    """socket.create_connection, except every address attempt shares the deadline
    (create_connection gives each resolved address its own full timeout)."""
    err: OSError = OSError(f"no address for {host}")
    for family, kind, proto, _, addr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(min(PROBE_CONNECT, _left(deadline)))
            sock.connect(addr)
            return sock
        except OSError as e:
            sock.close()
            err = e
    raise err


class _Rejected:
    """Probe outcome: the server answered an authenticated probe with 401."""


def _identity(url: str, deadline: float, token: str | None = None) -> str | _Rejected | None:
    """machineIdentifier answered at url, or None. Without a token it asks /identity;
    with one (relay, https only) it asks / instead, and a 401 comes back as _Rejected.
    Follows no redirects, and gives up at the deadline even if the peer keeps trickling
    bytes: connecting and the TLS handshake share the remaining time, and a watchdog
    shuts the socket after that."""
    sock = conn = watchdog = None
    try:
        u = urllib.parse.urlsplit(url)
        https = u.scheme == "https"
        if token and not https:
            return None
        port = u.port or (443 if https else 80)
        sock = _open(u.hostname, port, deadline)
        if https:
            sock.settimeout(_left(deadline))
            sock = _TLS.wrap_socket(sock, server_hostname=u.hostname)
        sock.settimeout(min(PROBE_READ, _left(deadline)))
        watchdog = threading.Timer(_left(deadline), _abort, (sock,))
        watchdog.daemon = True
        watchdog.start()
        conn = (http.client.HTTPSConnection(u.hostname, port, context=_TLS) if https
                else http.client.HTTPConnection(u.hostname, port))
        conn.sock = sock
        headers = {"Accept": "application/json"}
        if token:
            headers["X-Plex-Token"] = token
        conn.request("GET", "/" if token else "/identity", headers=headers)
        r = conn.getresponse()
        if r.status == 401 and token:
            return _Rejected()
        if r.status != 200:
            return None
        body = r.read(PROBE_MAX_BYTES + 1)
        if len(body) > PROBE_MAX_BYTES or time.monotonic() > deadline:
            return None
        return json.loads(body)["MediaContainer"]["machineIdentifier"]
    except Exception as e:  # noqa: BLE001
        log.debug("probe %s failed: %s", _redact(url), e)
        return None
    finally:
        if watchdog is not None:
            watchdog.cancel()
        if sock is not None:
            sock.close()


def _probe(url: str, deadline: float, token: str | None, fut: Future) -> None:
    try:
        fut.set_result(_identity(url, deadline, token))
    finally:
        _probe_slots.release()


class _PlexSession(requests.Session):
    """Every request that carries the token goes through this session. It never
    follows an https -> http redirect, and drops X-Plex-Token wherever requests
    drops Authorization (another host or port)."""

    def get_redirect_target(self, resp):
        target = super().get_redirect_target(resp)
        if target and _is_https(resp.url) and not _is_https(urllib.parse.urljoin(resp.url, target)):
            log.warning("refused redirect from %s to %s", _redact(resp.url),
                        _redact(urllib.parse.urljoin(resp.url, target)))
            return None
        return target

    def rebuild_auth(self, prepared_request, response):
        super().rebuild_auth(prepared_request, response)
        if self.should_strip_auth(response.request.url, prepared_request.url):
            prepared_request.headers.pop("X-Plex-Token", None)


# Which versions of a movie or episode are copied. Plex lists every file it has
# for an item as a Media (the original, Optimized Versions, other editions).
# 'all' copies every one, at Plex's own paths (travelarr before 3.6); any other
# choice copies one: a cap takes the best version at or under it.
CAPS = {"2160": 2160, "1080": 1080, "720": 720, "sd": 576}
VERSION_PREFS = ("all", "original", "smallest", *CAPS)


def _size(media) -> int:
    return sum(p.size or 0 for p in media.parts)


def _optimized(media) -> bool:
    """Made by Plex's Optimize (marked so by Plex, or under its 'Plex Versions' folder)."""
    return bool(getattr(media, "isOptimizedVersion", False)) or any(in_versions(p.file) for p in media.parts)


def _lines(media) -> int | None:
    """The version's resolution class: Plex's videoResolution ('4k', '1080', 'sd'),
    else from its width, so a 1920x800 film counts as 1080p. None when unknown."""
    res = (getattr(media, "videoResolution", None) or "").lower().rstrip("p")
    if res == "4k":
        return 2160
    if res.isdigit():
        return int(res)
    if res == "sd":
        return 480
    width = getattr(media, "width", None)
    if not width:
        return None
    return next((lines for w, lines in ((3200, 2160), (2200, 1440), (1600, 1080), (1000, 720)) if width >= w), 480)


def pick_media(video, pref: str):
    """The version of video to copy; None when Plex has no file for it.
    'original': the largest version Plex did not optimize (what a single-version
    item always was); 'smallest': the fewest bytes; a cap ('2160' ... 'sd'): the
    highest resolution at or under it, then a version Plex did not optimize (an
    'Original Quality' optimized copy can be bigger than the original), then
    bitrate; else the smallest."""
    media = [m for m in video.media if m.parts]
    if len(media) <= 1:
        return media[0] if media else None
    if pref == "smallest":
        return min(media, key=_size)
    if pref in CAPS:
        cap = CAPS[pref]
        fit = [m for m in media if (_lines(m) or cap + 1) <= cap]
        if not fit:
            return min(media, key=_size)
        return max(fit, key=lambda m: (_lines(m), not _optimized(m), m.bitrate or 0, _size(m)))
    return max([m for m in media if not _optimized(m)] or media, key=_size)


def _original(video, but=None):
    """The largest version Plex did not optimize, other than `but`."""
    others = [m for m in video.media if m is not but and m.parts and not _optimized(m)]
    return max(others, key=_size) if others else None


def chosen_parts(video, sec, pref: str) -> list[tuple]:
    """(part, dest_rel) for each file of the version of video to copy. An optimized
    version lands where the original would, under the original's name with its own
    extension, never inside 'Plex Versions'. 'all': every file, at Plex's paths."""
    if pref == "all":
        return [(p, dest_rel_for(p.file, sec.locations, sec.title)) for p in video.iterParts()]
    m = pick_media(video, pref)
    if m is None:
        return []
    twin = _original(video, m) if _optimized(m) else None
    out = []
    for i, p in enumerate(m.parts):
        if twin is not None and i < len(twin.parts):
            dest = version_rel(dest_rel_for(twin.parts[i].file, sec.locations, sec.title), p.file)
        else:
            dest = dest_rel_for(unversioned(p.file), sec.locations, sec.title)
        out.append((p, dest))
    return out


class PlexGateway:
    def __init__(self, store, version: str):
        self.store = store
        self.version = version
        cid = store.get("client_id")
        if not cid:
            cid = f"travelarr-{uuid.uuid4()}"
            store.set("client_id", cid)
        self.client_id = cid
        # Make every plexapi request identify as this device, so it shows up
        # once (as "travelarr") under Plex > Settings > Authorized Devices.
        plexapi.X_PLEX_PRODUCT = "travelarr"
        plexapi.X_PLEX_IDENTIFIER = cid
        plexapi.X_PLEX_VERSION = version
        plexapi.X_PLEX_DEVICE_NAME = "travelarr"
        plexapi.BASE_HEADERS = plexapi.config.reset_base_headers()

        self.plex_tv = PLEX_TV
        self.session = _PlexSession()
        self._lock = threading.RLock()
        self._account = None
        self._server = None
        self.conn: dict | None = None
        self._fail: tuple[float, str] | None = None
        self._pin: dict | None = None
        self._sections: dict[tuple, object] = {}  # (server id, section key) -> section

    def _headers(self) -> dict:
        return {"Accept": "application/json", "X-Plex-Product": "travelarr",
                "X-Plex-Client-Identifier": self.client_id, "X-Plex-Version": self.version,
                "X-Plex-Device-Name": "travelarr",
                "X-Plex-Platform": "Linux"}

    def token(self) -> str | None:
        return self.store.get("token")

    def version_pref(self, override: str | None = None) -> str:
        """`override`: a choice for one transfer instead of the setting."""
        pref = override or self.store.get("media_version")
        return pref if pref in VERSION_PREFS else "all"

    def start_login(self) -> str:
        r = self.session.post(f"{self.plex_tv}/api/v2/pins", params={"strong": "true"},
                              headers=self._headers(), timeout=20)
        r.raise_for_status()
        d = r.json()
        self._pin = {"id": d["id"], "code": d["code"]}
        q = urllib.parse.urlencode({"clientID": self.client_id, "code": d["code"],
                                    "context[device][product]": "travelarr"})
        return f"https://app.plex.tv/auth#?{q}"

    def check_login(self) -> bool:
        if self.token():
            return True
        if not self._pin:
            return False
        r = self.session.get(f"{self.plex_tv}/api/v2/pins/{self._pin['id']}",
                             headers=self._headers(), timeout=20)
        if r.status_code == 404:  # PIN expired
            self._pin = None
            return False
        r.raise_for_status()
        tok = r.json().get("authToken")
        if not tok:
            return False
        self.store.set("token", tok)
        self._pin = None
        self.reset()
        try:
            self.store.set("username", self.account().username)
        except Exception as e:  # noqa: BLE001
            log.warning("signed in but could not read the account: %s", e)
        return True

    def sign_out(self) -> None:
        with self._lock:  # never a snapshot pairing one server's connection with another's id
            for k in ("token", "username", "server_id", "server_name", "preferred_url"):
                self.store.set(k, None)
            self.reset()

    def reset(self) -> None:
        with self._lock:
            self._account = self._server = self.conn = None
            self._fail = None
            self._sections.clear()

    def invalidate(self) -> None:
        """Drop the server connection (e.g. after a network error); the next
        call reconnects, picking local or remote automatically."""
        with self._lock:
            self._server = self.conn = None
            self._sections.clear()

    def account(self) -> MyPlexAccount:
        with self._lock:
            tok = self.token()
            if not tok:
                raise NotReady("Not signed in to Plex")
            if self._account is None:
                self._account = MyPlexAccount(token=tok, session=self.session, timeout=20)
            return self._account

    def servers(self) -> list[dict]:
        out = []
        for r in self.account().resources():
            if "server" not in (r.provides or ""):
                continue
            out.append({"id": r.clientIdentifier, "name": r.name, "owned": bool(r.owned),
                        "online": r.presence,
                        "connections": [{"uri": c.uri, "local": bool(c.local), "relay": bool(c.relay)}
                                        for c in r.connections]})
        return out

    def select_server(self, server_id: str, name: str | None, preferred_url: str | None,
                      allow_relay: bool) -> None:
        with self._lock:  # the id and the cached connection change together
            self.store.set("server_id", server_id)
            self.store.set("server_name", name)
            self.store.set("preferred_url", (preferred_url or "").strip() or None)
            self.store.set("allow_relay", bool(allow_relay))
            self.reset()

    def snapshot(self) -> tuple[PlexServer, str]:
        """The selected server's connection and id, read together."""
        with self._lock:
            return self.server(), self.store.get("server_id")

    def server_for(self, server_id: str | None) -> PlexServer:
        """The connection to use for one request of a job from server_id: raises
        OtherServer when another server is selected. The caller uses the returned
        object for that request, so a later selection change cannot redirect it."""
        with self._lock:
            if server_id and self.store.get("server_id") != server_id:
                raise OtherServer("this job belongs to another Plex server")
            return self.server()

    def server(self) -> PlexServer:
        with self._lock:
            if self._server is not None:
                return self._server
            if self._fail and time.time() - self._fail[0] < 20:
                raise NotReady(self._fail[1])
            try:
                return self._connect()
            except NotReady as e:
                self._fail = (time.time(), str(e))
                raise

    def _connect(self) -> PlexServer:
        sid = self.store.get("server_id")
        if not sid:
            raise NotReady("Choose your Plex server")
        try:
            res = next((r for r in self.account().resources() if r.clientIdentifier == sid), None)
        except requests.RequestException as e:
            raise NotReady(f"Cannot reach plex.tv: {e}") from None
        if res is None:
            raise NotReady("That server is no longer in your Plex account")
        tok = res.accessToken or self.token()
        url, skipped = self._manual_url(res, sid)
        kind = "manual"
        if url is None:
            url, more = self._first_reachable(self._auto_candidates(res), sid)
            skipped += more
            kind = self._kind(res, url) if url else None
        if url is None and not skipped and self.store.get("allow_relay"):
            # Relay addresses are not checked with /identity first; each one gets the
            # token on an authenticated check instead, over https only.
            url, skipped = self._first_reachable(self._relay_candidates(res), sid, RELAY_DEADLINE, tok)
            kind = "relay"
        if url is None and skipped:
            log.warning("connection checks for %s are still pending; DNS may not be answering", res.name)
            raise NotReady("Connection checks are still pending (DNS not answering?); retrying shortly")
        if url is None:
            raise NotReady(self._unreachable(res))
        try:
            srv = PlexServer(url, tok, session=self.session, timeout=15)
        except Unauthorized:
            raise
        except Exception as e:  # noqa: BLE001
            raise NotReady(f"{res.name} answered at {url} but the connection failed: {e}") from None
        self._server = srv
        self._fail = None
        self.conn = {"name": res.name, "url": srv._baseurl, "kind": kind}
        log.info("connected to %s via %s (%s)", res.name, kind, srv._baseurl)
        return srv

    @staticmethod
    def _unreachable(res) -> str:
        return (f"Cannot reach {res.name}. At home it uses the LAN address; away it needs "
                f"Plex Remote Access (direct, not relay) or your VPN.")

    @staticmethod
    def _auto_candidates(res) -> list[str]:
        """Local then remote addresses. Only https when the server requires it (plexapi's
        ssl=True is not enough: a connection's uri can itself be http://); otherwise every
        https address comes before any http one."""
        urls = list(dict.fromkeys(u for u in res.preferred_connections(locations=["local", "remote"]) if u))
        if res.httpsRequired:
            return [u for u in urls if _is_https(u)]
        return sorted(urls, key=lambda u: not _is_https(u))

    @staticmethod
    def _relay_candidates(res) -> list[str]:
        urls = dict.fromkeys(u for u in res.preferred_connections(locations=["relay"]) if u)
        return [u for u in urls if _is_https(u)][:RELAY_MAX_CANDIDATES]

    @staticmethod
    def _kind(res, url: str) -> str:
        for c in res.connections:
            if url.rstrip("/") in ((c.uri or "").rstrip("/"), (c.httpuri or "").rstrip("/")):
                return "relay" if c.relay else "local" if c.local else "remote"
        return "remote"

    def _manual_url(self, res, sid: str) -> tuple[str | None, int]:
        """The fixed address if it answers as this server, plus the skipped-probe count."""
        raw = self.store.get("preferred_url")
        if not raw:
            return None, 0
        url, why = _origin(raw)
        if url is None:
            log.warning("ignoring fixed address %s: %s", _redact(raw), why)
            return None, 0
        if res.httpsRequired and not _is_https(url):
            log.warning("ignoring fixed address %s: %s requires https", _redact(raw), res.name)
            return None, 0
        found, skipped = self._first_reachable([url], sid)
        if found is None and not skipped:
            log.warning("fixed address %s did not answer as %s; trying Plex's own addresses",
                        _redact(raw), res.name)
        return found, skipped

    def _first_reachable(self, urls: list[str], machine_id: str, window: float | None = None,
                         token: str | None = None) -> tuple[str | None, int]:
        """The first URL, in priority order, that answers as this server, plus how many
        candidates were skipped for lack of a probe slot. All are asked at once; a
        lower-priority answer only wins once every higher one has failed or run out of time."""
        urls = list(dict.fromkeys(urls))[:PROBE_MAX_CANDIDATES]
        deadline = time.monotonic() + (PROBE_DEADLINE if window is None else window)
        futures: list[Future | None] = []
        for url in urls:
            f = None
            if _probe_slots.acquire(blocking=False):
                f = Future()
                try:
                    threading.Thread(target=_probe, args=(url, deadline, token, f), name="plex-probe",
                                     daemon=True).start()
                except RuntimeError:
                    _probe_slots.release()
                    f = None
            futures.append(f)
        rejected = False
        for url, f in zip(urls, futures):
            if f is None:
                continue
            try:
                answer = f.result(timeout=max(0.0, deadline - time.monotonic()))
            except TimeoutError:
                continue
            if answer == machine_id:
                return url, futures.count(None)
            rejected = rejected or isinstance(answer, _Rejected)
        if rejected:
            raise Unauthorized("Plex rejected the token")
        return None, futures.count(None)

    def _section(self, key, srv: PlexServer | None = None) -> object:
        srv = srv or self.server()
        ck = (getattr(srv, "machineIdentifier", None), str(key))
        with self._lock:
            if ck not in self._sections:
                self._sections[ck] = srv.library.sectionByID(int(key))
            return self._sections[ck]

    def libraries(self) -> list[dict]:
        out = []
        srv = self.server()
        for s in srv.library.sections():
            if s.type in ("movie", "show"):
                self._sections[(getattr(srv, "machineIdentifier", None), str(s.key))] = s
                out.append({"key": str(s.key), "title": s.title, "type": s.type})
        return out

    def summary(self, it, pref: str | None = None) -> dict:
        it._autoReload = False  # listing data is enough; avoid a request per item
        d = {"ratingKey": str(it.ratingKey), "type": it.type, "title": it.title,
             "year": getattr(it, "year", None), "thumb": it.thumb}
        if it.type == "movie":
            added = getattr(it, "addedAt", None)
            d["addedAt"] = int(added.timestamp()) if added else None
            d["watched"] = bool(getattr(it, "viewCount", 0))
            try:
                pref = pref or self.version_pref()  # the size of what a copy would take
                copied = it.media if pref == "all" else [m for m in [pick_media(it, pref)] if m]
                d["size"] = sum(_size(m) for m in copied)
            except Exception:  # noqa: BLE001
                d["size"] = None
        elif it.type == "show":
            leaf = getattr(it, "leafCount", 0) or 0
            d["episodes"] = leaf
            d["unwatched"] = leaf - (getattr(it, "viewedLeafCount", 0) or 0)
        return d

    def list_items(self, section_key: str, q: str | None, start: int, size: int, sort: str = "title") -> dict:
        """sort 'recent': movies by date added, shows by their newest episode
        (a show's own addedAt misses new episodes of old shows). titleSort
        breaks ties so pages never overlap."""
        sec = self._section(section_key)
        srv = self.server()
        order = "titleSort"
        if sort == "recent":
            order = "addedAt:desc,titleSort" if sec.type == "movie" else "episode.addedAt:desc,titleSort"
        params = {"type": 1 if sec.type == "movie" else 2, "sort": order}
        if q:
            params["title"] = q
        key = f"/library/sections/{sec.key}/all?{urllib.parse.urlencode(params)}"
        items = srv.fetchItems(key, container_start=start, container_size=size, maxresults=size)
        head = srv.query(key, headers={"X-Plex-Container-Start": "0", "X-Plex-Container-Size": "0"})
        total = int(head.attrib.get("totalSize") or head.attrib.get("size") or 0)
        pref = self.version_pref()
        return {"items": [self.summary(i, pref) for i in items], "total": total, "start": start}

    def watchlist(self) -> list[dict]:
        srv = self.server()
        out = []
        for w in self.account().watchlist():
            lib = None
            if w.type in ("movie", "show"):
                try:
                    found = srv.library.search(guid=w.guid, libtype=w.type)
                    lib = found[0] if found else None
                except Exception:  # noqa: BLE001
                    lib = None
            if lib is not None:
                d = self.summary(lib)
                d["inLibrary"] = True
            else:
                w._autoReload = False
                d = {"ratingKey": None, "type": w.type, "title": w.title,
                     "year": getattr(w, "year", None), "thumb": w.thumb, "inLibrary": False}
            out.append(d)
        return out

    @staticmethod
    def _part(part, dest: str) -> dict:
        return {"id": part.id, "key": part.key, "size": part.size or 0, "file": part.file, "dest": dest}

    def item(self, rating_key: str, version: str | None = None) -> dict:
        """`version`: the version choice for this page instead of the setting."""
        it = self.server().fetchItem(int(rating_key))
        sec = self._section(it.librarySectionID)
        pref = self.version_pref(version)
        d = self.summary(it, pref)
        d["summary"] = getattr(it, "summary", "")
        if it.type in ("movie", "episode"):
            d["parts"] = [self._part(p, dest) for p, dest in chosen_parts(it, sec, pref)]
            d["size"] = sum(p["size"] for p in d["parts"])
            if it.type == "episode":
                d["showKey"] = str(it.grandparentRatingKey)
        if it.type == "show":
            seasons: dict[int, dict] = {}
            for ep in it.episodes():
                ep._autoReload = False
                s = seasons.setdefault(ep.parentIndex if ep.parentIndex is not None else -1,
                                       {"index": ep.parentIndex, "title": ep.parentTitle,
                                        "ratingKey": str(ep.parentRatingKey), "episodes": []})
                parts = [self._part(p, dest) for p, dest in chosen_parts(ep, sec, pref)]
                s["episodes"].append({"ratingKey": str(ep.ratingKey), "index": ep.index,
                                      "title": ep.title, "watched": ep.isWatched,
                                      "size": sum(p["size"] for p in parts),
                                      "dest": parts[0]["dest"] if parts else None})
            d["seasons"] = [seasons[k] for k in sorted(seasons)]
            d["size"] = sum(e["size"] for s in d["seasons"] for e in s["episodes"])
        return d

    def plan(self, keys: list[str], mode: str = "unwatched", source: str = "browse",
             only: set[str] | None = None, server_id: str | None = None,
             version: str | None = None) -> list[dict]:
        """Expand movies / episodes / seasons / shows into one job per file of
        the versions each video is copied in (see `chosen_parts`); `version`
        overrides the setting for this transfer.
        `only` limits a show or season to those episode keys (one request
        for the whole show instead of one per episode). One server snapshot
        serves the whole plan, and every job records that server."""
        srv, sid = self.snapshot()
        if server_id and sid != server_id:  # rating keys mean something else on another server
            raise OtherServer("the selected Plex server changed")
        pref = self.version_pref(version)
        jobs: list[dict] = []
        for rk in keys:
            it = srv.fetchItem(int(rk))
            sec = self._section(it.librarySectionID, srv)
            if it.type in ("movie", "episode"):
                videos = [it]
            elif it.type in ("show", "season"):
                videos = it.episodes()
                for v in videos:
                    v._autoReload = False  # the episode list already has parts and indices
                if only:
                    videos = [v for v in videos if str(v.ratingKey) in only]
                elif mode == "unwatched":
                    videos = [v for v in videos if not v.isWatched]
            else:
                continue
            jobs += [self.job(v, p, dest, sid, source) for v in videos for p, dest in chosen_parts(v, sec, pref)]
        return jobs

    @staticmethod
    def job(v, p, dest: str, sid: str, source: str) -> dict:
        """The job that copies part p of movie or episode v to dest."""
        return {
            "rating_key": str(v.ratingKey),
            "show_key": str(v.grandparentRatingKey) if v.type == "episode" else None,
            "kind": v.type,
            "title": v.title,
            "show_title": getattr(v, "grandparentTitle", None),
            "season": getattr(v, "parentIndex", None),
            "episode": getattr(v, "index", None) if v.type == "episode" else None,
            "year": getattr(v, "year", None),
            "part_id": p.id,
            "part_key": p.key,
            "size": p.size or 0,
            "dest_rel": dest,
            "source": source,
            "server_id": sid,
            "guid": getattr(v, "guid", None),
        }

    @staticmethod
    def file_request(part_key: str, srv: PlexServer) -> tuple[str, dict]:
        return f"{srv._baseurl}{part_key}?download=1", {"X-Plex-Token": srv._token}

    def read_range(self, srv: PlexServer, part_key: str, size: int, start: int, length: int) -> bytes | None:
        """`length` bytes of a Plex file from `start`; None when Plex does not send
        that range of a file of that size."""
        url, headers = self.file_request(part_key, srv)
        headers.update({"Range": f"bytes={start}-{start + length - 1}", "Accept-Encoding": "identity"})
        with self.session.get(url, headers=headers, stream=True, timeout=(15, 30)) as r:
            got = (r.headers.get("Content-Range") or "").replace("bytes ", "").replace("/", "-").split("-")
            if r.status_code != 206 or len(got) != 3 or got[0] != str(start) or got[2] != str(size):
                return None
            data = r.raw.read(length, decode_content=False)
        return data if len(data) == length else None

    @staticmethod
    def text_streams(part) -> list[dict]:
        """External (sidecar) subtitles only; embedded ones travel inside the file."""
        return [{"key": s.key, "codec": (s.codec or "").lower(),
                 "lang": s.languageTag or s.languageCode, "forced": bool(s.forced),
                 "hi": bool(s.hearingImpaired), "title": s.displayTitle}
                for s in part.subtitleStreams() if s.key]

    def subtitle_streams(self, rating_key: str, part_id: int, srv: PlexServer) -> tuple[list[dict], str | None]:
        """The part's external subtitles and the item's guid. An optimized version
        has none of its own: it gets the original's, made for the same timing."""
        it = srv.fetchItem(int(rating_key))
        for m in it.media:
            for i, p in enumerate(m.parts):
                if p.id != part_id:
                    continue
                streams = self.text_streams(p)
                twin = _original(it, m) if not streams and _optimized(m) else None
                if twin is not None and i < len(twin.parts):
                    streams = self.text_streams(twin.parts[i])
                return streams, getattr(it, "guid", None)
        # not "no subtitles": the file is not on Plex any more, so nothing can be compared
        raise ValueError("the file on Plex changed again; run Re-sync")

    def fetch_bytes(self, key: str, srv: PlexServer, max_bytes: int = 20 << 20) -> bytes:
        with self.session.get(f"{srv._baseurl}{key}", headers={"X-Plex-Token": srv._token},
                              stream=True, timeout=(15, 60)) as r:
            r.raise_for_status()
            buf = bytearray()
            for chunk in r.iter_content(65536):
                buf += chunk
                if len(buf) > max_bytes:
                    raise ValueError("subtitle larger than expected")
            return bytes(buf)

    def thumb(self, path: str, width: int, height: int) -> tuple[bytes, str]:
        """The token goes in a header, never the URL: request errors quote the URL
        and are shown to the browser (plexapi's transcodeImage puts it in the URL)."""
        srv = self.server()
        q = urllib.parse.urlencode({"url": path, "height": height, "width": width, "minSize": 1, "upscale": 1})
        r = self.session.get(f"{srv._baseurl}/photo/:/transcode?{q}", headers={"X-Plex-Token": srv._token},
                             timeout=20)
        r.raise_for_status()
        return r.content, r.headers.get("Content-Type", "image/jpeg")
