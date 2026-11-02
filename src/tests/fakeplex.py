"""A small fake Plex Media Server (+ the two plex.tv PIN endpoints) that
returns the XML shapes python-plexapi parses, and serves part downloads
with Range support plus knobs to simulate failures."""
from __future__ import annotations

import json
import posixpath
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import quoteattr

TOKEN = "tok-123"
MOVIES, TV = "/data/Media/Movies", "/data/Media/TV-Shows"


def attrs(d: dict) -> str:
    return " ".join(f"{k}={quoteattr(str(v))}" for k, v in d.items() if v is not None)


SRT = "1\n00:00:01,000 --> 00:00:02,500\n{}\n\n".format


class FakePlex:
    def __init__(self):
        self.requests: list[tuple[str, str, dict]] = []
        self.drop_after: dict[int, int] = {}      # part id -> bytes to send before dropping (once)
        self.drop_every: dict[int, int] = {}      # part id -> bytes to send before dropping (every time)
        self.replace_on_drop: dict[int, bytes] = {}   # new content swapped in when the drop happens
        self.ignore_range: set[int] = set()
        self.bad_range_once: set[int] = set()     # next ranged answer starts at the wrong offset
        self.range_416: set[int] = set()
        self.weak_etag: set[int] = set()
        self.no_etag: set[int] = set()
        self.encoding: set[int] = set()           # claims Content-Encoding: gzip
        self.no_length: set[int] = set()          # full answers without Content-Length
        self.extra_bytes: dict[int, int] = {}     # next ranged answer: no Content-Length, N bytes too many
        self.rate: dict[int, int] = {}            # bytes per second
        self.content_type: dict[int, str] = {}    # overrides video/x-matroska
        self.xml_instead: set[int] = set()
        self.identity_id = "srv1"
        self.identity_redirect = False
        self.identity_pad = 0
        self.root_status: int | None = None       # make "/" fail after /identity succeeded
        self.redirect_to: str | None = None       # target for /redir
        self.https_required = False
        self.conns: list | None = None            # overrides the advertised connections
        self.pin_polls = 0
        self.parts: dict[int, bytes] = {}
        self.movies = [
            dict(rk=101, title="Heat", year=1995, guid="plex://movie/heat", watched=True, part=1001, added=1_700_000_200,
                 file=f"{MOVIES}/Heat (1995)/Heat (1995).mkv", size=300_000,
                 subs=[dict(id=9001, codec="srt", lang="es", ext=True, body=SRT("Hola")),
                       dict(id=9002, codec="srt", lang="en", forced=True, ext=True, body=SRT("Hi")),
                       dict(id=9003, codec="srt", lang="en", ext=False),
                       dict(id=9004, codec="pgs", lang="fr", ext=True),
                       dict(id=9005, codec="srt", lang="de", ext=True, body="<?xml version='1.0'?><MediaContainer/>")]),
            dict(rk=102, title="Loose Movie", year=2001, guid="plex://movie/loose", watched=False, part=1002,
                 added=1_700_000_300,
                 file=f"{MOVIES}/Loose Movie.mkv", size=120_000, subs=[]),
            dict(rk=103, title="Heathers", year=1989, guid="plex://movie/heathers", watched=False, part=1003,
                 added=1_700_000_200,                       # same second as Heat
                 file=f"{MOVIES}/Heathers (1989)/Heathers (1989).mkv", size=50_000, subs=[]),
        ]
        self.show = dict(rk=200, title="Show A", year=2020, guid="plex://show/a")
        self.episodes = [
            dict(rk=201, season=1, index=1, title="Pilot", watched=True, part=2001, size=80_000,
                 file=f"{TV}/Show A/Season 01/Show A - S01E01 - Pilot.mkv", subs=[]),
            dict(rk=202, season=1, index=2, title="Second", watched=False, part=2002, size=90_000,
                 file=f"{TV}/Show A/Season 01/Show A - S01E02 - Second.mkv",
                 subs=[dict(id=9101, codec="srt", lang="es", ext=True, body=SRT("Dos"))]),
            dict(rk=203, season=2, index=1, title="Third", watched=False, part=2003, size=70_000,
                 file=f"{TV}/Show A/Season 02/Show A - S02E01 - Third.mkv", subs=[]),
        ]
        for v in self.movies + self.episodes:
            # deterministic, non-repeating-ish content so corruption would show
            self.parts[v["part"]] = bytes((i * 7 + v["part"]) % 251 for i in range(v["size"]))
        self.versions = {pid: 1 for pid in self.parts}
        self.watchlist = [("movie", "plex://movie/heat", "Heat"), ("show", "plex://show/a", "Show A"),
                          ("movie", "plex://movie/nope", "Not Owned")]
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.handle(self, "GET")

            def do_POST(self):
                outer.handle(self, "POST")

            def do_HEAD(self):
                outer.handle(self, "HEAD")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def add_version(self, v, part, file, size, content=None, **media):
        """Plex has another version of movie/episode dict v (its own Media): media
        holds res (videoResolution), width, bitrate, optimized and subs."""
        x = dict(part=part, file=file, size=size, subs=media.pop("subs", []), **media)
        v.setdefault("versions", []).append(x)
        self.parts[part] = content or bytes((i * 13 + part) % 251 for i in range(size))
        self.versions[part] = 1
        return x

    # ---------------------------------------------------------------- xml
    def part_xml(self, v, full):
        """Every version of v, v's own first, each one a Media as Plex lists them."""
        if v.get("no_parts"):
            return ""
        return "".join(self.media_xml(x, full) for x in [v] + v.get("versions", []))

    def media_xml(self, v, full):
        streams = ""
        if full:
            streams = '<Stream id="1" streamType="1" codec="h264" index="0"/>'
            for s in v["subs"]:
                streams += "<Stream " + attrs({
                    "id": s["id"], "streamType": 3, "codec": s["codec"], "languageTag": s["lang"],
                    "languageCode": s["lang"], "forced": 1 if s.get("forced") else None,
                    "key": f"/library/streams/{s['id']}" if s["ext"] else None,
                    "index": None if s["ext"] else 2, "displayTitle": f"{s['lang']} {s['codec']}"}) + "/>"
        extra = ""
        for x in v.get("extra_parts", []):   # a video split into several files
            extra += "<Part " + attrs({"id": x["part"], "key": f"/library/parts/{x['part']}/1700000000/file.mkv",
                                       "file": x["file"], "size": x["size"], "container": "mkv"}) + "/>"
        media = attrs({"id": f'{v["part"]}0', "container": "mkv", "videoResolution": v.get("res"),
                       "width": v.get("width"), "bitrate": v.get("bitrate"),
                       "proxyType": 42 if v.get("optimized") else None})  # 42: an Optimized Version
        return (f'<Media {media}><Part ' + attrs({
            "id": v["part"], "key": f"/library/parts/{v['part']}/1700000000/file.mkv",
            "file": v["file"], "size": v["size"], "container": "mkv"}) + f">{streams}</Part>{extra}</Media>")

    def movie_xml(self, m, full=False):
        return "<Video " + attrs({
            "ratingKey": m["rk"], "key": f"/library/metadata/{m['rk']}", "guid": m["guid"], "type": "movie",
            "title": m["title"], "titleSort": m["title"], "year": m["year"], "summary": f"About {m['title']}",
            "thumb": f"/library/metadata/{m['rk']}/thumb/1", "librarySectionID": 1, "addedAt": m["added"],
            "viewCount": 1 if m["watched"] else None}) + ">" + self.part_xml(m, full) + "</Video>"

    def show_xml(self):
        s = self.show
        watched = sum(1 for e in self.episodes if e["watched"])
        return "<Directory " + attrs({
            "ratingKey": s["rk"], "key": f"/library/metadata/{s['rk']}/children", "guid": s["guid"],
            "type": "show", "title": s["title"], "titleSort": s["title"], "year": s["year"],
            "thumb": f"/library/metadata/{s['rk']}/thumb/1", "librarySectionID": 2,
            "leafCount": len(self.episodes), "viewedLeafCount": watched, "childCount": 2,
            "summary": "A show"}) + "/>"

    def ep_xml(self, e, full=False):
        return "<Video " + attrs({
            "ratingKey": e["rk"], "key": f"/library/metadata/{e['rk']}", "type": "episode",
            "guid": f"plex://episode/{e['rk']}",
            "title": e["title"], "grandparentRatingKey": 200, "grandparentTitle": "Show A",
            "grandparentKey": "/library/metadata/200", "parentRatingKey": 200 + e["season"] * 10,
            "parentTitle": f"Season {e['season']}", "parentIndex": e["season"], "index": e["index"],
            "librarySectionID": 2, "viewCount": 1 if e["watched"] else None}) + ">" + \
            self.part_xml(e, full) + "</Video>"

    def folder(self, h, sec: int, parent: str | None):
        """Plex's folder view: the library folder's subfolders and the videos in it."""
        root = MOVIES if sec == 1 else TV
        videos = [(v, v["file"][len(root) + 1:]) for v in (self.movies if sec == 1 else self.episodes)
                  if v["file"].startswith(root + "/") and not v.get("no_parts")]
        dirs = sorted({"/".join(r.split("/")[:i]) for _, r in videos for i in range(1, r.count("/") + 1)})
        ids = {d: str(1000 * sec + n) for n, d in enumerate(dirs)}
        here = "" if parent is None else next((d for d, i in ids.items() if i == parent), None)
        if here is None:
            return self.send(h, 404, b"<html>404</html>", "text/html")
        body = "".join("<Directory " + attrs({"key": f"/library/sections/{sec}/folder?parent={ids[d]}",
                                               "title": posixpath.basename(d)}) + "/>"
                       for d in dirs if posixpath.dirname(d) == here)
        body += "".join(self.movie_xml(v) if sec == 1 else self.ep_xml(v)
                        for v, r in videos if posixpath.dirname(r) == here)
        return self.mc(h, body, librarySectionID=sec)

    # ------------------------------------------------------------- routing
    def send(self, h, code, body: bytes, ctype="text/xml;charset=utf-8", extra=None):
        h.send_response(code)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(body)

    def mc(self, h, inner="", **a):
        self.send(h, 200, f"<?xml version='1.0'?><MediaContainer {attrs(a)}>{inner}</MediaContainer>".encode())

    def handle(self, h, method):
        u = urllib.parse.urlsplit(h.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        path = u.path
        self.requests.append((method, h.path, dict(h.headers)))

        # plex.tv PIN flow
        if path == "/api/v2/pins" and method == "POST":
            return self.send(h, 201, json.dumps({"id": 42, "code": "abcd"}).encode(), "application/json")
        if path == "/api/v2/pins/42":
            self.pin_polls += 1
            tok = TOKEN if self.pin_polls >= 2 else None
            return self.send(h, 200, json.dumps({"id": 42, "authToken": tok}).encode(), "application/json")

        if path == "/identity":   # unauthenticated on a real server too
            if self.identity_redirect:
                return self.send(h, 302, b"", "text/plain", {"Location": "/"})
            mc = {"size": 0, "claimed": True, "machineIdentifier": self.identity_id}
            if self.identity_pad:
                mc["pad"] = "x" * self.identity_pad
            return self.send(h, 200, json.dumps({"MediaContainer": mc}).encode(), "application/json")

        token = h.headers.get("X-Plex-Token") or q.get("X-Plex-Token")
        if token != TOKEN:
            return self.send(h, 401, b"<html>401 Unauthorized</html>", "text/html")

        if path == "/redir" and self.redirect_to:
            return self.send(h, 302, b"", "text/plain", {"Location": self.redirect_to})
        if path == "/":
            if self.root_status:
                return self.send(h, self.root_status, b"<html>error</html>", "text/html")
            return self.mc(h, friendlyName="Home", machineIdentifier="srv1", version="1.41.0.0",
                           myPlex=1, platform="Linux")
        if path in ("/library", "/library/"):
            return self.mc(h, '<Directory key="sections" title="Library Sections"/>',
                           identifier="com.plexapp.plugins.library", title1="Plex Library")
        if path == "/library/sections":
            return self.mc(h,
                           f'<Directory key="1" type="movie" title="Movies" agent="tv.plex.agents.movie" '
                           f'uuid="u1"><Location id="1" path="{MOVIES}"/></Directory>'
                           f'<Directory key="2" type="show" title="TV Shows" agent="tv.plex.agents.series" '
                           f'uuid="u2"><Location id="2" path="{TV}"/></Directory>', size=2)
        m = re.fullmatch(r"/library/sections/(\d+)/all", path)
        if m:
            sec = m.group(1)
            items = [(x["title"], x["added"], self.movie_xml(x)) for x in self.movies] if sec == "1" \
                else [(self.show["title"], 0, self.show_xml())]
            if q.get("title"):
                items = [i for i in items if q["title"].lower() in i[0].lower()]
            order = q.get("sort", "titleSort")
            if order == "titleSort":
                items.sort()
            elif order == ("addedAt:desc,titleSort" if sec == "1" else "episode.addedAt:desc,titleSort"):
                items.sort(key=lambda i: (-i[1], i[0]))
            else:
                return self.send(h, 400, b"bad sort")
            items = [(t, x) for t, _, x in items]
            start = int(h.headers.get("X-Plex-Container-Start", 0))
            size = int(h.headers.get("X-Plex-Container-Size", len(items)))
            page = items[start:start + size]
            return self.mc(h, "".join(x for _, x in page), size=len(page), totalSize=len(items),
                           offset=start, librarySectionID=sec)
        if path == "/library/all" and "guid" in q:
            hits = [self.movie_xml(x) for x in self.movies if x["guid"] == q["guid"]]
            if q["guid"] == self.show["guid"]:
                hits.append(self.show_xml())
            return self.mc(h, "".join(hits), size=len(hits))
        m = re.fullmatch(r"/library/sections/(\d+)/folder", path)
        if m:
            return self.folder(h, int(m.group(1)), q.get("parent"))
        m = re.fullmatch(r"/library/metadata/(\d+(?:,\d+)+)", path)
        if m:   # several items in one request, each with its parts and streams
            out = []
            for rk in map(int, m.group(1).split(",")):
                out += [self.movie_xml(x, full=True) for x in self.movies if x["rk"] == rk]
                out += [self.ep_xml(e, full=True) for e in self.episodes if e["rk"] == rk]
            first = re.search(r'librarySectionID="(\d+)"', out[0]) if out else None  # as Plex does
            return self.mc(h, "".join(out), size=len(out), librarySectionID=first and first.group(1))
        m = re.fullmatch(r"/library/metadata/(\d+)(/allLeaves)?", path)
        if m:
            rk, leaves = int(m.group(1)), m.group(2)
            if rk in (210, 220):
                season = (rk - 200) // 10
                return self.mc(h, "<Directory " + attrs({
                    "ratingKey": rk, "key": f"/library/metadata/{rk}/children", "type": "season",
                    "title": f"Season {season}", "index": season, "parentRatingKey": 200,
                    "parentTitle": "Show A", "librarySectionID": 2}) + "/>", size=1, librarySectionID=2)
            if rk == 200 and leaves:
                return self.mc(h, "".join(self.ep_xml(e) for e in self.episodes),
                               size=len(self.episodes), librarySectionID=2)
            if rk == 200:
                return self.mc(h, self.show_xml(), size=1, librarySectionID=2)
            for x in self.movies:
                if x["rk"] == rk:
                    return self.mc(h, self.movie_xml(x, full=True), size=1, librarySectionID=1)
            for e in self.episodes:
                if e["rk"] == rk:
                    return self.mc(h, self.ep_xml(e, full=True), size=1, librarySectionID=2)
            return self.send(h, 404, b"<html>404</html>", "text/html")
        m = re.fullmatch(r"/library/parts/(\d+)/\d+/file\.mkv", path)
        if m and q.get("download") == "1":
            return self.serve_part(h, int(m.group(1)))
        m = re.fullmatch(r"/library/streams/(\d+)", path)
        if m:
            sid = int(m.group(1))
            for v in self.movies + self.episodes:
                for s in v["subs"] + [x for o in v.get("versions", []) for x in o["subs"]]:
                    if s["id"] == sid and s.get("body"):
                        return self.send(h, 200, s["body"].encode(), "text/plain")
            return self.send(h, 404, b"<html>404</html>", "text/html")
        if path == "/photo/:/transcode":
            if "missing" in q.get("url", ""):
                return self.send(h, 404, b"<html>404</html>", "text/html")
            return self.send(h, 200, b"\xff\xd8\xff\xe0fakejpeg", "image/jpeg")
        return self.send(h, 404, b"<html>404</html>", "text/html")

    def validator(self, pid) -> str:
        """What travelarr stores for this part's current version."""
        return f'"v{self.versions[pid]}-{pid}"|Wed, 07 Oct 2026 18:00:{self.versions[pid]:02d} GMT'

    def serve_part(self, h, pid):
        data = self.parts[pid]
        if pid in self.xml_instead:
            return self.mc(h, "", size=0)
        rng = h.headers.get("Range")
        ranged = bool(rng) and pid not in self.ignore_range
        if ranged and pid in self.range_416:
            return self.send(h, 416, b"<html>416</html>", "text/html")
        start = int(re.match(r"bytes=(\d+)-", rng).group(1)) if ranged else 0
        if ranged and start >= len(data):
            return self.send(h, 416, b"<html>416</html>", "text/html")
        if ranged and pid in self.bad_range_once:   # a wrong slice that starts past the resume point
            self.bad_range_once.discard(pid)
            start = min(start + 70_000, len(data) - 1)
        body = data[start:]
        extra = self.extra_bytes.pop(pid, 0) if ranged else 0
        open_ended = extra or (not ranged and pid in self.no_length)
        h.send_response(206 if ranged else 200)
        h.send_header("Content-Type", self.content_type.get(pid, "video/x-matroska"))
        if open_ended:   # close-delimited: the client gets every byte we send
            h.send_header("Connection", "close")
            h.close_connection = True
        else:
            h.send_header("Content-Length", str(len(body)))
        h.send_header("Accept-Ranges", "bytes")
        if pid not in self.no_etag:
            etag = f'"v{self.versions[pid]}-{pid}"'
            h.send_header("ETag", "W/" + etag if pid in self.weak_etag else etag)
        h.send_header("Last-Modified", f"Wed, 07 Oct 2026 18:00:{self.versions[pid]:02d} GMT")
        if pid in self.encoding:
            h.send_header("Content-Encoding", "gzip")
        if ranged:
            h.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        h.end_headers()
        cut = self.drop_after.pop(pid, None)
        if cut is None:
            cut = self.drop_every.get(pid)
        if cut is not None and cut < len(body):
            self.write_body(h, pid, body[:cut])
            if pid in self.replace_on_drop:
                self.parts[pid] = self.replace_on_drop.pop(pid)
                self.versions[pid] += 1
            h.close_connection = True
            try:
                h.connection.shutdown(2)
            except OSError:
                pass
            return
        self.write_body(h, pid, body + bytes(extra))

    def write_body(self, h, pid, data):
        try:
            rate = self.rate.get(pid)
            if not rate:
                h.wfile.write(data)
                return
            step = 16 * 1024
            for i in range(0, len(data), step):
                h.wfile.write(data[i:i + step])
                h.wfile.flush()
                time.sleep(step / rate)
        except OSError:
            pass   # the client went away


class FakeConn:
    """`uri` is what plex.tv advertises (https or http); `httpuri` is always http."""

    def __init__(self, uri, local=True, relay=False, httpuri=None):
        self.uri, self.httpuri = uri, httpuri or uri
        self.local, self.relay, self.ipv6 = local, relay, False


class FakeWatch:
    def __init__(self, typ, guid, title):
        self.type, self.guid, self.title, self.year = typ, guid, title, None
        self.thumb = "https://metadata-static.plex.tv/x.jpg"


class FakeAccount:
    """Stands in for plexapi's MyPlexAccount (plex.tv is not reachable in tests)."""

    def __init__(self, fake: FakePlex, session):
        self.fake, self.session, self.username = fake, session, "alien"

    def resources(self):
        from plexapi.myplex import MyPlexResource
        fake = self.fake

        class Res:
            name, clientIdentifier, provides, owned, presence = "Home", "srv1", "server", True, True
            accessToken = TOKEN
            httpsRequired = fake.https_required
            connections = fake.conns if fake.conns is not None else [FakeConn(fake.url)]
            # plexapi's own ordering, so tests see the real candidate order
            DEFAULT_LOCATION_ORDER = MyPlexResource.DEFAULT_LOCATION_ORDER
            DEFAULT_SCHEME_ORDER = MyPlexResource.DEFAULT_SCHEME_ORDER
            DEFAULT_IP_ORDER = MyPlexResource.DEFAULT_IP_ORDER
            preferred_connections = MyPlexResource.preferred_connections
        return [Res()]

    def watchlist(self):
        return [FakeWatch(*w) for w in self.fake.watchlist]
