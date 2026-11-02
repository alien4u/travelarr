"""
End-to-end: the real travelarr app (FastAPI + PlexGateway + Downloader)
against a fake Plex server over real HTTP, writing to a temp media folder.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import requests  # noqa: E402
import urllib3  # noqa: E402
from fastapi import testclient  # noqa: E402

from app import paths, plexgw, worker  # noqa: E402
from app import auth as auth_mod  # noqa: E402
from app import main as main_mod  # noqa: E402
from app import resync as resync_mod  # noqa: E402
from app import store as store_mod  # noqa: E402
from app.main import create_app  # noqa: E402
from app.plexgw import PlexGateway  # noqa: E402

HEAT = "Movies/Heat (1995)/Heat (1995).mkv"            # part 1001, 300,000 bytes, with subtitles
LOOSE = "Movies/Loose Movie.mkv"                        # part 1002, 120,000 bytes
HEATHERS = "Movies/Heathers (1989)/Heathers (1989).mkv"  # part 1003, 50,000 bytes
from tests.fakeplex import SRT as SRT_BODY  # noqa: E402
from tests.fakeplex import MOVIES, TOKEN, TV, FakeAccount, FakeConn, FakePlex  # noqa: E402


class TestClient(testclient.TestClient):
    """Sends the header every request from the page carries (see `api()` in app.js)."""

    def __init__(self, app, **kw):
        super().__init__(app, headers={"X-Requested-With": "travelarr"}, **kw)


class PlexFixture(unittest.TestCase):
    """The real app on a fake Plex server, writing to a temp media folder."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="travelarr2-")
        self.media = os.path.join(self.tmp, "media")
        os.makedirs(os.path.join(self.media, "TV-Shows"))
        os.makedirs(os.path.join(self.media, "Movies"))
        self.fake = FakePlex()

        def factory(store):
            gw = PlexGateway(store, "test")
            gw.plex_tv = self.fake.url
            gw.account = lambda: FakeAccount(self.fake, gw.session)
            return gw

        self.app = create_app(data_dir=os.path.join(self.tmp, "config"), media_root=self.media,
                              env={}, gateway_factory=factory, start_worker=False)
        self.store, self.gw, self.dl = self.app.state.store, self.app.state.gw, self.app.state.dl
        self.dl.backoff = 0.01
        self.dl.chunk = 16 * 1024
        self.c = TestClient(self.app)
        self.c.__enter__()

    def tearDown(self):
        self.c.__exit__(None, None, None)
        self.fake.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers ---------------------------------------------------------------
    def sign_in_and_connect(self):
        self.assertFalse(self.c.get("/api/state").json()["signedIn"])
        url = self.c.post("/api/auth/start").json()["url"]
        self.assertIn("app.plex.tv/auth", url)
        self.assertIn("code=abcd", url)
        self.assertFalse(self.c.post("/api/auth/check").json()["signedIn"])  # not approved yet
        self.assertTrue(self.c.post("/api/auth/check").json()["signedIn"])
        srv = self.c.get("/api/servers").json()["servers"]
        self.assertEqual(srv[0]["name"], "Home")
        r = self.c.post("/api/server", json={"id": "srv1", "name": "Home"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["conn"]["kind"], "local")

    def drain(self):
        while True:
            job = self.store.next_queued()
            if not job:
                return
            self.dl._process(job)

    def m(self, rel):
        return os.path.join(self.media, rel)

    def connect(self):
        return self.c.post("/api/connect")

    def choose(self, **body):
        return self.c.post("/api/server", json={"id": "srv1", "name": "Home", **body})

    def hanging_port(self):
        """A port that accepts TCP connections but never answers."""
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(8)
        self.addCleanup(s.close)
        return s, s.getsockname()[1]

    def trickle_port(self, mode):
        """A server that answers one byte at a time and never finishes, either inside
        the headers or inside a declared body."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        stop = threading.Event()
        head = b"HTTP/1.1 200 OK\r\n" if mode == "headers" else \
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n"

        def drip(c):
            try:
                c.sendall(head)
                while not stop.is_set():
                    c.sendall(b"x")
                    time.sleep(0.1)
            except OSError:
                pass
            finally:
                c.close()

        def serve():
            while not stop.is_set():
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(target=drip, args=(c,), daemon=True).start()
        threading.Thread(target=serve, daemon=True).start()
        self.addCleanup(srv.close)
        self.addCleanup(stop.set)
        return srv.getsockname()[1]

    @staticmethod
    def refused_ports(n):
        socks = [socket.socket() for _ in range(n)]
        for s in socks:
            s.bind(("127.0.0.1", 0))
        ports = [s.getsockname()[1] for s in socks]
        for s in socks:
            s.close()
        return ports

    def token_requests(self, since=0):
        return [r for r in self.fake.requests[since:]
                if "X-Plex-Token" in r[1] or any(k.lower() == "x-plex-token" for k in r[2])]

    @staticmethod
    def slots_free(wait=5.0):
        end = time.monotonic() + wait
        while True:
            got = 0
            while got < 32 and plexgw._probe_slots.acquire(blocking=False):
                got += 1
            for _ in range(got):
                plexgw._probe_slots.release()
            if got == 32 or time.monotonic() > end:
                return got == 32
            time.sleep(0.1)


class AppTest(PlexFixture):
    def test_full_flow(self):
        self.sign_in_and_connect()
        st = self.c.get("/api/state").json()
        self.assertEqual(st["user"], "alien")

        libs = self.c.get("/api/libraries").json()["libraries"]
        self.assertEqual([(l["key"], l["type"]) for l in libs], [("1", "movie"), ("2", "show")])

        movies = self.c.get("/api/library/1?start=0&size=2").json()
        self.assertEqual(movies["total"], 3)
        self.assertEqual([i["title"] for i in movies["items"]], ["Heat", "Heathers"])
        self.assertEqual(movies["items"][0]["size"], 300_000)
        page2 = self.c.get("/api/library/1?start=2&size=2").json()
        self.assertEqual([i["title"] for i in page2["items"]], ["Loose Movie"])
        found = self.c.get("/api/library/1?q=heathers").json()
        self.assertEqual([i["title"] for i in found["items"]], ["Heathers"])

        wl = self.c.get("/api/watchlist").json()["items"]
        self.assertEqual([(w["title"], w["inLibrary"]) for w in wl],
                         [("Heat", True), ("Show A", True), ("Not Owned", False)])

        show = self.c.get("/api/item/200").json()
        self.assertEqual([s["index"] for s in show["seasons"]], [1, 2])
        ep = show["seasons"][0]["episodes"][1]
        self.assertEqual(ep["dest"], "TV-Shows/Show A/Season 01/Show A - S01E02 - Second.mkv")
        self.assertFalse(ep["watched"])
        self.assertTrue(show["seasons"][0]["episodes"][0]["watched"])

        # Queue: the show (unwatched only) + Heat + a loose movie file
        r = self.c.post("/api/queue", json={"keys": ["200"], "mode": "unwatched"}).json()
        self.assertEqual(r["added"], 2)
        r = self.c.post("/api/queue", json={"keys": ["101", "102"], "source": "watchlist"}).json()
        self.assertEqual(r["added"], 2)
        r = self.c.post("/api/queue", json={"keys": ["101", "202"]}).json()
        self.assertEqual((r["added"], r["skipped"]), (0, 2))   # no duplicates
        self.assertEqual(r["warning"], f"Everything selected is already on {self.dl.device} or in the queue.")

        self.drain()

        for v in self.fake.movies[:2] + self.fake.episodes[1:]:
            rel = paths.dest_rel_for(v["file"], ["/data/Media/Movies", "/data/Media/TV-Shows"], "x")
            with open(self.m(rel), "rb") as fh:
                self.assertEqual(fh.read(), self.fake.parts[v["part"]], rel)
        self.assertFalse(os.path.exists(self.m("TV-Shows/Show A/Season 01/Show A - S01E01 - Pilot.mkv")))
        self.assertTrue(os.path.isfile(self.m("Movies/Loose Movie.mkv")))

        # Subtitles: external text subs written with Infuse/Plex naming,
        # embedded skipped, image-based skipped, fake XML body rejected.
        heat = "Movies/Heat (1995)/Heat (1995)"
        with open(self.m(heat + ".es.srt")) as fh:
            self.assertIn("Hola", fh.read())
        self.assertTrue(os.path.isfile(self.m(heat + ".en.forced.srt")))
        self.assertFalse(os.path.exists(self.m(heat + ".de.srt")))
        self.assertFalse(any(f.endswith(".fr.srt") for f in os.listdir(self.m("Movies/Heat (1995)"))))
        self.assertTrue(os.path.isfile(self.m("TV-Shows/Show A/Season 01/Show A - S01E02 - Second.es.srt")))

        hist = self.c.get("/api/jobs?view=history").json()["jobs"]
        self.assertEqual(len(hist), 4)
        self.assertTrue(all(j["status"] == "done" and j["finished_at"] for j in hist))
        heat_job = next(j for j in hist if j["title"] == "Heat")
        self.assertEqual((heat_job["subs_done"], heat_job["subs_total"]), (2, 4))
        self.assertIn("image-based", heat_job["error"])
        self.assertIn("not a subtitle", heat_job["error"])
        self.assertEqual(heat_job["source"], "watchlist")
        self.assertFalse([f for f in self._all_files() if f.endswith(paths.PART_SUFFIX)])

        # Badges
        movies = self.c.get("/api/library/1").json()["items"]
        self.assertEqual({i["title"]: i.get("copy") for i in movies}["Heat"], "done")
        shows = self.c.get("/api/library/2").json()["items"]
        self.assertEqual(shows[0]["copy"]["done"], 2)

        # Remove the show: files, its subs and now-empty folders go; roots and
        # files travelarr did not create stay.
        keep = self.m("TV-Shows/Show A/Season 01/notes-i-made.txt")
        with open(keep, "w") as fh:
            fh.write("mine")
        r = self.c.post("/api/remove", json={"showKeys": ["200"], "serverId": "srv1"}).json()
        self.assertEqual(r["results"], {"removed": 2})
        self.assertFalse(os.path.exists(self.m("TV-Shows/Show A/Season 01/Show A - S01E02 - Second.mkv")))
        self.assertFalse(os.path.exists(self.m("TV-Shows/Show A/Season 01/Show A - S01E02 - Second.es.srt")))
        self.assertFalse(os.path.exists(self.m("TV-Shows/Show A/Season 02")))   # emptied, removed
        self.assertTrue(os.path.isfile(keep))
        self.assertTrue(os.path.isdir(self.m("TV-Shows")))

        # Remove a loose movie: the Movies root must survive even though it is now empty-ish
        self.c.post("/api/remove", json={"ratingKeys": ["102"], "serverId": "srv1"})
        self.assertFalse(os.path.exists(self.m("Movies/Loose Movie.mkv")))
        self.assertTrue(os.path.isdir(self.m("Movies")))

        hist = self.c.get("/api/jobs?view=history").json()["jobs"]
        removed = [j for j in hist if j["status"] == "removed"]
        self.assertEqual(len(removed), 3)
        self.assertTrue(all(j["removed_at"] for j in removed))

        # Re-adding after removal is allowed and creates a new history row
        r = self.c.post("/api/queue", json={"keys": ["102"]}).json()
        self.assertEqual(r["added"], 1)

        # Thumbnails are proxied and cached
        r1 = self.c.get("/api/thumb?path=/library/metadata/101/thumb/1")
        self.assertEqual(r1.status_code, 200)
        self.assertTrue(r1.content.startswith(b"\xff\xd8"))
        before = len(self.fake.requests)
        self.c.get("/api/thumb?path=/library/metadata/101/thumb/1")
        self.assertEqual(len(self.fake.requests), before)
        self.assertEqual(self.c.get("/api/thumb?path=http://evil/x").status_code, 400)
        r = self.c.get("/api/thumb?path=/library/metadata/missing/thumb/1")   # Plex answers 404
        self.assertEqual(r.status_code, 502)
        self.assertNotIn(TOKEN, r.text)
        self.assertFalse(any(TOKEN in p for _m, p, _h in self.fake.requests if p.startswith("/photo/")))

    def _all_files(self):
        for root, _d, files in os.walk(self.media):
            for f in files:
                yield os.path.join(root, f)

    def test_resume_after_dropped_connection(self):
        self.sign_in_and_connect()
        self.fake.drop_after[1001] = 100_000
        self.c.post("/api/queue", json={"keys": ["101"]})
        self.drain()
        with open(self.m("Movies/Heat (1995)/Heat (1995).mkv"), "rb") as fh:
            self.assertEqual(fh.read(), self.fake.parts[1001])
        ranges = [r[2].get("Range") for r in self.fake.requests if "/library/parts/1001/" in r[1]]
        self.assertEqual(ranges[0], None)
        self.assertTrue(ranges[1] and ranges[1].startswith("bytes=") and ranges[1] != "bytes=0-", ranges)
        self.assertEqual(self.store.jobs("history")[0]["status"], "done")

    def test_server_ignoring_range_restarts_cleanly(self):
        self.sign_in_and_connect()
        self.fake.drop_after[1001] = 100_000
        self.fake.ignore_range.add(1001)
        self.c.post("/api/queue", json={"keys": ["101"]})
        self.drain()
        with open(self.m("Movies/Heat (1995)/Heat (1995).mkv"), "rb") as fh:
            self.assertEqual(fh.read(), self.fake.parts[1001])

    def test_xml_instead_of_file_fails_job(self):
        self.sign_in_and_connect()
        self.fake.xml_instead.add(1003)
        self.c.post("/api/queue", json={"keys": ["103"]})
        self.drain()
        j = self.store.jobs("history")[0]
        self.assertEqual(j["status"], "failed")
        self.assertIn("instead of the file", j["error"])
        self.assertFalse(os.path.exists(self.m("Movies/Heathers (1989)/Heathers (1989).mkv")))
        # retry after the server is fixed
        self.fake.xml_instead.clear()
        self.c.post(f"/api/jobs/{j['id']}/retry")
        self.drain()
        self.assertEqual(self.store.job(j["id"])["status"], "done")

    def test_show_with_selected_episodes_is_one_request(self):
        self.sign_in_and_connect()
        before = len(self.fake.requests)
        r = self.c.post("/api/queue", json={"keys": ["200"], "mode": "all", "only": ["201", "203"]}).json()
        self.assertEqual(r["added"], 2)
        titles = sorted(j["title"] for j in self.store.jobs("active"))
        self.assertEqual(titles, ["Pilot", "Third"])   # watched Pilot included because it was picked
        meta = [x for x in self.fake.requests[before:] if "/library/metadata/" in x[1]]
        self.assertEqual(len(meta), 2)                  # the show + its episode list, not one per episode

    def test_many_titles_in_one_request(self):
        self.sign_in_and_connect()
        keys = {"keys": ["101", "103", "200"], "mode": "all"}
        r = self.c.post("/api/queue", json=keys).json()
        self.assertEqual((r["added"], r["skipped"]), (5, 0))   # 2 movies + all 3 episodes
        self.assertEqual(r["bytes"], 300_000 + 50_000 + 80_000 + 90_000 + 70_000)
        r = self.c.post("/api/queue", json=keys).json()
        self.assertEqual((r["added"], r["skipped"]), (0, 5))
        self.assertEqual(r["warning"], f"Everything selected is already on {self.dl.device} or in the queue.")
        r = self.c.post("/api/queue", json={"keys": ["101", "102"]}).json()
        self.assertEqual((r["added"], r["skipped"]), (1, 1))

    def test_many_titles_with_unwatched_shows(self):
        self.sign_in_and_connect()
        r = self.c.post("/api/queue", json={"keys": ["102", "200"], "mode": "unwatched"}).json()
        self.assertEqual(r["added"], 3)
        self.assertEqual(sorted(j["title"] for j in self.store.jobs("active")), ["Loose Movie", "Second", "Third"])

    def test_queue_size_bound(self):
        self.sign_in_and_connect()
        before = len(self.fake.requests)
        r = self.c.post("/api/queue", json={"keys": [str(i) for i in range(1, main_mod.MAX_QUEUE_KEYS + 2)]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "Too many titles at once (at most 200)")
        self.assertEqual(len(self.fake.requests), before)   # refused before asking Plex
        r = self.c.post("/api/queue", json={"keys": ["101"] * main_mod.MAX_QUEUE_KEYS})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["added"], r.json()["skipped"]), (1, 199))

    def test_cancel_removes_partial(self):
        self.sign_in_and_connect()
        self.c.post("/api/queue", json={"keys": ["101"]})
        job = self.store.next_queued()
        self.dl.cancel(job["id"])
        self.dl._process(job)
        self.assertEqual(self.store.job(job["id"])["status"], "cancelled")
        self.assertFalse(os.path.exists(self.m("Movies/Heat (1995)/Heat (1995).mkv" + paths.PART_SUFFIX)))

    def test_space_reserve_blocks_download(self):
        self.sign_in_and_connect()
        self.c.post("/api/settings", json={"reserveGb": 10 ** 7})
        self.c.post("/api/queue", json={"keys": ["101"]})
        self.drain()
        j = self.store.jobs("history")[0]
        self.assertEqual(j["status"], "failed")
        self.assertIn("free space", j["error"])

    def test_recently_added(self):
        self.sign_in_and_connect()

        def sorts_sent(since):
            return {dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r[1]).query)).get("sort")
                    for r in self.fake.requests[since:] if re.search(r"/library/sections/\d+/all", r[1])}
        n = len(self.fake.requests)
        d = self.c.get("/api/library/1?sort=recent").json()
        self.assertEqual([(i["title"], i["addedAt"]) for i in d["items"]],    # Heat and Heathers tie: by title
                         [("Loose Movie", 1_700_000_300), ("Heat", 1_700_000_200), ("Heathers", 1_700_000_200)])
        self.assertEqual(d["total"], 3)
        self.assertEqual(sorts_sent(n), {"addedAt:desc,titleSort"})
        pages = [self.c.get(f"/api/library/1?sort=recent&start={s}&size=1").json()["items"] for s in range(3)]
        self.assertEqual([p[0]["title"] for p in pages], ["Loose Movie", "Heat", "Heathers"])
        found = self.c.get("/api/library/1?sort=recent&q=hea").json()["items"]
        self.assertEqual([i["title"] for i in found], ["Heat", "Heathers"])

        n = len(self.fake.requests)
        show = self.c.get("/api/library/2?sort=recent").json()["items"][0]
        self.assertEqual(sorts_sent(n), {"episode.addedAt:desc,titleSort"})   # newest episode, not the show
        self.assertNotIn("addedAt", show)
        n = len(self.fake.requests)
        self.c.get("/api/library/2")
        self.assertEqual(sorts_sent(n), {"titleSort"})

        r = self.c.get("/api/library/1?sort=bogus")
        self.assertEqual((r.status_code, r.json()["code"]), (400, "bad_request"))
        self.assertEqual([i["title"] for i in self.c.get("/api/library/1").json()["items"]],
                         ["Heat", "Heathers", "Loose Movie"])

    def test_existing_identical_file_is_adopted(self):
        self.sign_in_and_connect()
        dest = self.m("Movies/Heathers (1989)/Heathers (1989).mkv")
        os.makedirs(os.path.dirname(dest))
        with open(dest, "wb") as fh:
            fh.write(self.fake.parts[1003])
        self.c.post("/api/queue", json={"keys": ["103"]})
        self.drain()
        j = self.store.jobs("history")[0]
        self.assertEqual(j["status"], "done")
        self.assertEqual(j["note"], worker.ADOPTED.format(self.dl.device))
        self.assertFalse(any("/library/parts/1003/" in r[1] for r in self.fake.requests))

    def test_unreachable_server_reports_not_ready(self):
        self.sign_in_and_connect()
        real = self.fake.url
        self.fake.url = "http://127.0.0.1:9"   # nothing listens there
        self.gw.invalidate()
        r = self.c.get("/api/libraries")
        self.assertEqual(r.status_code, 409)
        self.assertIn("Cannot reach Home", r.json()["error"])
        # within the cool-down window it answers fast without retrying
        self.assertEqual(self.c.get("/api/watchlist").status_code, 409)
        self.fake.url = real
        self.assertEqual(self.c.post("/api/connect").status_code, 200)

    def test_not_signed_in(self):
        r = self.c.get("/api/libraries")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "not_ready")

    def test_ui_is_served(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("travelarr", r.text)
        self.assertEqual(r.headers["cache-control"], "no-cache")
        for asset in ("app.js", "style.css"):
            url = f"/static/{asset}?v={main_mod.VERSION}"
            self.assertIn(f'"{url}"', r.text)
            self.assertEqual(self.c.get(url).status_code, 200)

    # connection selection ----------------------------------------------------
    def test_probes_never_send_the_token(self):
        self.sign_in_and_connect()
        probes = [r for r in self.fake.requests if r[1].startswith("/identity")]
        self.assertTrue(probes)
        self.assertFalse([r for r in self.token_requests() if r[1].startswith("/identity")])

    def test_priority_fallback_to_remote(self):
        self.sign_in_and_connect()
        dead = self.refused_ports(1)[0]
        self.fake.conns = [FakeConn(f"http://127.0.0.1:{dead}"), FakeConn(self.fake.url, local=False)]
        conn = self.connect().json()["conn"]
        self.assertEqual((conn["kind"], conn["url"]), ("remote", self.fake.url))

    def test_fast_local_does_not_wait_for_slow_remote(self):
        self.sign_in_and_connect()
        hang, port = self.hanging_port()
        self.fake.conns = [FakeConn(self.fake.url), FakeConn(f"http://127.0.0.1:{port}", local=False)]
        t = time.monotonic()
        conn = self.connect().json()["conn"]
        self.assertLess(time.monotonic() - t, 1.5)
        self.assertEqual(conn["kind"], "local")
        hang.close()
        self.assertTrue(self.slots_free())

    def test_slow_local_waits_only_until_deadline(self):
        self.sign_in_and_connect()
        hang, port = self.hanging_port()
        self.fake.conns = [FakeConn(f"http://127.0.0.1:{port}"), FakeConn(self.fake.url, local=False)]
        with mock.patch.object(plexgw, "PROBE_READ", 30), mock.patch.object(plexgw, "PROBE_DEADLINE", 1.0):
            t = time.monotonic()
            conn = self.connect().json()["conn"]
            elapsed = time.monotonic() - t
        self.assertEqual(conn["kind"], "remote")
        self.assertTrue(0.8 <= elapsed <= 2.5, elapsed)
        hang.close()
        self.assertTrue(self.slots_free())

    def test_wrong_server_identity_is_rejected(self):
        self.sign_in_and_connect()
        self.fake.identity_id = "someone-else"
        before = len(self.fake.requests)
        r = self.connect()
        self.assertEqual((r.status_code, r.json()["code"]), (409, "not_ready"))
        self.assertEqual(self.token_requests(before), [])

    def test_identity_redirect_and_oversized_body_are_rejected(self):
        self.sign_in_and_connect()
        self.fake.identity_redirect = True
        self.assertEqual(self.connect().status_code, 409)
        self.fake.identity_redirect = False
        self.fake.identity_pad = 70_000
        self.assertEqual(self.connect().status_code, 409)
        self.fake.identity_pad = 0
        self.assertEqual(self.connect().status_code, 200)

    def test_transport_policy(self):
        self.sign_in_and_connect()
        self.fake.conns = [FakeConn("http://a:1"),
                           FakeConn("https://b:2", local=False, httpuri="http://b:2")]
        res = FakeAccount(self.fake, None).resources()[0]
        self.assertEqual(PlexGateway._auto_candidates(res), ["https://b:2", "http://a:1", "http://b:2"])
        self.fake.https_required = True
        res = FakeAccount(self.fake, None).resources()[0]
        self.assertEqual(PlexGateway._auto_candidates(res), ["https://b:2"])
        # end to end: an https-only server with only http addresses is never contacted
        self.fake.conns = None
        before = len(self.fake.requests)
        self.assertEqual(self.connect().status_code, 409)
        self.assertEqual(len(self.fake.requests), before)

    def test_fixed_address(self):
        self.sign_in_and_connect()
        port = self.fake.url.rsplit(":", 1)[1]
        self.assertEqual(self.choose(preferredUrl=self.fake.url).json()["conn"]["kind"], "manual")
        self.assertEqual(self.choose(preferredUrl=f"HTTP://127.0.0.1:{port}/").json()["conn"]["kind"], "manual")
        for bad in (self.fake.url + "/library", self.fake.url + "?x=1", f"http://u:p@127.0.0.1:{port}",
                    "http://127.0.0.1:99999"):
            with self.assertLogs("travelarr.plex", "WARNING"):
                self.assertEqual(self.choose(preferredUrl=bad).json()["conn"]["kind"], "local", bad)
        self.fake.https_required = True
        before = len(self.fake.requests)
        with self.assertLogs("travelarr.plex", "WARNING"):
            self.assertEqual(self.choose(preferredUrl=self.fake.url).status_code, 409)
        self.assertEqual(self.token_requests(before), [])

    def test_failure_after_probe_is_not_ready(self):
        self.sign_in_and_connect()
        self.fake.root_status = 500
        r = self.connect()
        self.assertEqual((r.status_code, r.json()["code"]), (409, "not_ready"))
        self.assertIn("answered at", r.json()["error"])

    def test_relay_never_gets_token_over_http(self):
        self.sign_in_and_connect()
        self.assertEqual(self.choose(allowRelay=True).status_code, 200)
        dead = self.refused_ports(1)[0]
        self.fake.conns = [FakeConn(f"http://127.0.0.1:{dead}"),
                           FakeConn(self.fake.url, local=False, relay=True)]
        before = len(self.fake.requests)
        self.assertEqual(self.connect().status_code, 409)
        self.assertEqual(self.token_requests(before), [])
        self.fake.conns = [FakeConn("https://r1:8443", local=False, relay=True, httpuri="http://r1:8443"),
                           FakeConn("http://r2:80", local=False, relay=True)]
        res = FakeAccount(self.fake, None).resources()[0]
        self.assertEqual(PlexGateway._relay_candidates(res), ["https://r1:8443"])

    def test_fixed_address_never_logged_raw(self):
        self.sign_in_and_connect()
        with self.assertLogs("travelarr.plex", "WARNING") as cm:
            r = self.choose(preferredUrl="https://user:s3cret@host.invalid:32400/p?token=abc#f")
        self.assertEqual(r.json()["conn"]["kind"], "local")
        out = "\n".join(cm.output)
        for secret in ("s3cret", "user:", "token=abc", "/p?"):
            self.assertNotIn(secret, out)
        self.assertIn("https://host.invalid:32400", out)
        with self.assertLogs("travelarr.plex", "WARNING") as cm:
            self.assertEqual(self.choose(preferredUrl="not a url").json()["conn"]["kind"], "local")
        self.assertIn("<unparseable address>", "\n".join(cm.output))
        self.assertNotIn("not a url", "\n".join(cm.output))

    def test_probe_saturation(self):
        self.sign_in_and_connect()
        self.assertTrue(self.slots_free())
        held = 0
        while plexgw._probe_slots.acquire(blocking=False):
            held += 1
        try:
            before = len(self.fake.requests)
            with self.assertLogs("travelarr.plex", "WARNING"):
                r = self.connect()
            self.assertEqual(r.status_code, 409)
            self.assertIn("still pending", r.json()["error"])
            self.assertEqual(len(self.fake.requests), before)
        finally:
            for _ in range(held):
                plexgw._probe_slots.release()
        self.assertEqual(self.connect().json()["conn"]["kind"], "local")

    def test_redirects_keep_token_safe(self):
        self.sign_in_and_connect()
        other = FakePlex()
        self.addCleanup(other.stop)
        self.fake.redirect_to = other.url + "/library/sections"
        self.gw.session.get(self.fake.url + "/redir", headers={"X-Plex-Token": TOKEN}, timeout=5)
        self.assertTrue(other.requests)
        self.assertFalse([r for r in other.requests if any(k.lower() == "x-plex-token" for k in r[2])])

        def target(url, location):
            r = requests.Response()
            r.url, r.status_code = url, 302
            r.headers["Location"] = location
            return self.gw.session.get_redirect_target(r)
        with self.assertLogs("travelarr.plex", "WARNING"):
            self.assertIsNone(target("https://a.example/x", "http://a.example/y"))
        self.assertEqual(target("https://a.example/x", "/y"), "/y")
        self.assertEqual(target("https://a.example/x", "https://b.example/y"), "https://b.example/y")

    def test_relay_candidate_cap(self):
        self.sign_in_and_connect()
        self.choose(allowRelay=True)
        self.fake.conns = [FakeConn(f"https://127.0.0.1:{p}", local=False, relay=True)
                           for p in self.refused_ports(5)]
        calls, real = [], plexgw._identity

        def spy(url, deadline, token=None):
            if token:
                calls.append(url)
            return real(url, deadline, token)
        with mock.patch.object(plexgw, "_identity", spy):
            r = self.connect()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(len(calls), 3)

    def test_relay_wall_clock(self):
        self.sign_in_and_connect()
        self.choose(allowRelay=True)
        hang, port = self.hanging_port()   # accepts TCP, never answers the TLS handshake
        self.fake.conns = [FakeConn(f"https://127.0.0.1:{port}", local=False, relay=True)]
        with mock.patch.object(plexgw, "RELAY_DEADLINE", 1.0):
            t = time.monotonic()
            r = self.connect()
            elapsed = time.monotonic() - t
        self.assertEqual(r.status_code, 409)
        self.assertTrue(0.8 <= elapsed <= 2.5, elapsed)
        self.assertTrue(self.slots_free(3))   # freed by the deadline, socket still open

    def test_trickling_peer_cannot_hold_probe_slots(self):
        self.sign_in_and_connect()
        for mode in ("headers", "body"):
            port = self.trickle_port(mode)
            self.fake.conns = [FakeConn(f"http://127.0.0.1:{port}"), FakeConn(self.fake.url, local=False)]
            with mock.patch.object(plexgw, "PROBE_DEADLINE", 1.0):
                t = time.monotonic()
                conn = self.connect().json()["conn"]
                elapsed = time.monotonic() - t
            self.assertEqual(conn["kind"], "remote", mode)
            self.assertTrue(0.8 <= elapsed <= 2.5, (mode, elapsed))
            self.assertTrue(self.slots_free(3), mode)   # the peer is still dripping

    def test_probe_connect_shares_deadline_across_addresses(self):
        class BlackHole:   # every connect attempt hangs for its whole timeout
            def __init__(self, *a):
                self.timeout = None

            def settimeout(self, t):
                self.timeout = t

            def connect(self, addr):
                time.sleep(self.timeout)
                raise TimeoutError("timed out")

            def close(self):
                pass
        five = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"10.0.0.{i}", 80)) for i in range(5)]
        fake_socket = types.SimpleNamespace(getaddrinfo=lambda *a, **kw: five, socket=BlackHole,
                                            SOCK_STREAM=socket.SOCK_STREAM, SHUT_RDWR=socket.SHUT_RDWR)
        with mock.patch.object(plexgw, "socket", fake_socket):
            t = time.monotonic()
            self.assertIsNone(plexgw._identity("http://five.invalid", time.monotonic() + 1.0))
        self.assertLess(time.monotonic() - t, 1.6)   # not 5 x PROBE_CONNECT

    def test_relay_token_rejected_is_auth_error(self):
        self.sign_in_and_connect()
        self.choose(allowRelay=True)
        self.fake.conns = [FakeConn("https://r1.invalid:8443", local=False, relay=True)]
        with mock.patch.object(plexgw, "_identity", lambda url, deadline, token=None: plexgw._Rejected()):
            r = self.connect()
        self.assertEqual((r.status_code, r.json()["code"]), (409, "auth"))

    def test_saturation_with_fixed_address(self):
        self.sign_in_and_connect()
        self.assertEqual(self.choose(preferredUrl=self.fake.url, allowRelay=True).json()["conn"]["kind"], "manual")
        self.fake.conns = []
        self.assertTrue(self.slots_free())
        held = 0
        while plexgw._probe_slots.acquire(blocking=False):
            held += 1
        try:
            with self.assertLogs("travelarr.plex", "WARNING"):
                r = self.connect()
            self.assertIn("still pending", r.json()["error"])
        finally:
            for _ in range(held):
                plexgw._probe_slots.release()
        self.assertEqual(self.connect().json()["conn"]["kind"], "manual")


class DownloadTest(PlexFixture):
    """Live speed and resume robustness."""

    def setUp(self):
        super().setUp()
        self.sign_in_and_connect()

    def read(self, rel):
        with open(self.m(rel), "rb") as fh:
            return fh.read()

    def part(self, rel):
        return self.m(rel) + paths.PART_SUFFIX

    def ranges(self, pid):
        return [r[2].get("Range") for r in self.fake.requests if f"/library/parts/{pid}/" in r[1]]

    def queue(self, rk):
        self.c.post("/api/queue", json={"keys": [rk]})
        return self.store.next_queued()["id"]

    def retry(self, jid):
        self.c.post(f"/api/jobs/{jid}/retry")
        self.drain()

    def fail_once_with_partial(self, rk="102", pid=1002, cut=100_000):
        """A failed job holding a validated partial: one drop, no retries."""
        jid = self.queue(rk)
        self.fake.drop_after[pid] = cut
        self.dl.max_attempts = 1
        self.drain()
        self.dl.max_attempts = worker.MAX_ATTEMPTS
        j = self.store.job(jid)
        self.assertEqual(j["status"], "failed")
        self.assertGreater(j["bytes_done"], worker.OVERLAP)
        return j

    # speed -------------------------------------------------------------------
    def test_rate_window(self):
        clock = [1000.0]
        job = {"id": 1, "kind": "movie", "title": "x", "year": None, "size": 100 << 20, "bytes_done": 0}
        with mock.patch.object(worker.time, "monotonic", lambda: clock[0]):
            dl = self.dl
            dl._begin(job)
            self.assertEqual(dl.snapshot()["speed"], 0.0)            # no time has passed yet
            for _ in range(30):
                clock[0] += 0.1
                dl._note(1_000_000)
            self.assertAlmostEqual(dl.snapshot()["speed"], 10_000_000, delta=500_000)
            for _ in range(20):
                clock[0] += 0.1
                dl._note(1_000_000)
            clock[0] += 2.5                                           # stalled for 2.5 s
            self.assertTrue(2_500_000 <= dl.snapshot()["speed"] <= 7_500_000)
            clock[0] += 3.0                                           # stalled longer than the window
            self.assertEqual(dl.snapshot()["speed"], 0.0)
            dl._begin(job)
            for _ in range(40):                                       # 40 MiB in 0.4 s: shown, not "starting"
                clock[0] += 0.01
                dl._note(1 << 20)
            self.assertGreater(dl.snapshot()["speed"], 0)
            dl.current = None

    def test_live_speed_and_states(self):
        pid = 1002
        data = bytes((i * 13) % 251 for i in range(6_000_000))
        self.fake.parts[pid] = data
        self.fake.movies[1]["size"] = len(data)
        self.fake.rate[pid] = 1_000_000
        self.fake.drop_after[pid] = 4_500_000
        self.dl.backoff = 1.0
        job = self.store.job(self.queue("102"))
        self.dl.transitions.clear()
        bad, seen, stop = [], [], threading.Event()

        def poll():   # also the concurrency check: never an exception, always coherent
            while not stop.is_set():
                try:
                    s = self.dl.snapshot()
                except Exception as e:  # noqa: BLE001
                    bad.append(repr(e))
                    return
                if s:
                    if not (s["state"] in ("connecting", "downloading", "retrying", "finishing")
                            and 0 <= s["bytes"] <= s["size"] and s["speed"] >= 0
                            and (s["retry_in"] is not None) == (s["state"] == "retrying")):
                        bad.append(s)
                    seen.append((time.monotonic(), s))
                time.sleep(0.01)
        poller = threading.Thread(target=poll, daemon=True)
        t0 = time.monotonic()
        poller.start()
        self.dl._process(job)
        stop.set()
        poller.join()
        self.assertEqual(bad, [])
        self.assertEqual(self.read(LOOSE), data)
        mid = [s for t, s in seen if 2 <= t - t0 <= 4]
        self.assertTrue(mid)
        for s in mid:
            self.assertEqual(s["state"], "downloading")
            self.assertTrue(600_000 <= s["speed"] <= 1_400_000, s["speed"])
        it = iter(self.dl.transitions)
        want = ["connecting", "downloading", "retrying", "connecting", "downloading", "finishing"]
        self.assertTrue(all(x in it for x in want), list(self.dl.transitions))
        retrying = [s for _, s in seen if s["state"] == "retrying"]
        self.assertTrue(retrying)
        self.assertTrue(all(0 <= s["retry_in"] <= 1 for s in retrying))
        speeds = [s["speed"] for s in retrying]
        self.assertTrue(all(b <= a for a, b in zip(speeds, speeds[1:])), speeds)

    # resume validation ---------------------------------------------------------
    def test_resume_rejects_wrong_range(self):
        self.fake.drop_after[1002] = 100_000
        self.fake.bad_range_once.add(1002)
        self.queue("102")
        self.drain()
        self.assertEqual(self.read(LOOSE), self.fake.parts[1002])
        # refused before writing, then resumed at the same point: no damage to repair later
        r = self.ranges(1002)
        self.assertEqual(len(r), 3, r)
        self.assertIsNone(r[0])
        self.assertTrue(r[1] and r[1] == r[2], r)

    def test_same_size_change_restarts_from_zero(self):
        # changed only in the first 1000 bytes: the overlap check alone would not notice
        new = bytes(b ^ 0xFF for b in self.fake.parts[1002][:1000]) + self.fake.parts[1002][1000:]
        self.fake.drop_after[1002] = 100_000
        self.fake.replace_on_drop[1002] = new
        self.queue("102")
        with self.assertLogs("travelarr.worker", "WARNING") as cm:
            self.drain()
        self.assertIn("again from the beginning", "\n".join(cm.output))
        self.assertEqual(self.read(LOOSE), new)
        self.assertEqual(self.ranges(1002)[-1], None)     # a fresh request after the ranged one

    def test_changed_size_fails_then_retry_queues_again(self):
        new = self.fake.parts[1002] + b"more"
        self.fake.drop_after[1002] = 100_000
        self.fake.replace_on_drop[1002] = new
        jid = self.queue("102")
        self.drain()
        j = self.store.job(jid)
        self.assertEqual(j["status"], "failed")
        self.assertTrue(j["error"].startswith(worker.CHANGED), j["error"])
        self.assertFalse(os.path.lexists(self.part(LOOSE)))
        self.assertEqual((j["bytes_done"], j["validator"]), (0, None))
        self.fake.movies[1]["size"] = len(new)            # what Plex now reports
        self.assertEqual(self.c.post(f"/api/jobs/{jid}/retry").json()["requeued"], 1)
        j = self.store.job(jid)  # the same row, now for the file Plex has
        self.assertEqual((j["status"], j["note"], j["size"]), ("queued", "queued again: the file changed on Plex",
                                                                len(new)))
        self.drain()
        self.assertEqual(self.read(LOOSE), new)
        self.assertEqual(self.store.job(jid)["status"], "done")

    def test_416_on_resume_means_changed(self):
        self.fake.drop_after[1002] = 100_000
        self.fake.range_416.add(1002)
        jid = self.queue("102")
        self.drain()
        self.assertTrue(self.store.job(jid)["error"].startswith(worker.CHANGED))
        self.assertFalse(os.path.lexists(self.part(LOOSE)))

    def test_recovered_drops_do_not_exhaust_retries(self):
        self.fake.drop_every[1001] = 100_000
        self.dl.max_attempts = 3
        jid = self.queue("101")
        self.drain()
        self.assertEqual(self.store.job(jid)["status"], "done")
        self.assertEqual(self.read(HEAT), self.fake.parts[1001])
        self.assertGreater(len(self.ranges(1001)), 3)

    def test_repeated_unvalidated_drops_give_up(self):
        self.fake.no_etag.add(1002)
        self.fake.drop_every[1002] = 100_000
        self.dl.max_attempts = 3
        jid = self.queue("102")
        self.drain()
        self.assertIn("gave up after 3 attempts", self.store.job(jid)["error"])
        self.assertEqual(self.ranges(1002), [None, None, None])

    def test_unvalidated_download_is_never_resumed(self):
        self.fake.no_etag.add(1002)
        self.fake.drop_after[1002] = 100_000
        self.fake.weak_etag.add(1001)
        self.fake.drop_after[1001] = 100_000
        self.queue("102")
        self.queue("101")
        self.drain()
        self.assertEqual(self.ranges(1002), [None, None])
        self.assertEqual(self.ranges(1001), [None, None])
        self.assertEqual(self.read(LOOSE), self.fake.parts[1002])
        self.assertEqual(self.read(HEAT), self.fake.parts[1001])

    def test_encoded_body_is_refused(self):
        self.fake.encoding.add(1002)
        jid = self.queue("102")
        self.drain()
        self.assertIn("encoded", self.store.job(jid)["error"])

    def test_excess_body_never_grows_partial_past_size(self):
        size = 120_000
        self.fake.drop_after[1002] = 100_000
        self.fake.extra_bytes[1002] = 5000
        real = os.pwrite
        over = []

        def pwrite(fd, data, off):
            if off + len(data) > size:
                over.append((off, len(data)))
            return real(fd, data, off)
        self.queue("102")
        with mock.patch.object(worker.os, "pwrite", pwrite), self.assertLogs("travelarr.worker", "INFO") as cm:
            self.drain()
        self.assertEqual(over, [])
        self.assertIn("more data than", "\n".join(cm.output))
        self.assertEqual(self.ranges(1002)[-1], f"bytes={size - worker.OVERLAP}-")
        self.assertEqual(self.read(LOOSE), self.fake.parts[1002])

    def test_short_writes_are_completed(self):
        real = os.pwrite
        self.queue("101")
        with mock.patch.object(worker.os, "pwrite", lambda fd, data, off: real(fd, bytes(data[:1000]), off)):
            self.drain()
        self.assertEqual(self.read(HEAT), self.fake.parts[1001])

    def test_unknown_size_is_learned_before_writing(self):
        job = self.gw.plan(["102"])[0]
        job["size"] = 0
        self.store.add_jobs([job])
        jid = self.store.next_queued()["id"]
        order, sizes = [], []
        real_update, real_pwrite, real_note = self.store.update, os.pwrite, self.dl._note

        def update(i, **f):
            if "size" in f:
                order.append(("size", f["size"]))
            return real_update(i, **f)

        def pwrite(fd, data, off):
            order.append(("write",))
            return real_pwrite(fd, data, off)

        def note(n):
            real_note(n)
            sizes.append(self.dl.snapshot()["size"])
        with mock.patch.object(self.store, "update", update), mock.patch.object(worker.os, "pwrite", pwrite), \
                mock.patch.object(self.dl, "_note", note):
            self.drain()
        self.assertEqual(order[0], ("size", 120_000))
        self.assertTrue(sizes and all(s == 120_000 for s in sizes))
        self.assertEqual(self.store.job(jid)["size"], 120_000)
        self.assertEqual(self.read(LOOSE), self.fake.parts[1002])
        # an answer that reveals no size at all
        job = self.gw.plan(["103"])[0]
        job["size"] = 0
        self.store.add_jobs([job])
        self.fake.no_length.add(1003)
        self.drain()
        self.assertIn("did not report the file size", self.store.jobs("history")[0]["error"])

    # partial files on disk -----------------------------------------------------
    def test_corrupt_tail_is_detected_on_resume(self):
        j = self.fail_once_with_partial()
        with open(self.part(LOOSE), "r+b") as fh:
            fh.seek(j["bytes_done"] - 10)
            b = fh.read(1)
            fh.seek(-1, 1)
            fh.write(bytes([b[0] ^ 0xFF]))
        with self.assertLogs("travelarr.worker", "WARNING"):
            self.retry(j["id"])
        self.assertEqual(self.read(LOOSE), self.fake.parts[1002])

    def test_unconfirmed_tail_is_discarded(self):
        j = self.fail_once_with_partial()
        with open(self.part(LOOSE), "ab") as fh:
            fh.write(b"\0" * 5000)                  # what a power cut can leave past the last fsync
        before = len(self.ranges(1002))
        self.retry(j["id"])
        self.assertEqual(self.ranges(1002)[before], f"bytes={j['bytes_done'] - worker.OVERLAP}-")
        self.assertEqual(self.read(LOOSE), self.fake.parts[1002])

    def test_complete_partial_is_verified_before_finalize(self):
        jid = self.queue("102")
        os.makedirs(os.path.dirname(self.part(LOOSE)), exist_ok=True)
        with open(self.part(LOOSE), "wb") as fh:
            fh.write(self.fake.parts[1002])
        self.store.update(jid, bytes_done=120_000, validator=self.fake.validator(1002))
        self.drain()
        self.assertEqual(self.ranges(1002), [f"bytes={120_000 - worker.OVERLAP}-"])
        self.assertEqual(self.store.job(jid)["status"], "done")
        self.assertEqual(self.read(LOOSE), self.fake.parts[1002])

    def test_complete_partial_with_bad_tail_restarts(self):
        jid = self.queue("102")
        os.makedirs(os.path.dirname(self.part(LOOSE)), exist_ok=True)
        bad = bytearray(self.fake.parts[1002])
        bad[-100] ^= 0xFF
        with open(self.part(LOOSE), "wb") as fh:
            fh.write(bad)
        self.store.update(jid, bytes_done=120_000, validator=self.fake.validator(1002))
        with self.assertLogs("travelarr.worker", "WARNING"):
            self.drain()
        self.assertEqual(self.ranges(1002), [f"bytes={120_000 - worker.OVERLAP}-", None])
        self.assertEqual(self.read(LOOSE), self.fake.parts[1002])

    def test_legacy_partial_is_not_trusted(self):
        jid = self.queue("102")
        os.makedirs(os.path.dirname(self.part(LOOSE)), exist_ok=True)
        with open(self.part(LOOSE), "wb") as fh:
            fh.write(self.fake.parts[1002][:80_000])
        self.store.update(jid, bytes_done=80_000)      # written by a version without validators
        self.drain()
        self.assertEqual(self.ranges(1002), [None])
        self.assertEqual(self.read(LOOSE), self.fake.parts[1002])

    def test_old_database_gains_validator_column(self):
        path = os.path.join(self.tmp, "old.db")
        old = re.sub(r",\n\s*validator[^\n]*", "", store_mod.SCHEMA)
        self.assertNotIn("validator", old)
        db = sqlite3.connect(path)
        db.executescript(old)
        db.execute("INSERT INTO jobs(rating_key, kind, title, part_id, part_key, dest_rel, status) "
                   "VALUES('1', 'movie', 't', 1, '/k', 'M/x.mkv', 'done')")
        db.commit()
        db.close()
        s = store_mod.Store(path)
        self.assertEqual((s.job(1)["title"], s.job(1)["validator"]), ("t", None))
        s.close()
        store_mod.Store(path).close()                   # a second open changes nothing

    def test_lost_rename_is_found_at_startup(self):
        jid = self.queue("102")
        self.drain()
        os.replace(self.m(LOOSE), self.part(LOOSE))     # the rename did not survive a power cut
        self.dl._reconcile_done()
        self.assertEqual(self.store.job(jid)["status"], "queued")
        before = len(self.ranges(1002))
        self.drain()
        self.assertEqual(self.ranges(1002)[before:], [f"bytes={120_000 - worker.OVERLAP}-"])
        self.assertEqual(self.read(LOOSE), self.fake.parts[1002])

    def test_partial_wins_over_same_size_final(self):
        j = self.fail_once_with_partial()
        with open(self.m(LOOSE), "wb") as fh:
            fh.write(b"x" * 120_000)                    # same name and size, other content
        self.retry(j["id"])
        self.assertNotEqual(self.store.job(j["id"])["note"], worker.ADOPTED.format(self.dl.device))
        self.assertEqual(self.read(LOOSE), self.fake.parts[1002])

    def test_stale_final_is_replaced_by_surviving_partial(self):
        jid = self.queue("102")
        self.drain()
        os.replace(self.m(LOOSE), self.part(LOOSE))     # the completed partial survived a power cut...
        with open(self.m(LOOSE), "wb") as fh:
            fh.write(b"old" * 40_000)                   # ...and so did an older file at the final path
        self.dl._reconcile_done()
        self.assertEqual(self.store.job(jid)["status"], "queued")
        self.drain()
        self.assertEqual(self.read(LOOSE), self.fake.parts[1002])
        self.assertFalse(os.path.lexists(self.part(LOOSE)))

    def test_adopted_file_shows_complete_while_finishing(self):
        os.makedirs(os.path.dirname(self.m(LOOSE)), exist_ok=True)
        with open(self.m(LOOSE), "wb") as fh:
            fh.write(self.fake.parts[1002])
        seen, real = [], self.dl._subtitles

        def subtitles(job):
            seen.append(self.dl.snapshot())
            return real(job)
        self.queue("102")
        with mock.patch.object(self.dl, "_subtitles", subtitles):
            self.drain()
        self.assertEqual((seen[0]["state"], seen[0]["bytes"], seen[0]["size"]), ("finishing", 120_000, 120_000))

    def test_mixed_case_html_is_refused(self):
        self.fake.content_type[1002] = "Text/HTML; charset=UTF-8"
        jid = self.queue("102")
        self.drain()
        self.assertIn("instead of the file", self.store.job(jid)["error"])

    def test_missing_length_on_known_size_means_changed(self):
        self.fake.no_length.add(1002)
        jid = self.queue("102")
        self.drain()
        j = self.store.job(jid)
        self.assertTrue(j["error"].startswith(worker.CHANGED), j["error"])
        self.assertFalse(os.path.lexists(self.part(LOOSE)))

    def test_retry_reasons_are_short(self):
        timeout = requests.ConnectionError(urllib3.exceptions.ReadTimeoutError(None, "/x", "Read timed out."))
        self.assertEqual(worker._why(timeout), "Plex stopped sending data")
        self.assertEqual(worker._why(requests.exceptions.ChunkedEncodingError("x")), "the connection dropped mid-file")
        self.assertEqual(worker._why(requests.ConnectionError("stream ended early at 1 of 2 bytes")),
                         "stream ended early at 1 of 2 bytes")

    # durability ------------------------------------------------------------------
    def test_fsync_before_progress_is_recorded(self):
        events = []
        real_fsync, real_pwrite, real_replace, real_update = os.fsync, os.pwrite, os.replace, self.store.update

        def fsync(fd):
            events.append(("fsync",))
            return real_fsync(fd)

        def pwrite(fd, data, off):
            events.append(("write",))
            return real_pwrite(fd, data, off)

        def replace(a, b):
            events.append(("replace",))
            return real_replace(a, b)

        def update(i, **f):
            if f.get("bytes_done"):
                events.append(("progress", f["bytes_done"]))
            return real_update(i, **f)
        self.fake.drop_after[1001] = 150_000
        self.queue("101")
        with mock.patch.object(worker, "CHECKPOINT_BYTES", 40_000), mock.patch.object(worker.os, "fsync", fsync), \
                mock.patch.object(worker.os, "pwrite", pwrite), mock.patch.object(worker.os, "replace", replace), \
                mock.patch.object(self.store, "update", update):
            self.drain()
        self.assertEqual(self.read(HEAT), self.fake.parts[1001])
        dirty, progress = False, 0
        for e in events:
            if e[0] == "write":
                dirty = True
            elif e[0] == "fsync":
                dirty = False
            else:
                self.assertFalse(dirty, e)
                progress += e[0] == "progress"
        self.assertGreaterEqual(progress, 3)
        self.assertIn(("replace",), events)

    def test_fsync_failure_keeps_last_confirmed_progress(self):
        calls, real = [0], os.fsync

        def fsync(fd):
            calls[0] += 1
            if calls[0] == 2:
                raise OSError(5, "I/O error")
            return real(fd)
        jid = self.queue("101")
        with mock.patch.object(worker, "CHECKPOINT_BYTES", 40_000), mock.patch.object(worker.os, "fsync", fsync):
            self.drain()
        j = self.store.job(jid)
        self.assertEqual(j["status"], "failed")
        self.assertIn("could not confirm data on disk", j["error"])
        self.assertTrue(0 < j["bytes_done"] <= 40_000 + self.dl.chunk)     # the first checkpoint
        self.assertTrue(os.path.exists(self.part(HEAT)))
        self.retry(jid)
        self.assertEqual(self.read(HEAT), self.fake.parts[1001])

    def test_progress_store_failure_is_sticky(self):
        n, later, real = [0], [], self.store.update

        def update(i, **f):
            if f.get("bytes_done"):
                n[0] += 1
                if n[0] == 2:
                    raise sqlite3.OperationalError("disk I/O error")
                if n[0] > 2:
                    later.append(f)
            return real(i, **f)
        jid = self.queue("101")
        with mock.patch.object(worker, "CHECKPOINT_BYTES", 40_000), mock.patch.object(self.store, "update", update):
            self.drain()
        j = self.store.job(jid)
        self.assertIn("could not record progress", j["error"])
        self.assertTrue(0 < j["bytes_done"] <= 40_000 + self.dl.chunk)
        self.assertEqual(later, [])
        self.retry(jid)
        self.assertEqual(self.read(HEAT), self.fake.parts[1001])

    def test_disk_error_wins_over_network_error(self):
        self.fake.drop_after[1002] = 100_000
        jid = self.queue("102")

        def fsync(fd):
            raise OSError(5, "I/O error")
        with mock.patch.object(worker, "CHECKPOINT_BYTES", 10 ** 12), \
                mock.patch.object(worker, "CHECKPOINT_SECS", 10 ** 6), mock.patch.object(worker.os, "fsync", fsync):
            self.drain()
        j = self.store.job(jid)
        self.assertIn("could not confirm data on disk", j["error"])
        self.assertEqual(len(self.ranges(1002)), 1)     # not retried
        self.assertEqual(j["bytes_done"], 0)

    # links -----------------------------------------------------------------------
    def test_links_never_redirect_writes(self):
        outside = os.path.join(self.tmp, "outside.bin")
        with open(outside, "wb") as fh:
            fh.write(b"precious" * 15_000)              # 120,000 bytes, same size as the movie
        precious = open(outside, "rb").read()
        os.makedirs(os.path.dirname(self.m(LOOSE)), exist_ok=True)
        for make in (os.symlink, os.link):              # a planted partial
            make(outside, self.part(LOOSE))
            jid = self.queue("102")
            self.drain()
            self.assertEqual(self.read(LOOSE), self.fake.parts[1002])
            self.assertFalse(os.path.islink(self.m(LOOSE)))
            self.assertEqual(open(outside, "rb").read(), precious)
            self.c.post(f"/api/jobs/{jid}/remove")
        os.symlink(outside, self.m(LOOSE))              # a planted final of the right size
        jid = self.queue("102")
        self.drain()
        self.assertNotEqual(self.store.job(jid)["note"], worker.ADOPTED.format(self.dl.device))
        self.assertFalse(os.path.islink(self.m(LOOSE)))
        self.assertEqual(self.read(LOOSE), self.fake.parts[1002])
        self.assertEqual(open(outside, "rb").read(), precious)

    def test_subtitle_links_never_redirect_writes(self):
        outside = os.path.join(self.tmp, "outside.srt")
        with open(outside, "wb") as fh:
            fh.write(b"precious")
        base = "Movies/Heat (1995)/Heat (1995)"
        os.makedirs(os.path.dirname(self.m(HEAT)), exist_ok=True)
        os.symlink(outside, self.m(base + ".es.srt") + paths.PART_SUFFIX)    # a planted temp file
        os.symlink(outside, self.m(base + ".en.forced.srt"))                 # a planted sidecar
        self.queue("101")
        self.drain()
        self.assertEqual(open(outside, "rb").read(), b"precious")
        for rel in (base + ".es.srt", base + ".en.forced.srt"):
            self.assertFalse(os.path.islink(self.m(rel)), rel)
        with open(self.m(base + ".es.srt")) as fh:
            self.assertIn("Hola", fh.read())


    # cancel vs. claim ---------------------------------------------------------
    def request_after_claim(self, action="cancel"):
        """Run the queue, sending a request right after the worker claims each job."""
        real, seen = self.store.claim, {}

        def claim(job_id, *a):
            ok = real(job_id, *a)
            seen["result"] = self.c.post(f"/api/jobs/{job_id}/{action}").json()
            seen["status"] = self.store.job(job_id)["status"]
            return ok
        with mock.patch.object(self.store, "claim", claim):
            self.drain()
        return seen

    def test_cancel_before_claim(self):
        jid = self.queue("102")
        job = self.store.next_queued()
        self.c.post(f"/api/jobs/{jid}/cancel")
        self.dl._process(job)
        self.assertEqual(self.store.job(jid)["status"], "cancelled")
        self.assertEqual(self.ranges(1002), [])

    def test_cancel_right_after_claim(self):
        jid = self.queue("102")
        self.assertEqual(self.request_after_claim()["status"], "downloading")
        self.assertEqual(self.store.job(jid)["status"], "cancelled")
        self.assertEqual(self.ranges(1002), [])
        self.assertFalse(os.path.lexists(self.part(LOOSE)))

    def test_cancel_right_after_claim_keeps_existing_file(self):
        with open(self.m(LOOSE), "wb") as fh:
            fh.write(self.fake.parts[1002])
        jid = self.queue("102")
        self.request_after_claim()
        self.assertEqual(self.store.job(jid)["status"], "cancelled")
        self.assertEqual(self.read(LOOSE), self.fake.parts[1002])

    def test_cancel_just_before_the_rename(self):
        jid = self.queue("102")
        real = self.dl._fsync

        def fsync(fd):
            real(fd)
            if os.fstat(fd).st_size == 120_000:
                self.c.post(f"/api/jobs/{jid}/cancel")
        with mock.patch.object(self.dl, "_fsync", fsync):
            self.drain()
        self.assertEqual(self.store.job(jid)["status"], "cancelled")
        self.assertFalse(os.path.lexists(self.m(LOOSE)))
        self.assertFalse(os.path.lexists(self.part(LOOSE)))

    def subtitles_hook(self, fn):
        real = self.gw.subtitle_streams

        def subs(*a):
            fn()
            return real(*a)
        return mock.patch.object(self.gw, "subtitle_streams", subs)

    def test_cancel_after_the_last_check_finishes(self):
        jid = self.queue("101")
        with self.subtitles_hook(lambda: self.c.post(f"/api/jobs/{jid}/cancel")):
            self.drain()
        self.assertEqual(self.store.job(jid)["status"], "done")
        self.assertNotIn(jid, self.dl._cancel)
        self.assertEqual(self.read(HEAT), self.fake.parts[1001])

    def test_remove_while_finishing_reports_busy(self):
        # the video is in place: removal waits for the job to finish instead of racing it
        jid, got = self.queue("101"), {}
        with self.subtitles_hook(lambda: got.update(self.c.post(f"/api/jobs/{jid}/remove").json())):
            self.drain()
        self.assertEqual(got, {"result": "busy"})
        self.assertEqual(self.store.job(jid)["status"], "done")
        self.assertEqual(self.read(HEAT), self.fake.parts[1001])


class FilesTest(PlexFixture):
    """Local Files: browsing the media folder and deleting inside library folders."""

    def setUp(self):
        super().setUp()
        self.outside = tempfile.mkdtemp(prefix="travelarr-outside-")
        self.addCleanup(shutil.rmtree, self.outside, True)

    def write(self, rel, data=b"x"):
        p = self.m(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def ls(self, path="", **params):
        return self.c.get("/api/files", params={"path": path, **params})

    def names(self, path=""):
        r = self.ls(path)
        self.assertEqual(r.status_code, 200, r.text)
        return [e["name"] for e in r.json()["entries"]]

    def rm(self, *rels):
        return self.c.post("/api/files/delete", json={"paths": list(rels)})

    @contextlib.contextmanager
    def mode(self, rel, mode):
        if os.geteuid() == 0:
            self.skipTest("file modes do not stop root")
        p = self.m(rel)
        os.makedirs(p, exist_ok=True)
        os.chmod(p, mode)
        try:
            yield p
        finally:
            os.chmod(p, 0o755)

    def test_listing(self):
        self.write("TV-Shows/Show A/Season 1/e1.mkv", b"x" * 100)
        self.write("TV-Shows/Show A/Season 2/e1.mkv", b"x" * 50)
        self.write("TV-Shows/Show A/Season 10/e1.mkv", b"x" * 10)
        self.write("TV-Shows/Show A/.DS_Store", b"x" * 7)
        self.write("TV-Shows/Show A/notes.txt", b"x" * 3)
        self.write("Movies/Heat (1995)/Heat (1995).mkv" + paths.PART_SUFFIX, b"x" * 5)
        root = self.ls().json()
        self.assertEqual([(e["name"], e["dir"], e["deletable"]) for e in root["entries"]],
                         [("Movies", True, False), ("TV-Shows", True, False)])
        self.assertEqual((root["path"], root["total"], root["offset"]), ("", 2, 0))
        self.assertIsNotNone(root["disk"])
        show = self.ls("TV-Shows").json()["entries"][0]
        self.assertEqual((show["path"], show["size"], show["files"], show["complete"], show["deletable"]),
                         ("TV-Shows/Show A", 170, 5, True, True))          # the hidden file counts
        self.assertEqual(self.names("TV-Shows/Show A"), ["Season 1", "Season 2", "Season 10", "notes.txt"])
        notes = self.ls("TV-Shows/Show A").json()["entries"][-1]
        self.assertEqual((notes["dir"], notes["size"], notes["files"], notes["partial"]), (False, 3, 1, False))
        self.assertTrue(self.ls("Movies/Heat (1995)").json()["entries"][0]["partial"])
        self.assertEqual(self.ls("TV-Shows/./Show A/").json()["path"], "TV-Shows/Show A")
        os.symlink(self.m("TV-Shows/Show A"), self.m("Movies/Inside"))
        inside = self.ls("Movies").json()["entries"][-1]
        self.assertEqual((inside["name"], inside["dir"], inside["link"]), ("Inside", False, True))
        self.assertEqual(self.names("Movies/Inside"), ["Season 1", "Season 2", "Season 10", "notes.txt"])

    def test_listing_refusals(self):
        os.makedirs(os.path.join(self.outside, "sub"))
        os.symlink(self.outside, self.m("Movies/out"))
        self.write("Movies/a.mkv")
        for bad in ("../x", "/etc", "Movies/../../x", "Movies/out", "Movies/out/sub", "Movies/a\x00"):
            self.assertEqual(self.ls(bad).status_code, 400, bad)
        r = self.ls("Movies/a.mkv")
        self.assertEqual((r.status_code, r.json()["code"]), (400, "not_folder"))
        r = self.ls("Movies/nope")
        self.assertEqual((r.status_code, r.json()["code"]), (404, "missing"))
        for params in ({"offset": "-1"}, {"limit": "0"}, {"limit": "501"}, {"limit": "x"}, {"offset": "1.5"},
                       {"offset": "9" * 5000}, {"limit": "0" * 5000 + "1"}):
            r = self.ls("Movies", **params)
            self.assertEqual((r.status_code, r.json()["code"]), (400, "bad_request"), params)
        self.assertEqual(self.ls("Movies", limit="500").status_code, 200)
        r = self.ls("Movies", offset="99").json()
        self.assertEqual((r["entries"], r["total"]), ([], 2))
        os.rename(self.media, self.media + "-gone")
        try:
            r = self.ls()
            self.assertEqual((r.status_code, r.json()["code"]), (409, "no_media"))
        finally:
            os.rename(self.media + "-gone", self.media)

    def test_unreadable_folders(self):
        self.write("Movies/X/ok.mkv", b"x" * 4)
        with self.mode("Movies/X/locked", 0):
            d = self.ls("Movies").json()
            self.assertEqual([(e["name"], e["size"], e["files"], e["complete"]) for e in d["entries"]],
                             [("X", 4, 1, False)])
            self.assertFalse(d["complete"])
            r = self.ls("Movies/X/locked")
            self.assertEqual((r.status_code, r.json()["code"]), (403, "unreadable"))

    def test_sizing_budget_and_slots(self):
        self.write("TV-Shows/Show A/e.mkv", b"x" * 10)
        with mock.patch.object(paths, "SIZE_BUDGET", 0):
            d = self.ls("TV-Shows").json()
        self.assertFalse(d["complete"])
        self.assertEqual([(e["size"], e["complete"]) for e in d["entries"]], [(0, False)])
        held = [paths._sizing.acquire(blocking=False) for _ in range(2)]
        try:
            self.assertEqual(held, [True, True])
            t0 = time.monotonic()
            with mock.patch.object(paths, "SIZE_BUDGET", 0.3):
                d = self.ls("TV-Shows").json()
            self.assertLess(time.monotonic() - t0, 2)
            self.assertEqual([(e["size"], e["complete"]) for e in d["entries"]], [(0, False)])
        finally:
            for _ in held:
                paths._sizing.release()
        self.assertTrue(self.ls("TV-Shows").json()["complete"])

    def test_sizing_stops_inside_a_folder(self):
        for i in range(100):
            self.write(f"Movies/Big/f{i}.mkv")
        clock = iter([0.0] * 5 + [10.0] * 10)    # the budget runs out while the folder is read
        with mock.patch.object(paths.time, "monotonic", lambda: next(clock)):
            size, files, complete = paths.tree_size(self.m("Movies/Big"), deadline=5.0)
        self.assertFalse(complete)
        self.assertEqual(files, 4)

    def test_links_in_the_path_cannot_be_deleted_through(self):
        os.symlink(self.media, self.m("Movies/media"))           # an alias of the whole share
        r = self.rm("Movies/media/TV-Shows")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(os.path.isdir(self.m("TV-Shows")))
        self.assertTrue(all(not e["deletable"] for e in self.ls("Movies/media").json()["entries"]))
        self.assertTrue(all(not e["deletable"] for e in self.ls("Movies/media/TV-Shows").json()["entries"]))

    def test_job_writing_through_a_linked_library_is_busy(self):
        self.sign_in_and_connect()
        os.rmdir(self.m("Movies"))
        os.makedirs(self.m("Movies-real"))
        os.symlink(self.m("Movies-real"), self.m("Movies"))      # jobs still write Movies/...
        self.c.post("/api/queue", json={"keys": ["102", "103"]})
        jobs = {j["rating_key"]: j["id"] for j in self.store.jobs("all")}
        self.assertTrue(self.store.claim(jobs["102"]))
        part = self.write(LOOSE + paths.PART_SUFFIX)
        r = self.rm("Movies-real/Loose Movie.mkv" + paths.PART_SUFFIX)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "busy"))
        self.assertTrue(os.path.isfile(part))
        self.assertEqual(self.rm("Movies/Loose Movie.mkv" + paths.PART_SUFFIX).status_code, 400)
        self.write("Movies-real/Heathers (1989)/old.nfo")
        r = self.rm("Movies-real/Heathers (1989)").json()
        self.assertEqual((r["deleted"], r["cancelled"]), (1, 1))         # the queued job behind the link
        self.assertEqual(self.store.job(jobs["103"])["status"], "cancelled")

    def test_link_a_job_writes_through_is_busy(self):
        self.sign_in_and_connect()
        os.makedirs(self.m("Movies/H-real"))
        os.symlink(self.m("Movies/H-real"), self.m("Movies/Heathers (1989)"))
        self.c.post("/api/queue", json={"keys": ["103"]})
        jid = self.store.next_queued()["id"]
        self.assertTrue(self.store.claim(jid))
        for target in ("Movies/Heathers (1989)", "Movies/H-real"):      # the link, and where it leads
            r = self.rm(target)
            self.assertEqual((r.status_code, r.json()["code"]), (409, "busy"), target)
        self.assertTrue(os.path.islink(self.m("Movies/Heathers (1989)")))
        self.store.update(jid, status="queued")
        r = self.rm("Movies/Heathers (1989)").json()                     # queued: cancelled, link removed
        self.assertEqual((r["deleted"], r["cancelled"]), (1, 1))
        self.assertFalse(os.path.lexists(self.m("Movies/Heathers (1989)")))
        self.assertTrue(os.path.isdir(self.m("Movies/H-real")))

    def test_paging(self):
        for i in range(1000):
            self.write(f"Movies/Big/f{i}.mkv")
        got, token = [], None
        for offset in range(0, 1000, 200):
            d = self.ls("Movies/Big", offset=str(offset), **({"token": token} if token else {})).json()
            self.assertEqual((d["total"], d["offset"], len(d["entries"])), (1000, offset, 200))
            token = token or d["token"]
            got += [e["name"] for e in d["entries"]]
        self.assertEqual(got, [f"f{i}.mkv" for i in range(1000)])
        self.write("Movies/Big/a-new.mkv")
        r = self.ls("Movies/Big", offset="200", token=token)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "changed"))
        self.assertEqual(self.ls("Movies/Big").json()["total"], 1001)

    def test_delete_refusals(self):
        keep = self.write("Movies/keep.mkv")
        os.makedirs(os.path.join(self.outside, "dir"))
        precious = os.path.join(self.outside, "dir", "f")
        with open(precious, "w") as fh:
            fh.write("precious")
        os.symlink(os.path.join(self.outside, "dir"), self.m("Movies/out"))
        for bad in ("Movies", "Movies/.", "../x", "/etc", "Movies/\x00a", "Movies/out/f", "TV-Shows/../Movies"):
            r = self.rm("Movies/keep.mkv", bad)
            self.assertEqual(r.status_code, 400, bad)
        r = self.rm("Movies/keep.mkv", "Movies/nope")
        self.assertEqual((r.status_code, r.json()["code"]), (404, "missing"))
        for body in ({}, {"paths": "Movies/keep.mkv"}, {"paths": [1]}, {"paths": []}, {"paths": [""]},
                     {"paths": ["Movies/keep.mkv"] * 501}, ["Movies/keep.mkv"]):
            r = self.c.post("/api/files/delete", json=body)
            self.assertEqual((r.status_code, r.json()["code"]), (400, "bad_request"), str(body)[:40])
        self.assertTrue(os.path.isfile(keep))
        self.assertEqual(open(precious).read(), "precious")
        self.assertTrue(os.path.isdir(self.m("Movies")))

    def test_links_are_removed_not_followed(self):
        target = os.path.join(self.outside, "file.txt")
        with open(target, "w") as fh:
            fh.write("precious")
        os.makedirs(os.path.join(self.outside, "dir"))
        inner = os.path.join(self.outside, "dir", "inner.txt")
        with open(inner, "w") as fh:
            fh.write("precious")
        os.makedirs(self.m("Movies/A"))
        os.makedirs(self.m("Movies/B"))
        os.symlink(target, self.m("Movies/A/link-file"))
        os.symlink(os.path.join(self.outside, "dir"), self.m("Movies/A/link-dir"))
        os.symlink(os.path.join(self.outside, "dir"), self.m("Movies/B/link-dir"))
        r = self.rm("Movies/A/link-file", "Movies/A/link-dir").json()
        self.assertEqual((r["deleted"], r["errors"]), (2, []))
        self.assertFalse(os.path.lexists(self.m("Movies/A/link-file")))
        self.assertFalse(os.path.lexists(self.m("Movies/A/link-dir")))
        self.assertEqual(self.rm("Movies/B").json()["deleted"], 1)       # a folder holding a link
        self.assertFalse(os.path.lexists(self.m("Movies/B")))
        self.assertEqual(open(target).read(), "precious")
        self.assertEqual(open(inner).read(), "precious")

    def test_backslash_is_part_of_a_name(self):
        self.write("Movies/a\\b.mkv", b"one")
        self.write("Movies/a/b.mkv", b"two")
        self.assertIn("Movies/a\\b.mkv", [e["path"] for e in self.ls("Movies").json()["entries"]])
        r = self.rm("Movies/a\\b.mkv").json()
        self.assertEqual((r["deleted"], r["bytes"]), (1, 3))
        self.assertFalse(os.path.lexists(self.m("Movies/a\\b.mkv")))
        self.assertEqual(open(self.m("Movies/a/b.mkv"), "rb").read(), b"two")
        self.assertEqual(self.names("Movies/a"), ["b.mkv"])

    def test_deleting_a_copied_season(self):
        self.sign_in_and_connect()
        self.c.post("/api/queue", json={"keys": ["200"], "mode": "all"})
        self.drain()
        r = self.rm("TV-Shows/Show A/Season 01").json()
        self.assertEqual((r["deleted"], r["files"], r["removed"], r["cancelled"], r["errors"]), (1, 3, 2, 0, []))
        self.assertFalse(os.path.exists(self.m("TV-Shows/Show A/Season 01")))
        self.assertTrue(os.path.isdir(self.m("TV-Shows/Show A")))       # nothing removed above it
        jobs = {j["rating_key"]: j for j in self.store.jobs("all")}
        self.assertEqual([jobs[k]["status"] for k in ("201", "202", "203")], ["removed", "removed", "done"])
        self.assertEqual(jobs["201"]["note"], main_mod.LOCAL_NOTE)
        show = self.c.get("/api/item/200").json()
        self.assertEqual({str(e["ratingKey"]): e["copy"] for s in show["seasons"] for e in s["episodes"]},
                         {"201": None, "202": None, "203": "done"})
        r = self.c.post("/api/queue", json={"keys": ["200"], "mode": "all"}).json()
        self.assertEqual((r["added"], r["skipped"]), (2, 1))

    def test_delete_and_the_queue(self):
        self.sign_in_and_connect()
        self.write("Movies/Heat (1995)/old.nfo")
        self.c.post("/api/queue", json={"keys": ["101", "102", "103"]})
        jobs = {j["rating_key"]: j["id"] for j in self.store.jobs("all")}
        # a queued download inside the folder is cancelled
        r = self.rm("Movies/Heat (1995)").json()
        self.assertEqual((r["deleted"], r["cancelled"]), (1, 1))
        self.assertEqual(self.store.job(jobs["101"])["status"], "cancelled")
        self.assertEqual(self.store.job(jobs["101"])["note"], main_mod.LOCAL_NOTE)
        # an old subtitle of a queued download goes; the download stays queued
        self.write("Movies/Heathers (1989)/Heathers (1989).en.srt")
        r = self.rm("Movies/Heathers (1989)/Heathers (1989).en.srt").json()
        self.assertEqual((r["deleted"], r["cancelled"]), (1, 0))
        self.assertEqual(self.store.job(jobs["103"])["status"], "queued")
        # anything a downloading job writes is refused, and then nothing changes
        self.assertTrue(self.store.claim(jobs["102"]))
        part = self.write(LOOSE + paths.PART_SUFFIX)
        sub = self.write("Movies/Loose Movie.en.srt")
        for target in (LOOSE + paths.PART_SUFFIX, "Movies/Loose Movie.en.srt"):
            r = self.rm(target, "Movies/Heathers (1989)")
            self.assertEqual((r.status_code, r.json()["code"]), (409, "busy"), target)
            self.assertIn("Loose Movie", r.json()["error"])
        self.assertTrue(os.path.isfile(part) and os.path.isfile(sub))
        self.assertTrue(os.path.isdir(self.m("Movies/Heathers (1989)")))
        self.assertEqual(self.store.job(jobs["103"])["status"], "queued")
        self.assertEqual(self.store.job(jobs["102"])["status"], "downloading")

    def test_nested_selection(self):
        self.write("TV-Shows/Show B/Season 1/e.mkv", b"x" * 9)
        r = self.rm("TV-Shows/Show B", "TV-Shows/Show B/Season 1", "TV-Shows/Show B/").json()
        self.assertEqual((r["deleted"], r["bytes"], r["files"], r["errors"]), (1, 9, 1, []))
        self.assertFalse(os.path.exists(self.m("TV-Shows/Show B")))

    def test_totals_count_only_what_was_deleted(self):
        self.write("Movies/A/a.mkv", b"x" * 100)
        self.write("Movies/B/b.mkv", b"x" * 50)
        with self.mode("Movies/B", 0o555):
            r = self.rm("Movies/A", "Movies/B/b.mkv").json()
        self.assertEqual((r["deleted"], r["bytes"], r["files"]), (1, 100, 1))
        self.assertEqual([e["path"] for e in r["errors"]], ["Movies/B/b.mkv"])
        self.assertTrue(os.path.isfile(self.m("Movies/B/b.mkv")))


class DeviceNameTest(unittest.TestCase):
    def test_sources(self):
        for env, host, want in [({"TRAVELARR_NAME": " Road NAS ", "HOST_HOSTNAME": "N2"}, "x", "Road NAS"),
                                ({"HOST_HOSTNAME": " N2 "}, "3f2a1b9c4d5e", "N2"),   # Unraid's server name
                                ({"TRAVELARR_NAME": " ", "HOST_HOSTNAME": ""}, "n2.lan", "n2"),
                                ({}, "N2", "N2"),
                                ({}, "3f2a1b9c4d5e", "NAS"),           # Docker's default: the container id
                                ({}, "", "NAS")]:
            with mock.patch.dict(os.environ, env), mock.patch("socket.gethostname", return_value=host):
                for var in ("TRAVELARR_NAME", "HOST_HOSTNAME"):
                    if var not in env:
                        os.environ.pop(var, None)
                self.assertEqual(main_mod.device_name(), want, (env, host))

    def test_state_and_worker_use_it(self):
        tmp = tempfile.mkdtemp()
        try:
            app = create_app(data_dir=tmp, media_root=tmp, env={}, start_worker=False, device="Road NAS",
                             gateway_factory=lambda s: PlexGateway(s, "t"))
            with TestClient(app) as c:
                self.assertEqual(c.get("/api/state").json()["device"], "Road NAS")
            self.assertEqual(app.state.dl.device, "Road NAS")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ResyncTest(PlexFixture):
    """Re-sync: compare the copies in a Local Files folder with Plex and bring them up to date."""
    HEAT_DIR = "Movies/Heat (1995)"
    HEATHERS_DIR = "Movies/Heathers (1989)"
    SHOW = "TV-Shows/Show A"
    S01, S02 = "TV-Shows/Show A/Season 01", "TV-Shows/Show A/Season 02"
    HEAT_ES = "Movies/Heat (1995)/Heat (1995).es.srt"
    HEAT_EN = "Movies/Heat (1995)/Heat (1995).en.forced.srt"
    EMPTY = {"serverId": "srv1", "checked": 0, "unchanged": 0, "unverified": 0, "items": []}

    def setUp(self):
        super().setUp()
        self.sign_in_and_connect()
        heat = self.movie(101)
        heat["subs"] = [x for x in heat["subs"] if x["id"] != 9005]   # the always-invalid one

    # helpers ---------------------------------------------------------------
    def read(self, rel):
        with open(self.m(rel), "rb") as fh:
            return fh.read()

    def copy(self, *keys, mode="all"):
        self.c.post("/api/queue", json={"keys": list(keys), "mode": mode})
        self.drain()

    def check(self, path):
        r = self.c.post("/api/resync/check", json={"path": path})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def kinds(self, path):
        return {i["ratingKey"] or i["path"]: i["kind"] for i in self.check(path)["items"]}

    def apply(self, path, *rks, server=None, subtitles=False):
        sid = server or self.check(path)["serverId"]
        return self.c.post("/api/resync/apply", json={"path": path, "serverId": sid, "ratingKeys": list(rks),
                                                       "subtitles": subtitles})

    def row(self, rk, status="done"):
        rows = [j for j in self.store.jobs_for(rating_key=rk) if j["status"] == status]
        return rows[-1] if rows else None

    def new_part(self, m, pid, file=None, size=None, content=None):
        """Plex now has another version of a movie/episode dict m."""
        m["part"] = pid
        m["file"] = file or m["file"]
        m["size"] = size or m["size"]
        self.fake.parts[pid] = content or bytes((i * 11 + pid) % 251 for i in range(m["size"]))
        self.fake.versions[pid] = 1

    def movie(self, rk):
        return next(x for x in self.fake.movies if x["rk"] == rk)

    def part_requests(self, pid, since=0):
        return [r for r in self.fake.requests[since:] if f"/library/parts/{pid}/" in r[1]]

    # check -----------------------------------------------------------------
    def test_unchanged_copy_reports_nothing(self):
        self.copy("101")
        d = self.check(self.HEAT_DIR)
        self.assertEqual((d["checked"], d["unchanged"], d["items"]), (1, 1, []))
        self.assertEqual(d["serverId"], "srv1")
        self.assertEqual(self.check(LOOSE), self.EMPTY)
        self.assertEqual(self.check("Movies/Heat"), self.EMPTY)

    def test_whole_media_folder_and_library_folders(self):
        self.copy("101", "102")
        self.copy("200")
        for path in ("", "Movies", "TV-Shows", self.SHOW, self.S01):
            self.assertEqual(self.check(path)["items"], [], path)
        self.assertEqual([self.check(p)["checked"] for p in ("", "Movies", self.SHOW, self.S01, self.S02)],
                         [5, 2, 3, 2, 1])
        self.new_part(self.movie(102), 1102, size=125_000)
        os.remove(self.m("TV-Shows/Show A/Season 02/Show A - S02E01 - Third.mkv"))
        self.assertEqual(self.kinds(""), {"102": "changed", "203": "absent"})
        self.assertEqual(self.kinds("TV-Shows"), {"203": "absent"})
        self.assertEqual(self.kinds(self.S01), {})
        r = self.apply("", "203", subtitles=True).json()                    # 102 left out
        self.assertEqual((r["upgrades"], r["redownloads"], r["refreshes"]), (0, 1, 3))
        self.drain()
        self.assertEqual(self.kinds(""), {"102": "changed"})
        self.assertEqual(self.row("201")["note"], "re-synced: subtitles")

    def test_what_plex_has_and_the_nas_does_not(self):
        self.copy("202")
        self.assertEqual(self.kinds(self.S01), {"201": "absent"})        # same season folder
        for path in ("", "TV-Shows", self.SHOW):
            self.assertEqual(self.kinds(path), {"201": "absent"}, path)  # no Season 02 folder on the NAS
        self.assertEqual(self.check(self.S02), self.EMPTY)
        item = self.check(self.S01)["items"][0]
        self.assertEqual((item["label"], item["reason"], item["videoBytes"], item["watched"]),
                         ("Show A S01E01 Pilot", "not on the NAS", 80_000, True))
        # deleted in Local Files: the episode is offered again, a movie whose folder is gone is not
        self.copy("201", "103")
        self.c.post("/api/files/delete", json={"paths": ["TV-Shows/Show A/Season 01/Show A - S01E02 - Second.mkv",
                                                         self.HEATHERS_DIR]})
        self.assertEqual(self.row("202", "removed")["note"], main_mod.LOCAL_NOTE)
        self.assertEqual(self.kinds(""), {"202": "absent"})
        self.assertEqual(self.check(self.S01)["items"][0]["watched"], False)
        r = self.apply("", "202").json()
        self.assertEqual((r["copies"], r["videoBytes"], r["refreshes"], r["skipped"]), (1, 90_000, 0, 0))
        self.assertIsNone(self.row("201", "queued"))                      # subtitles left alone unless asked
        self.drain()
        self.assertEqual(self.read("TV-Shows/Show A/Season 01/Show A - S01E02 - Second.mkv"), self.fake.parts[2002])
        self.assertEqual(self.kinds(""), {})

    def test_files_travelarr_never_copied(self):
        """A season copied some other way: compared all the same."""
        os.makedirs(self.m(self.S01))
        with open(self.m("TV-Shows/Show A/Season 01/Show A - S01E01 - Pilot.mkv"), "wb") as fh:
            fh.write(self.fake.parts[2001])
        self.assertEqual(self.store.jobs("all"), [])
        d = self.check(self.S01)
        self.assertEqual((d["checked"], d["unchanged"], [(i["ratingKey"], i["kind"]) for i in d["items"]]),
                         (1, 1, [("202", "absent")]))
        r = self.apply(self.S01, "202", subtitles=True).json()
        self.assertEqual((r["copies"], r["refreshes"]), (1, 1))
        self.drain()
        adopted = self.row("201")
        self.assertEqual((adopted["note"], adopted["part_id"]), ("re-synced: subtitles", 2001))  # recorded, kept
        self.assertTrue(self.part_requests(2002))
        self.assertEqual([r for r in self.part_requests(2001) if "Range" not in r[2]], [])   # only compared
        self.assertEqual(self.read(self.row("202")["dest_rel"]), self.fake.parts[2002])
        # a different size with the same name: the new version replaces it, and its old subtitles go
        pilot = "TV-Shows/Show A/Season 01/Show A - S01E01 - Pilot.mkv"
        with open(self.m(pilot), "r+b") as fh:
            fh.truncate(10)
        with open(self.m(pilot[:-4] + ".de.srt"), "w") as fh:
            fh.write("alt")
        self.store.update(adopted["id"], status="removed")
        self.assertEqual(self.kinds(self.S01), {"201": "changed"})
        self.assertEqual(self.apply(self.S01, "201").json()["upgrades"], 1)
        self.drain()
        self.assertEqual(self.read(pilot), self.fake.parts[2001])
        self.assertFalse(os.path.lexists(self.m(pilot[:-4] + ".de.srt")))

    def test_absent_reuses_the_row_of_a_renamed_file(self):
        self.copy("201")
        row = self.row("201")
        os.remove(self.m(row["dest_rel"]))                          # deleted outside travelarr
        ep = next(e for e in self.fake.episodes if e["rk"] == 201)
        ep["file"] = "/data/Media/TV-Shows/Show A/Season 01/Show A - S01E01 - The Pilot.mkv"  # renamed, same part
        self.assertEqual(self.kinds(self.S01), {"201": "absent", "202": "absent"})
        self.assertEqual(self.apply(self.S01, "201").json()["redownloads"], 1)
        self.drain()
        job = self.store.job(row["id"])
        self.assertEqual((job["status"], job["dest_rel"]),
                         ("done", "TV-Shows/Show A/Season 01/Show A - S01E01 - The Pilot.mkv"))
        self.assertEqual(self.read(job["dest_rel"]), self.fake.parts[2001])

    def test_new_name_replaces_a_file_travelarr_never_copied(self):
        old = "TV-Shows/Show A/Season 01/Show A - S01E01 - Pilot WEBDL-720p.mkv"
        os.makedirs(self.m(self.S01))
        for rel, data in ((old, b"o" * 70_000), (old[:-4] + ".en.srt", b"old"), (old[:-4] + ".nfo", b"keep")):
            with open(self.m(rel), "wb") as fh:
                fh.write(data)
        item = next(i for i in self.check(self.S01)["items"] if i["ratingKey"] == "201")
        self.assertEqual((item["kind"], item["video"]), ("upgraded", {
            "from": {"file": "Show A - S01E01 - Pilot WEBDL-720p.mkv", "size": 70_000},
            "to": {"file": "Show A - S01E01 - Pilot.mkv", "size": 80_000}}))
        r = self.apply(self.S01, "201").json()
        self.assertEqual((r["upgrades"], r["videoBytes"]), (1, 80_000))
        self.drain()
        self.assertEqual(self.read("TV-Shows/Show A/Season 01/Show A - S01E01 - Pilot.mkv"), self.fake.parts[2001])
        self.assertFalse(os.path.lexists(self.m(old)))
        self.assertFalse(os.path.lexists(self.m(old[:-4] + ".en.srt")))
        self.assertTrue(os.path.isfile(self.m(old[:-4] + ".nfo")))              # not a subtitle
        found = self.row("201", "removed")
        self.assertEqual((found["dest_rel"], found["note"]), (old, "replaced by a re-sync"))

    def test_title_pages_name_their_folder_on_the_nas(self):
        item = lambda rk: self.c.get(f"/api/item/{rk}").json()  # noqa: E731
        self.assertEqual([item(rk)["nasFolder"] for rk in ("101", "200")], [None, None])   # not on the NAS
        self.copy("101", "102", "201")
        self.assertEqual(item("101")["nasFolder"], self.HEAT_DIR)
        self.assertIsNone(item("102")["nasFolder"])                  # a loose movie: that is the library folder
        self.assertEqual(item("200")["nasFolder"], self.SHOW)
        self.assertEqual(self.kinds(item("200")["nasFolder"]), {"202": "absent"})

    def test_failed_copies_and_rows_of_another_server(self):
        self.copy("201")
        row = self.row("201")
        self.store.update(row["id"], server_id="srv2", guid="plex://episode/other")
        self.assertEqual(self.check(self.S01)["unchanged"], 1)  # the file on the NAS decides, not the row
        self.c.post("/api/queue", json={"keys": ["202"], "mode": "all"})
        failed = self.row("202", "queued")
        self.store.update(failed["id"], status="failed", error="boom")
        item = self.check(self.S01)["items"][0]
        self.assertEqual((item["ratingKey"], item["kind"], item["reason"]), ("202", "busy", resync_mod.FAILED))
        self.assertEqual(self.apply(self.S01, subtitles=True).json()["refreshes"], 1)
        self.assertEqual((self.store.job(row["id"])["server_id"], self.store.job(row["id"])["guid"]),
                         ("srv1", "plex://episode/201"))
        self.drain()
        self.store.update(row["id"], status="failed", error="boom")   # a failed refresh: left to the Queue
        self.assertEqual(self.kinds(self.S01), {"201": "busy", "202": "busy"})

    def test_upgrade_with_a_new_file_name(self):
        self.copy("101")
        old = self.row("101")
        new = "/data/Media/Movies/Heat (1995)/Heat (1995) Bluray-2160p.mkv"
        self.new_part(self.movie(101), 1101, file=new, size=310_000)
        item = self.check(self.HEAT_DIR)["items"][0]
        self.assertEqual(item["kind"], "upgraded")
        self.assertEqual(item["video"], {"from": {"file": "Heat (1995).mkv", "size": 300_000},
                                         "to": {"file": "Heat (1995) Bluray-2160p.mkv", "size": 310_000}})
        r = self.apply(self.HEAT_DIR, "101").json()
        self.assertEqual((r["upgrades"], r["videoBytes"]), (1, 310_000))
        self.drain()
        self.assertEqual(self.store.job(old["id"])["status"], "removed")
        self.assertEqual(self.store.job(old["id"])["note"], "replaced by a re-sync")
        done = self.row("101")
        self.assertEqual((done["mode"], done["note"]), ("upgrade", "re-synced: new version"))
        self.assertEqual(self.read("Movies/Heat (1995)/Heat (1995) Bluray-2160p.mkv"), self.fake.parts[1101])
        self.assertFalse(os.path.lexists(self.m(HEAT)))
        self.assertFalse(os.path.lexists(self.m(self.HEAT_ES)))      # old name retired
        self.assertTrue(os.path.isfile(self.m("Movies/Heat (1995)/Heat (1995) Bluray-2160p.es.srt")))
        self.assertEqual(json.loads(done["cleanup"]), [])
        self.assertEqual(json.loads(self.store.job(old["id"])["cleanup"]), [])
        leftovers = [n for n in os.listdir(self.m("Movies/Heat (1995)")) if "travelarr" in n]
        self.assertEqual(leftovers, [])
        self.assertEqual(self.check(self.HEAT_DIR)["items"], [])

    def test_upgrade_in_place_same_name_new_size(self):
        self.copy("101")
        old = self.row("101")
        self.new_part(self.movie(101), 1101, size=320_000)
        self.assertEqual(self.kinds(self.HEAT_DIR), {"101": "changed"})
        self.apply(self.HEAT_DIR, "101")
        self.drain()
        self.assertEqual(self.read(HEAT), self.fake.parts[1101])
        self.assertTrue(os.path.isfile(self.m(self.HEAT_ES)))       # same name: re-fetched, kept
        self.assertEqual(self.store.job(old["id"])["status"], "removed")
        self.assertIn(self.HEAT_ES, json.loads(self.row("101")["sidecars"]))

    def test_same_name_and_size_new_content_is_found(self):
        self.copy("101")
        before, size, n = len(self.fake.requests), 300_000, resync_mod.SAMPLE
        self.assertEqual(self.kinds(self.HEAT_DIR), {})
        self.assertEqual([r[2]["Range"] for r in self.part_requests(1001, before)],
                         [f"bytes=0-{n - 1}", f"bytes={size - n}-{size - 1}"])     # the start, then the end
        self.new_part(self.movie(101), 1101)  # same name, same size, other bytes
        item = self.check(self.HEAT_DIR)["items"][0]
        self.assertEqual((item["kind"], item["reason"]), ("changed", resync_mod.NEW_BYTES))
        self.assertEqual(self.apply(self.HEAT_DIR, "101").json()["upgrades"], 1)
        self.drain()
        self.assertEqual(self.read(HEAT), self.fake.parts[1101])
        self.assertEqual(self.kinds(self.HEAT_DIR), {})
        # Plex's file replaced under the same part: the same row copies it again
        m = self.movie(101)
        self.fake.parts[1101] = bytes(reversed(self.fake.parts[1101]))
        self.assertEqual(self.kinds(self.HEAT_DIR), {"101": "changed"})
        self.assertEqual(self.apply(self.HEAT_DIR, "101").json()["redownloads"], 1)
        self.drain()
        self.assertEqual(self.read(HEAT), self.fake.parts[m["part"]])

    def test_bytes_that_cannot_be_read_fall_back_to_name_and_size(self):
        self.copy("101")
        self.fake.ignore_range.add(1001)
        d = self.check(self.HEAT_DIR)
        self.assertEqual((d["unchanged"], d["unverified"], d["items"]), (1, 1, []))

    def test_apply_compares_bytes_only_for_the_ticked(self):
        self.copy("101", "103")
        self.new_part(self.movie(103), 1203)                     # same name and size, other bytes
        self.assertEqual(self.kinds("Movies"), {"103": "changed"})
        before = len(self.fake.requests)
        r = self.apply("Movies", server="srv1", subtitles=True).json()  # nothing ticked: subtitles only
        self.assertEqual((r["refreshes"], r["upgrades"]), (2, 0))
        self.assertEqual(self.part_requests(1001, before) + self.part_requests(1203, before), [])

    def test_upgrade_hash_survives_a_dropped_connection(self):
        self.copy("101")
        self.new_part(self.movie(101), 1101, size=320_000)
        self.fake.drop_after[1101] = 100_000
        self.dl.backoff = 0.01
        self.apply(self.HEAT_DIR, "101")
        self.drain()
        self.assertEqual(self.row("101")["staging_mark"], hashlib.sha256(self.fake.parts[1101]).hexdigest())
        self.assertEqual(self.read(HEAT), self.fake.parts[1101])

    def test_missing_copy_is_copied_again(self):
        self.copy("103")
        os.remove(self.m(HEATHERS))
        self.assertEqual(self.check(self.HEATHERS_DIR)["items"][0]["reason"], "not on the NAS")
        r = self.apply(self.HEATHERS_DIR, "103").json()
        self.assertEqual((r["redownloads"], r["videoBytes"]), (1, 50_000))
        rid = self.row("103", "queued")["id"]
        self.drain()
        done = self.store.job(rid)
        self.assertEqual((done["status"], done["note"], done["mode"]), ("done", "re-synced: was missing", None))
        self.assertEqual(self.read(HEATHERS), self.fake.parts[1003])
        self.assertEqual(self.check("Movies")["items"], [])   # in the library folder only what is on the NAS

    def test_wrong_size_on_the_nas(self):
        self.copy("102")
        with open(self.m(LOOSE), "r+b") as fh:
            fh.truncate(10)
        item = self.check("Movies")["items"][0]
        self.assertEqual((item["kind"], item["video"]["from"]["size"]), ("changed", 10))
        self.assertEqual(self.apply("Movies", "102").json()["redownloads"], 1)
        self.drain()
        self.assertEqual(self.read(LOOSE), self.fake.parts[1002])

    def test_subtitles_are_fetched_again(self):
        self.copy("101")
        m = self.movie(101)
        es = next(x for x in m["subs"] if x["id"] == 9001)
        es["body"] = SRT_BODY("Hole")                                        # same size, new text
        m["subs"] = [x for x in m["subs"] if x["id"] != 9002]               # en forced removed
        m["subs"].append(dict(id=9006, codec="srt", lang="fr", ext=True, body=SRT_BODY("Salut")))
        self.assertEqual(self.check(self.HEAT_DIR)["items"], [])            # subtitles are not compared
        rid = self.row("101")["id"]
        self.assertEqual(self.apply(self.HEAT_DIR).json()["refreshes"], 0)
        r = self.apply(self.HEAT_DIR, subtitles=True).json()
        self.assertEqual((r["refreshes"], r["videoBytes"]), (1, 0))
        self.assertEqual(self.store.pending_bytes(), 0)
        before = len(self.fake.requests)
        self.drain()
        self.assertEqual(self.part_requests(1001, before), [])               # the video is not touched
        row = self.store.job(rid)
        self.assertEqual((row["status"], row["note"], row["mode"]), ("done", "re-synced: subtitles", None))
        self.assertEqual(self.read(self.HEAT_ES), es["body"].encode())
        self.assertTrue(os.path.isfile(self.m("Movies/Heat (1995)/Heat (1995).fr.srt")))
        self.assertFalse(os.path.lexists(self.m(self.HEAT_EN)))
        self.assertNotIn(self.HEAT_EN, json.loads(row["sidecars"]))
        self.assertEqual(self.check(self.HEAT_DIR)["items"], [])

    def test_failed_subtitle_fetch_keeps_old_ones(self):
        self.copy("101")
        m = self.movie(101)
        next(x for x in m["subs"] if x["id"] == 9001)["body"] = "<html>oops</html>"   # not a subtitle
        m["subs"] = [x for x in m["subs"] if x["id"] != 9002]
        self.apply(self.HEAT_DIR, subtitles=True)
        self.drain()
        row = self.row("101")
        self.assertTrue(os.path.isfile(self.m(self.HEAT_EN)))               # not retired
        self.assertIn(self.HEAT_EN, json.loads(row["sidecars"]))
        self.assertEqual(row["note"], "old subtitles kept: some subtitles could not be fetched")
        self.assertEqual(self.read(self.HEAT_ES), SRT_BODY("Hola").encode())  # old file of that name kept

    def test_folder_scope_reports_an_episode_gone_from_plex(self):
        self.copy("200")
        second = "TV-Shows/Show A/Season 01/Show A - S01E02 - Second.mkv"
        self.fake.episodes = [e for e in self.fake.episodes if e["rk"] != 202]
        self.assertEqual(self.kinds(self.S01), {second: "notonplex"})
        item = self.check(self.S01)["items"][0]
        self.assertEqual((item["label"], item["ratingKey"]), ("Show A - S01E02 - Second.mkv", None))
        self.assertEqual(self.check(self.S02)["checked"], 1)
        self.assertEqual(self.kinds(self.SHOW), {second: "notonplex"})
        self.fake.episodes = []
        self.assertEqual(set(self.kinds("").values()), {"notonplex"})
        self.assertEqual(len(self.kinds("")), 3)
        r = self.apply("").json()                                           # nothing to do, the files stay
        self.assertEqual((r["upgrades"], r["redownloads"], r["refreshes"], r["skipped"]), (0, 0, 0, 0))
        self.assertTrue(os.path.isfile(self.m(second)))

    def test_busy_and_double_apply(self):
        self.copy("103")
        os.remove(self.m(HEATHERS))
        self.apply(self.HEATHERS_DIR, "103")
        self.assertEqual(self.kinds(self.HEATHERS_DIR), {"103": "busy"})
        r = self.apply(self.HEATHERS_DIR, "103").json()
        self.assertEqual((r["redownloads"], r["skipped"]), (0, 1))

    def test_multipart_and_unavailable(self):
        self.copy("103")
        h = self.movie(103)
        h["extra_parts"] = [dict(part=1203, file=f"{h['file']}.cd2", size=10)]
        self.assertEqual(self.kinds(self.HEATHERS_DIR), {"Movies/Heathers (1989)/Heathers (1989).mkv": "multipart"})
        h["extra_parts"], h["no_parts"] = [], True
        self.assertEqual(self.kinds(self.HEATHERS_DIR), {"Movies/Heathers (1989)/Heathers (1989).mkv": "notonplex"})

    def test_requests_are_validated(self):
        self.copy("101")
        for bad in ("../x", "/etc", "Movies/../..", 5, None):
            self.assertEqual(self.c.post("/api/resync/check", json={"path": bad}).status_code, 400, bad)
        self.assertEqual(self.apply(self.HEAT_DIR, "101", server="other").status_code, 409)
        for bad in (None, "101", ["101", "101"], [101], ["102"]):
            r = self.c.post("/api/resync/apply", json={"path": self.HEAT_DIR, "serverId": "srv1", "ratingKeys": bad})
            self.assertEqual(r.status_code, 400, bad)
        from app import resync
        with resync.LOCK:
            r = self.c.post("/api/resync/check", json={"path": self.HEAT_DIR})
        self.assertEqual((r.status_code, r.json()["error"]), (409, "A re-sync check is already running"))

    # upgrade lifecycle -------------------------------------------------------
    def upgrade_queued(self):
        self.copy("101")
        old = self.row("101")
        self.new_part(self.movie(101), 1101, file="/data/Media/Movies/Heat (1995)/Heat (1995) v2.mkv",
                      size=310_000)
        self.apply(self.HEAT_DIR, "101")
        return old, self.row("101", "queued")

    def test_cancel_before_placement_keeps_the_old_copy(self):
        old, up = self.upgrade_queued()
        self.c.post(f"/api/jobs/{up['id']}/cancel")
        self.assertEqual(self.store.job(up["id"])["status"], "cancelled")
        self.drain()
        self.assertEqual(self.store.job(old["id"])["status"], "done")
        self.assertEqual(self.read(HEAT), self.fake.parts[1001])

    def test_removing_the_old_copy_wins_over_a_queued_upgrade(self):
        old, up = self.upgrade_queued()
        self.assertEqual(self.c.post(f"/api/jobs/{old['id']}/remove").json(), {"result": "removed"})
        self.drain()
        self.assertEqual(self.store.job(up["id"])["status"], "cancelled")
        self.assertFalse(os.path.lexists(self.m("Movies/Heat (1995)/Heat (1995) v2.mkv")))
        self.assertFalse(os.path.lexists(self.m(HEAT)))

    def test_old_copy_removed_while_the_upgrade_downloads(self):
        old, up = self.upgrade_queued()
        real = self.store.begin_upgrade

        def begin(*a):
            self.store.remove(old["id"])          # removed between the download and placement
            return real(*a)
        with mock.patch.object(self.store, "begin_upgrade", begin):
            self.drain()
        job = self.store.job(up["id"])
        self.assertEqual((job["status"], job["note"]), ("cancelled", "the copy it would replace was removed"))
        self.assertFalse(os.path.lexists(self.m("Movies/Heat (1995)/Heat (1995) v2.mkv")))
        self.assertEqual([n for n in os.listdir(self.m("Movies/Heat (1995)")) if "resync" in n], [])

    def test_removal_while_placing_is_busy(self):
        old, up = self.upgrade_queued()
        self.store.update(up["id"], phase="placing")
        self.assertEqual(self.c.post(f"/api/jobs/{old['id']}/remove").json(), {"result": "busy"})
        self.assertEqual(self.c.post(f"/api/jobs/{up['id']}/remove").json(), {"result": "busy"})
        self.assertFalse(self.store.cancel_queued(up["id"]))
        self.assertEqual(self.kinds(self.HEAT_DIR), {"101": "busy"})

    def test_crash_after_the_rename_is_recovered_by_hash(self):
        old, up = self.upgrade_queued()
        real = self.store.update
        state = {"n": 0}

        def update(job_id, **f):
            if f == {"phase": "placed"} and not state["n"]:
                state["n"] += 1
                raise sqlite3.OperationalError("disk I/O error")   # after the rename, before `placed`
            return real(job_id, **f)
        with mock.patch.object(self.store, "update", update):
            self.dl._process(self.store.next_queued())
        job = self.store.job(up["id"])
        self.assertEqual((job["status"], job["phase"]), ("queued", "placing"))   # never failed
        self.assertIn(up["id"], self.dl._later)                                   # backs off
        self.dl._later.clear()
        before = len(self.fake.requests)
        self.drain()
        self.assertEqual(self.part_requests(1101, before), [])               # no second download
        self.assertEqual(self.store.job(up["id"])["status"], "done")
        self.assertEqual(self.store.job(old["id"])["status"], "removed")
        self.assertFalse(os.path.lexists(self.m(HEAT)))

    def test_staging_lost_before_the_rename_is_not_certified(self):
        old, up = self.upgrade_queued()
        self.store.update(up["id"], phase="placing", staging_mark="0" * 64)   # crashed after T1, staging gone
        self.drain()
        job = self.store.job(up["id"])
        self.assertEqual((job["status"], job["phase"], job["error"]), ("failed", None, worker.UNCONFIRMED))
        o = self.store.job(old["id"])
        self.assertEqual((o["status"], o["note"]), ("done", "unverified after an interrupted re-sync"))
        self.assertEqual(self.read(HEAT), self.fake.parts[1001])
        self.assertEqual(self.kinds(self.HEAT_DIR), {"101": "busy"})                  # the failed upgrade comes first
        self.c.post(f"/api/jobs/{up['id']}/remove")
        self.assertEqual(self.kinds(self.HEAT_DIR), {"101": "upgraded"})              # unlocked, can be re-synced

    def test_rename_error_fails_and_retry_does_not_download(self):
        old, up = self.upgrade_queued()
        real = worker.os.replace

        def replace(src, dst):
            if ".travelarr-resync-" in src:
                raise OSError(13, "Permission denied")
            return real(src, dst)
        with mock.patch.object(worker.os, "replace", replace):
            self.drain()
        job = self.store.job(up["id"])
        self.assertEqual((job["status"], job["phase"]), ("failed", None))
        self.assertEqual(self.store.job(old["id"])["status"], "done")
        before = len(self.fake.requests)
        self.c.post(f"/api/jobs/{up['id']}/retry")
        self.drain()
        # the complete staging file is only re-checked against Plex (its last 64 KB), not downloaded again
        tail = f"bytes={310_000 - worker.OVERLAP}-"
        self.assertEqual([r[2].get("Range") for r in self.part_requests(1101, before)], [tail])
        self.assertEqual(self.store.job(up["id"])["status"], "done")
        self.assertEqual(self.read("Movies/Heat (1995)/Heat (1995) v2.mkv"), self.fake.parts[1101])

    # ownership and cleanup ---------------------------------------------------
    def test_cleanup_never_deletes_a_file_a_copy_owns(self):
        self.copy("101", "102")
        heat = self.row("101")
        loose = self.row("102")
        self.store.update(loose["id"], status="removed", cleanup=json.dumps([HEAT, LOOSE]))
        self.assertEqual(self.dl.cleanup(loose["id"]), [])
        self.assertTrue(os.path.isfile(self.m(HEAT)))                         # owned by a done copy
        self.assertFalse(os.path.lexists(self.m(LOOSE)))
        self.store.update(heat["id"], status="queued", mode="redownload")
        self.store.update(loose["id"], cleanup=json.dumps([HEAT]))
        self.assertEqual(self.dl.cleanup(loose["id"]), [HEAT])                # deferred for an unfinished job

    def test_cancel_hands_off_the_partial_only(self):
        self.copy("102")
        rid = self.row("102")["id"]
        self.store.transition(rid, lambda r: True, status="queued", mode="redownload")
        with open(self.m(LOOSE) + paths.PART_SUFFIX, "wb") as fh:
            fh.write(b"x" * 10)
        self.c.post(f"/api/jobs/{rid}/cancel")
        self.assertEqual(self.store.job(rid)["status"], "cancelled")
        self.assertFalse(os.path.lexists(self.m(LOOSE) + paths.PART_SUFFIX))
        self.assertTrue(os.path.isfile(self.m(LOOSE)))                        # still the cancelled copy's

    def test_placed_job_waiting_after_a_crash_can_be_removed(self):
        self.copy("102")
        rid = self.row("102")["id"]
        self.store.update(rid, status="queued", phase="placed")
        self.assertFalse(self.store.cancel_queued(rid))
        self.assertEqual(self.c.post(f"/api/jobs/{rid}/remove").json(), {"result": "removed"})
        self.assertFalse(os.path.lexists(self.m(LOOSE)))
        self.drain()
        self.assertEqual(self.store.job(rid)["status"], "removed")

    # review findings ---------------------------------------------------------
    def test_bulk_removal_only_touches_the_selected_server(self):
        self.copy("102")
        rid = self.row("102")["id"]
        self.store.update(rid, server_id="other")
        self.c.post("/api/remove", json={"ratingKeys": ["102"], "serverId": "srv1"})
        self.assertEqual(self.store.job(rid)["status"], "done")
        self.assertTrue(os.path.isfile(self.m(LOOSE)))

    def test_removal_is_ordered_with_the_upgrade_lock(self):
        old, up = self.upgrade_queued()
        self.store.update(up["id"], phase="placing")
        self.assertIsNone(self.store.remove(old["id"]))           # refused inside the same transaction
        self.assertFalse(self.store.mark_removed(old["id"], "x"))
        self.assertEqual(self.store.job(old["id"])["status"], "done")

    def test_local_files_refuses_the_copy_an_upgrade_is_replacing(self):
        old, up = self.upgrade_queued()
        self.store.update(up["id"], phase="placed")
        r = self.c.post("/api/files/delete", json={"paths": [HEAT]})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(os.path.isfile(self.m(HEAT)))

    def test_lost_rename_recovery_keeps_ownership(self):
        self.copy("102")
        rid = self.row("102")["id"]
        with open(self.m(LOOSE) + paths.PART_SUFFIX, "wb") as fh:
            fh.write(b"y" * 10)
        self.dl._reconcile_done()
        job = self.store.job(rid)
        self.assertEqual((job["status"], job["mode"]), ("queued", "recover"))
        self.assertIn(LOOSE, store_mod.owned(job))
        self.c.post(f"/api/jobs/{rid}/cancel")
        self.assertIn(LOOSE, store_mod.owned(self.store.job(rid)))   # still owned after cancel
        self.assertTrue(os.path.isfile(self.m(LOOSE)))

    def test_cancel_during_a_subtitle_refresh_returns_to_done(self):
        self.copy("101")
        rid = self.row("101")["id"]
        es = next(x for x in self.movie(101)["subs"] if x["id"] == 9001)
        es["body"] = SRT_BODY("Otra vez")
        self.apply(self.HEAT_DIR, "101")
        real = self.gw.fetch_bytes

        def fetch(*a, **kw):
            self.dl.cancel(rid)          # cancelled while the first subtitle downloads
            return real(*a, **kw)
        with mock.patch.object(self.gw, "fetch_bytes", fetch):
            self.drain()
        job = self.store.job(rid)
        self.assertEqual((job["status"], job["mode"], job["note"]), ("done", None, "re-sync cancelled"))
        self.assertEqual(len(json.loads(job["sidecars"])), 2)                   # nothing lost or retired
        self.assertTrue(os.path.isfile(self.m(self.HEAT_EN)))

    def test_resync_ignores_rows_of_another_server(self):
        self.copy("102")
        other = dict(self.row("102"))
        self.store.add_jobs([{**{k: other[k] for k in store_mod.JOB_FIELDS if k in other},
                              "server_id": "other", "dest_rel": "Movies/elsewhere.mkv", "part_id": 1}])
        self.assertEqual(self.check("Movies"), {**self.EMPTY, "checked": 1, "unchanged": 1})

    # review round 2 ----------------------------------------------------------
    def test_bulk_removal_names_its_server(self):
        self.copy("102")
        for body in ({"ratingKeys": ["102"]}, {"ratingKeys": ["102"], "serverId": "other"}):
            self.assertEqual(self.c.post("/api/remove", json=body).status_code, 409)
        self.assertTrue(os.path.isfile(self.m(LOOSE)))

    def placed_upgrade(self):
        """An upgrade whose new file is in place, interrupted before it finished."""
        old, up = self.upgrade_queued()
        real = self.dl._subtitles
        calls = {"n": 0}

        def boom(job):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("power cut")
            return real(job)
        self._sub_patch = mock.patch.object(self.dl, "_subtitles", boom)
        self._sub_patch.start()
        self.addCleanup(self._sub_patch.stop)
        self.dl._process(self.store.next_queued())
        job = self.store.job(up["id"])
        self.assertEqual((job["status"], job["phase"]), ("queued", "placed"))
        self.dl._later.clear()
        return old, up

    def test_placed_upgrade_finishes_with_another_server_selected(self):
        old, up = self.placed_upgrade()
        self.store.set("server_id", "other")
        job = self.store.next_queued("other")
        self.assertEqual(job["id"], up["id"])                       # picked whatever the server
        self.dl._process(job)
        done = self.store.job(up["id"])
        self.assertEqual((done["status"], done["note"]), ("done", "subtitles not refreshed: the Plex server changed"))
        self.assertEqual(self.store.job(old["id"])["status"], "removed")

    def test_placed_upgrade_finishes_while_plex_is_unreachable(self):
        old, up = self.placed_upgrade()
        with mock.patch.object(self.gw, "server", side_effect=plexgw.NotReady("down")):
            self.dl._process(self.store.next_queued("srv1"))
        done = self.store.job(up["id"])
        self.assertEqual(done["status"], "done")
        self.assertIn("subtitles not checked", done["error"])
        self.assertEqual(self.store.job(old["id"])["status"], "removed")

    def test_placed_file_gone_fails_once_and_unlocks(self):
        old, up = self.placed_upgrade()
        os.remove(self.m("Movies/Heat (1995)/Heat (1995) v2.mkv"))
        self.dl._process(self.store.next_queued("srv1"))
        job = self.store.job(up["id"])
        self.assertEqual((job["status"], job["phase"]), ("failed", None))
        self.assertIsNone(self.store.next_queued("srv1"))          # no loop
        o = self.store.job(old["id"])
        self.assertEqual((o["status"], o["note"]), ("done", "unverified after an interrupted re-sync"))
        self.assertTrue(os.path.isfile(self.m(HEAT)))
        self.assertEqual(self.c.post(f"/api/jobs/{up['id']}/remove").json(), {"result": "removed"})
        self.assertEqual(self.kinds(self.HEAT_DIR), {"101": "upgraded"})

    def test_replanning_refuses_another_server(self):
        with self.assertRaises(plexgw.OtherServer):
            self.gw.plan(["101"], server_id="other")
        r = self.c.post("/api/queue", json={"keys": ["101"], "serverId": "other"})
        self.assertEqual(r.status_code, 409)
        self.assertIsNone(self.store.next_queued())

    # review round 3 ----------------------------------------------------------
    def test_retry_after_an_abandoned_placement_keeps_the_new_file(self):
        self.c.post("/api/queue", json={"keys": ["102"]})
        jid = self.store.next_queued()["id"]
        real = self.dl._subtitles
        calls = {"n": 0}

        def boom(job):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("power cut")   # the video is placed, subtitles not done
            return real(job)
        with mock.patch.object(self.dl, "_subtitles", boom):
            self.dl._process(self.store.next_queued())
            self.assertEqual(self.store.job(jid)["phase"], "placed")
            with open(self.m(LOOSE), "r+b") as fh:
                fh.truncate(5)                    # changed on the NAS before it finished
            self.dl._later.clear()
            with mock.patch.object(worker, "remove_files", side_effect=PermissionError(13, "Permission denied")):
                self.dl._process(self.store.next_queued())
        job = self.store.job(jid)
        self.assertEqual((job["status"], job["phase"]), ("failed", None))
        self.assertIn(LOOSE, json.loads(job["cleanup"]))               # still to be deleted
        self.c.post(f"/api/jobs/{jid}/retry")
        self.drain()
        job = self.store.job(jid)
        self.assertEqual((job["status"], json.loads(job["cleanup"])), ("done", []))
        self.assertEqual(self.read(LOOSE), self.fake.parts[1002])      # the new copy survived

    def test_part_replaced_before_the_subtitle_step_keeps_subtitles(self):
        self.copy("101")
        rid = self.row("101")["id"]
        next(x for x in self.movie(101)["subs"] if x["id"] == 9001)["body"] = SRT_BODY("Nuevo texto")
        self.apply(self.HEAT_DIR, "101")
        self.new_part(self.movie(101), 1101)  # replaced again before the worker runs
        self.drain()
        job = self.store.job(rid)
        self.assertEqual(job["status"], "done")
        self.assertIn("subtitles not checked", job["error"])
        self.assertEqual(sorted(json.loads(job["sidecars"])), sorted([self.HEAT_EN, self.HEAT_ES]))
        self.assertTrue(os.path.isfile(self.m(self.HEAT_EN)))
        self.assertEqual(self.kinds(self.HEAT_DIR), {"101": "changed"})       # the next re-sync sees the new bytes

    # review round 4 ----------------------------------------------------------
    def test_a_later_refresh_never_reaches_the_replaced_copy(self):
        old, up = self.upgrade_queued()
        self.drain()
        done = self.store.job(up["id"])
        self.assertEqual(done["replaces"], "[]")
        with open(self.m(self.HEAT_ES), "w") as fh:     # someone puts a file where the old subtitle was
            fh.write("mine")
        self.store.transition(up["id"], lambda r: True, status="queued", mode="refresh")
        with mock.patch.object(self.gw, "subtitle_streams", side_effect=RuntimeError("Plex down")):
            self.drain()
        row = self.store.job(up["id"])
        self.assertNotIn(self.HEAT_ES, json.loads(row["sidecars"]))
        self.c.post(f"/api/jobs/{up['id']}/remove")
        self.assertEqual(self.read(self.HEAT_ES), b"mine")

    def test_removing_a_queued_redownload_deletes_the_copy(self):
        self.copy("102")
        rid = self.row("102")["id"]
        with open(self.m(LOOSE), "r+b") as fh:
            fh.truncate(10)
        self.assertEqual(self.apply("Movies", "102").json()["redownloads"], 1)
        r = self.c.post("/api/remove", json={"ratingKeys": ["102"], "serverId": "srv1"}).json()
        self.assertEqual(r["results"], {"removed": 1})
        self.assertEqual(self.store.job(rid)["status"], "removed")
        self.assertFalse(os.path.lexists(self.m(LOOSE)))

    # review round 5 ----------------------------------------------------------
    def crashed_while_placing(self):
        """An upgrade that committed T1 and crashed before its rename."""
        old, up = self.upgrade_queued()
        real = worker.os.replace

        def crash(src, dst):
            if ".travelarr-resync-" in src:
                raise KeyboardInterrupt  # stands in for the process dying
            return real(src, dst)
        with mock.patch.object(worker.os, "replace", crash):
            with self.assertRaises(KeyboardInterrupt):
                self.dl._process(self.store.next_queued())
        self.store.update(up["id"], status="queued")  # what the startup reset does
        job = self.store.job(up["id"])
        self.assertEqual(job["phase"], "placing")
        return old, job, self.m(store_mod.staging_rel(job))

    def test_recovery_refuses_a_truncated_staging_file(self):
        old, up, staging = self.crashed_while_placing()
        with open(staging, "r+b") as fh:
            fh.truncate(1000)
        self.drain()
        self.assertEqual(self.store.job(up["id"])["status"], "failed")
        self.assertEqual(self.store.job(old["id"])["status"], "done")
        self.assertEqual(self.read(HEAT), self.fake.parts[1001])
        self.assertFalse(os.path.lexists(self.m("Movies/Heat (1995)/Heat (1995) v2.mkv")))

    def test_recovery_refuses_a_substituted_staging_file(self):
        old, up, staging = self.crashed_while_placing()
        with open(staging, "r+b") as fh:
            fh.seek(150_000)
            fh.write(b"tampered")                 # same size, different content
        self.drain()
        self.assertEqual(self.store.job(up["id"])["status"], "failed")
        self.assertEqual(self.store.job(old["id"])["status"], "done")
        self.assertFalse(os.path.lexists(self.m("Movies/Heat (1995)/Heat (1995) v2.mkv")))

    def test_recovery_places_an_intact_staging_file(self):
        old, up, staging = self.crashed_while_placing()
        before = len(self.fake.requests)
        self.drain()
        self.assertEqual(self.part_requests(1101, before), [])
        self.assertEqual(self.store.job(up["id"])["status"], "done")
        self.assertEqual(self.read("Movies/Heat (1995)/Heat (1995) v2.mkv"), self.fake.parts[1101])

    def test_retry_cannot_bring_back_a_removed_job(self):
        self.c.post("/api/queue", json={"keys": ["102"]})
        jid = self.store.next_queued()["id"]
        self.store.update(jid, status="failed")
        real = self.store.job

        def job(job_id):
            row = real(job_id)
            if row and row["status"] == "failed":
                self.store.remove(job_id)          # Remove commits right after Retry read the row
            return row
        with mock.patch.object(self.store, "job", job):
            r = self.c.post(f"/api/jobs/{jid}/retry")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(real(jid)["status"], "removed")

    # review round 6 ----------------------------------------------------------
    def test_retry_after_a_rejected_staging_file_downloads_from_zero(self):
        old, up, staging = self.crashed_while_placing()
        with open(staging, "r+b") as fh:
            fh.seek(150_000)
            fh.write(b"tampered")
        self.drain()
        self.assertFalse(os.path.lexists(staging))
        before = len(self.fake.requests)
        self.c.post(f"/api/jobs/{up['id']}/retry")
        self.drain()
        self.assertEqual([r[2].get("Range") for r in self.part_requests(1101, before)], [None])  # from zero
        self.assertEqual(self.read("Movies/Heat (1995)/Heat (1995) v2.mkv"), self.fake.parts[1101])

    def test_changed_file_retry_updates_the_same_row(self):
        self.c.post("/api/queue", json={"keys": ["102"]})
        jid = self.store.next_queued()["id"]
        self.store.update(jid, status="failed", error=worker.CHANGED + ": test")
        self.store.remove(jid)
        self.assertFalse(self.store.requeue_changed(jid, self.gw.plan(["102"])[0]))   # Remove came first
        self.assertEqual(self.store.job(jid)["status"], "removed")
        self.c.post("/api/queue", json={"keys": ["103"]})
        jid2 = self.store.next_queued()["id"]
        self.store.update(jid2, status="failed", error=worker.CHANGED + ": test")
        self.new_part(self.movie(103), 1203, file="/data/Media/Movies/Heathers (1989)/Heathers v2.mkv", size=60_000)
        self.assertEqual(self.c.post(f"/api/jobs/{jid2}/retry").json(), {"ok": True, "requeued": 1})
        job = self.store.job(jid2)
        self.assertEqual((job["status"], job["part_id"], job["size"]), ("queued", 1203, 60_000))
        self.assertEqual(len([j for j in self.store.jobs("active") if j["rating_key"] == "103"]), 1)  # one live row
        self.drain()
        self.assertEqual(self.read("Movies/Heathers (1989)/Heathers v2.mkv"), self.fake.parts[1203])

    def test_changed_file_retry_keeps_the_copy_for_resync(self):
        for mode in ("redownload", "recover"):
            with self.subTest(mode=mode):
                self.copy("102")
                old = self.row("102")
                if mode == "redownload":
                    with open(self.m(LOOSE), "r+b") as fh:
                        fh.truncate(10)
                    self.assertEqual(self.apply("Movies", "102").json()["redownloads"], 1)
                else:                                                  # a lost final rename
                    shutil.copyfile(self.m(LOOSE), self.m(LOOSE) + paths.PART_SUFFIX)
                    self.assertTrue(self.store.requeue_lost(old["id"]))
                m = self.movie(102)
                new = bytes((i * 7 + len(mode)) % 251 for i in range(m["size"] + 5_000))
                self.fake.parts[m["part"]], m["size"] = new, len(new)  # Plex's file changes meanwhile
                self.drain()
                job = self.store.job(old["id"])
                self.assertEqual((job["status"], job["mode"]), ("failed", mode))
                self.assertTrue(job["error"].startswith(worker.CHANGED), job["error"])

                r = self.c.post(f"/api/jobs/{old['id']}/retry")
                self.assertEqual((r.status_code, r.json()["code"]), (409, "changed"))
                job = self.store.job(old["id"])
                self.assertEqual((job["status"], job["mode"], job["error"], job["dest_rel"]),
                                 ("done", None, None, LOOSE))
                self.assertEqual(self.store.status_map(["102"], "srv1"), {"102": "done"})   # no plain Copy offered
                self.assertFalse(os.path.lexists(self.m(LOOSE) + paths.PART_SUFFIX))

                self.assertEqual(self.kinds("Movies"), {"102": "changed"})
                self.assertEqual(self.apply("Movies", "102").json()["upgrades"], 1)
                self.drain()
                self.assertEqual(self.store.job(old["id"])["status"], "removed")
                done = self.row("102")
                self.assertEqual((done["mode"], done["dest_rel"]), ("upgrade", LOOSE))
                self.assertEqual(self.read(LOOSE), new)
                self.assertEqual(self.check("Movies")["items"], [])
                self.c.post(f"/api/jobs/{done['id']}/remove")
                self.assertFalse(os.path.lexists(self.m(LOOSE)))

    # servers -----------------------------------------------------------------
    def test_jobs_of_another_server_wait(self):
        self.c.post("/api/queue", json={"keys": ["102"]})
        jid = self.store.next_queued()["id"]
        self.store.update(jid, server_id="other")
        self.assertIsNone(self.store.next_queued("srv1"))
        self.assertTrue(next(j for j in self.c.get("/api/jobs").json()["jobs"] if j["id"] == jid)["waiting"])
        self.store.update(jid, status="failed")
        self.assertEqual(self.c.post(f"/api/jobs/{jid}/retry").status_code, 409)

    def test_server_change_before_placement_waits(self):
        self.c.post("/api/queue", json={"keys": ["102"]})
        jid = self.store.next_queued()["id"]
        real = self.gw.server_for

        def server_for(sid):
            if self.store.job(jid)["bytes_done"] == 120_000:    # downloaded, about to place
                raise plexgw.OtherServer("changed")
            return real(sid)
        with mock.patch.object(self.gw, "server_for", server_for):
            self.dl._process(self.store.next_queued())
        job = self.store.job(jid)
        self.assertEqual((job["status"], job["note"], job["phase"]), ("queued", worker.WAITING, None))
        self.assertTrue(os.path.isfile(self.m(LOOSE) + paths.PART_SUFFIX))   # kept for later
        self.drain()
        self.assertEqual(self.store.job(jid)["status"], "done")

    def test_badges_count_only_the_selected_server(self):
        self.copy("102")
        rid = self.row("102")["id"]
        self.assertEqual(self.store.status_map(["102"], "srv1"), {"102": "done"})
        self.store.update(rid, server_id="other")
        self.assertEqual(self.store.status_map(["102"], "srv1"), {})


class VersionTest(PlexFixture):
    """Of a title Plex has in several versions, the version setting picks which are copied."""
    HEAT_DIR = ResyncTest.HEAT_DIR
    HEAT_MP4 = "Movies/Heat (1995)/Heat (1995).mp4"
    HEAT_SUBS = ["Heat (1995).en.forced.srt", "Heat (1995).es.srt"]
    OPT = f"{MOVIES}/Heat (1995)/Plex Versions/Optimized for TV/Heat (1995).mp4"
    read, copy, check, kinds, apply, movie = (ResyncTest.read, ResyncTest.copy, ResyncTest.check, ResyncTest.kinds,
                                              ResyncTest.apply, ResyncTest.movie)

    def setUp(self):
        super().setUp()
        self.sign_in_and_connect()
        self.heat = self.movie(101)
        self.heat["subs"] = [x for x in self.heat["subs"] if x["id"] != 9005]   # the always-invalid one
        self.heat.update(res="4k", width=3840, bitrate=40_000)
        self.opt = self.fake.add_version(self.heat, 1501, self.OPT, 100_000, res="1080", width=1920, bitrate=8_000,
                                         optimized=True)

    def prefer(self, pref):
        r = self.c.post("/api/settings", json={"mediaVersion": pref})
        self.assertEqual((r.status_code, r.json().get("mediaVersion")), (200, pref), r.text)

    def pick(self, rk="101", version=None):
        """(part id, dest) of each file the movie page shows, and its size."""
        d = self.c.get(f"/api/item/{rk}", params={"mediaVersion": version} if version else None).json()
        return [(p["id"], p["dest"]) for p in d["parts"]], d["size"]

    def listed_size(self, rk="101"):
        return next(i["size"] for i in self.c.get("/api/library/1").json()["items"] if i["ratingKey"] == rk)

    def queue(self, *keys, **body):
        return self.c.post("/api/queue", json={"keys": list(keys), "mode": "all", **body}).json()

    def test_setting(self):
        self.assertEqual(self.c.get("/api/state").json()["mediaVersion"], "all")
        for bad in ("4k", None, 1080, "480"):
            self.assertEqual(self.c.post("/api/settings", json={"mediaVersion": bad}).status_code, 400, bad)
        self.assertEqual(self.c.get("/api/state").json()["mediaVersion"], "all")
        self.prefer("720")
        self.assertEqual(self.c.get("/api/state").json()["mediaVersion"], "720")

    def test_all_versions_is_the_default_and_copies_each_at_plexs_path(self):
        opt = "Movies/Heat (1995)/Plex Versions/Optimized for TV/Heat (1995).mp4"
        self.assertEqual(self.pick(), ([(1001, HEAT), (1501, opt)], 400_000))
        self.assertEqual(self.listed_size(), 400_000)
        r = self.queue("101")
        self.assertEqual((r["added"], r["bytes"]), (2, 400_000))
        self.drain()
        self.assertEqual((self.read(HEAT), self.read(opt)), (self.fake.parts[1001], self.fake.parts[1501]))

    def test_one_transfer_overrides_the_setting(self):
        self.prefer("1080")
        self.assertEqual(self.pick(version="original"), ([(1001, HEAT)], 300_000))
        self.assertEqual(self.pick(), ([(1501, self.HEAT_MP4)], 100_000))
        r = self.queue("101", mediaVersion="original")
        self.assertEqual((r["added"], r["bytes"]), (1, 300_000))
        self.drain()
        self.assertTrue(os.path.isfile(self.m(HEAT)))
        self.assertFalse(os.path.exists(self.m(self.HEAT_MP4)))
        self.assertEqual(self.c.get("/api/state").json()["mediaVersion"], "1080")    # the setting is unchanged
        self.assertEqual(self.c.post("/api/queue", json={"keys": ["101"], "mediaVersion": "4k"}).status_code, 400)
        self.assertEqual(self.c.get("/api/item/101", params={"mediaVersion": "4k"}).status_code, 400)

    def test_original_copies_only_the_original(self):
        self.prefer("original")
        self.assertEqual(self.pick(), ([(1001, HEAT)], 300_000))
        self.assertEqual(self.listed_size(), 300_000)
        r = self.queue("101")
        self.assertEqual((r["added"], r["bytes"]), (1, 300_000))
        self.drain()
        self.assertEqual(self.read(HEAT), self.fake.parts[1001])
        self.assertEqual(sorted(os.listdir(self.m(self.HEAT_DIR))), sorted(self.HEAT_SUBS + ["Heat (1995).mkv"]))
        d = self.check(self.HEAT_DIR)                     # two versions on Plex: compared, not "multipart"
        self.assertEqual((d["unchanged"], d["items"]), (1, []))

    def test_optimized_version_lands_under_the_originals_name(self):
        self.prefer("1080")
        self.assertEqual(self.pick(), ([(1501, self.HEAT_MP4)], 100_000))
        self.assertEqual(self.listed_size(), 100_000)
        r = self.queue("101")
        self.assertEqual((r["added"], r["bytes"]), (1, 100_000))
        self.drain()
        self.assertEqual(self.read(self.HEAT_MP4), self.fake.parts[1501])
        # no Plex Versions folder, and the original's subtitles named after the copy
        self.assertEqual(sorted(os.listdir(self.m(self.HEAT_DIR))), sorted(self.HEAT_SUBS + ["Heat (1995).mp4"]))
        self.assertEqual(self.read("Movies/Heat (1995)/Heat (1995).es.srt"), SRT_BODY("Hola").encode())
        d = self.check(self.HEAT_DIR)
        self.assertEqual((d["unchanged"], d["items"]), (1, []))
        self.assertEqual(self.c.get("/api/item/101").json()["nasFolder"], self.HEAT_DIR)
        self.assertEqual(self.queue("101")["added"], 0)

    def test_a_versions_own_subtitles_win(self):
        self.opt["subs"] = [dict(id=9501, codec="srt", lang="fr", ext=True, body=SRT_BODY("Salut"))]
        self.prefer("1080")
        self.copy("101")
        self.assertEqual(sorted(os.listdir(self.m(self.HEAT_DIR))), ["Heat (1995).fr.srt", "Heat (1995).mp4"])

    def test_choosing_between_versions(self):
        self.fake.add_version(self.heat, 1502, f"{MOVIES}/Heat (1995)/Plex Versions/Optimized for Mobile/Heat (1995).mp4",
                              60_000, res="720", width=1280, bitrate=4_000, optimized=True)
        other = f"{MOVIES}/Heat (1995)/Heat (1995) - 1080p.mkv"   # no videoResolution: its width says 1080p
        self.fake.add_version(self.heat, 1503, other, 150_000, width=1920, bitrate=12_000)
        mobile = "Movies/Heat (1995)/Heat (1995).mp4"
        expect = {"original": (1001, HEAT, 300_000), "smallest": (1502, mobile, 60_000), "2160": (1001, HEAT, 300_000),
                  "1080": (1503, "Movies/Heat (1995)/Heat (1995) - 1080p.mkv", 150_000), "720": (1502, mobile, 60_000),
                  "sd": (1502, mobile, 60_000)}                     # nothing fits SD: the smallest
        for pref, (pid, dest, size) in expect.items():
            self.prefer(pref)
            self.assertEqual(self.pick(), ([(pid, dest)], size), pref)
            self.assertEqual(self.listed_size(), size, pref)
        self.heat["versions"] = [v for v in self.heat["versions"] if v["part"] != 1502]
        self.prefer("720")
        self.assertEqual(self.pick(), ([(1501, self.HEAT_MP4)], 100_000))   # nothing fits 720p now: the smallest
        r = self.queue("101")
        self.assertEqual((r["added"], r["bytes"]), (1, 100_000))

    def test_at_the_same_resolution_the_original_wins(self):
        # as on a real server: an 'Original Quality' optimized copy of a 4K file, bigger and of a higher bitrate
        big = f"{MOVIES}/Heat (1995)/Plex Versions/Original Quality/Heat (1995).mp4"
        self.heat["versions"] = []
        self.fake.add_version(self.heat, 1504, big, 600_000, res="4k", width=3840, bitrate=90_000, optimized=True)
        for pref in ("2160", "original"):
            self.prefer(pref)
            self.assertEqual(self.pick(), ([(1001, HEAT)], 300_000), pref)
        self.prefer("1080")                                                # nothing fits: the smallest
        self.assertEqual(self.pick(), ([(1001, HEAT)], 300_000))

    def test_two_originals_of_one_resolution(self):
        loose = next(x for x in self.fake.movies if x["rk"] == 102)       # 120,000 bytes
        loose.update(res="1080", width=1920, bitrate=2_000)
        self.fake.add_version(loose, 1505, f"{MOVIES}/Loose Movie - Remux.mkv", 840_000, res="1080", width=1920,
                              bitrate=14_000)
        same = next(e for e in self.fake.episodes if e["rk"] == 201)      # two identical files
        same.update(res="1080", width=1920, bitrate=5_000)
        self.fake.add_version(same, 2505, f"{TV}/Show A/Season 01/Show A - S01E01 - Pilot (copy).mkv", same["size"],
                              res="1080", width=1920, bitrate=5_000)
        for pref, pid in (("2160", 1505), ("1080", 1505), ("720", 1002), ("sd", 1002)):
            self.prefer(pref)
            self.assertEqual(self.pick("102")[0][0][0], pid, pref)
            self.assertEqual(len(self.pick("201")[0]), 1, pref)

    def test_optimized_version_without_the_original(self):
        self.prefer("original")
        self.heat.update(file=self.OPT, part=1501, size=100_000, optimized=True, versions=[])
        self.assertEqual(self.pick(), ([(1501, self.HEAT_MP4)], 100_000))

    def test_episodes(self):
        ep = next(e for e in self.fake.episodes if e["rk"] == 202)
        ep["res"] = "4k"
        self.fake.add_version(ep, 2502, f"{TV}/Show A/Season 01/Plex Versions/Optimized for TV/Show A - S01E02 - Second.mp4",
                              30_000, res="720", optimized=True)
        mp4 = "TV-Shows/Show A/Season 01/Show A - S01E02 - Second.mp4"
        self.prefer("1080")
        d = self.c.get("/api/item/200").json()
        e = next(e for s in d["seasons"] for e in s["episodes"] if e["ratingKey"] == "202")
        self.assertEqual((e["size"], e["dest"], d["size"]), (30_000, mp4, 80_000 + 30_000 + 70_000))
        r = self.queue("200")
        self.assertEqual((r["added"], r["bytes"]), (3, 180_000))
        self.drain()
        self.assertEqual(self.read(mp4), self.fake.parts[2502])
        self.assertTrue(os.path.isfile(self.m("TV-Shows/Show A/Season 01/Show A - S01E02 - Second.es.srt")))
        self.assertEqual(self.check("TV-Shows/Show A")["items"], [])

    def test_resync_replaces_the_copy_of_the_other_version(self):
        self.prefer("original")
        self.copy("101")                                                   # the 4K original
        self.prefer("1080")
        r = self.queue("101")                                              # never a second copy beside it
        self.assertEqual((r["added"], r["skipped"]), (0, 1))
        self.assertEqual(self.kinds(self.HEAT_DIR), {"101": "upgraded"})
        item = self.check(self.HEAT_DIR)["items"][0]
        self.assertEqual(item["video"], {"from": {"file": "Heat (1995).mkv", "size": 300_000},
                                         "to": {"file": "Heat (1995).mp4", "size": 100_000}})
        r = self.apply(self.HEAT_DIR, "101").json()
        self.assertEqual((r["upgrades"], r["videoBytes"]), (1, 100_000))
        self.drain()
        self.assertEqual(sorted(os.listdir(self.m(self.HEAT_DIR))), sorted(self.HEAT_SUBS + ["Heat (1995).mp4"]))
        self.assertEqual(self.read(self.HEAT_MP4), self.fake.parts[1501])
        self.assertEqual(self.read("Movies/Heat (1995)/Heat (1995).es.srt"), SRT_BODY("Hola").encode())
        self.assertEqual(self.check(self.HEAT_DIR)["items"], [])
        self.prefer("original")                                            # and back
        self.assertEqual(self.kinds(self.HEAT_DIR), {"101": "upgraded"})

    def test_a_file_of_the_other_version_stays(self):
        self.prefer("1080")
        self.copy("101")
        with open(self.m(HEAT), "wb") as fh:                               # the original, copied some other way
            fh.write(self.fake.parts[1001])
        self.assertEqual(self.kinds(self.HEAT_DIR), {HEAT: "otherversion"})
        r = self.apply(self.HEAT_DIR).json()
        self.assertEqual((r["upgrades"], r["skipped"]), (0, 0))
        self.assertTrue(os.path.isfile(self.m(HEAT)))


class StoreIndexTest(unittest.TestCase):
    def test_duplicate_check_uses_dest_index_on_old_databases(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "travelarr.db")
        old = sqlite3.connect(path)
        old.executescript(store_mod.SCHEMA.replace("CREATE INDEX IF NOT EXISTS jobs_dest   ON jobs(dest_rel);", ""))
        self.assertNotIn("jobs_dest", {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='index'")})
        old.close()
        st = store_mod.Store(path)
        self.addCleanup(st.close)
        plan = " ".join(r["detail"] for r in st.rows(
            "EXPLAIN QUERY PLAN SELECT id FROM jobs WHERE (part_id=? OR dest_rel=?) AND status IN "
            "('queued','downloading','done')", (1, "x")))
        self.assertIn("jobs_dest", plan)
        self.assertIn("jobs_part", plan)


class MigrationTest(unittest.TestCase):
    def test_old_database_gains_the_resync_columns(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "travelarr.db")
        old = sqlite3.connect(path)
        old.executescript(store_mod.SCHEMA)
        old.execute("INSERT INTO jobs(rating_key, kind, title, part_id, part_key, size, dest_rel, status) "
                    "VALUES('1','movie','A',1,'/k',5,'Movies/A/a.mkv','done')")
        old.commit()
        old.close()
        st = store_mod.Store(path)
        self.addCleanup(st.close)
        row = st.job(1)
        for col in store_mod.NEW_COLUMNS:
            self.assertIn(col, row)
        self.assertEqual((row["replaces"], row["cleanup"], row["server_id"]), ("[]", "[]", None))


class AuthTest(unittest.TestCase):
    PW = "Zq9-pw-7731"  # distinctive, so a log line can only contain it if it was built from it

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def app(self, env=None):
        return create_app(data_dir=self.tmp, media_root=self.tmp, env=env or {}, start_worker=False,
                          gateway_factory=lambda s: PlexGateway(s, "t"))

    def client(self, env=None):
        c = TestClient(self.app(env))
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return c

    def enable(self, c, user="admin", pw=PW):
        r = c.post("/api/security", json={"action": "enable", "user": user, "password": pw, "repeat": pw})
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def test_hashing(self):
        a, b = auth_mod.hash_password(self.PW), auth_mod.hash_password(self.PW)
        self.assertNotEqual(a, b)                                   # a new salt every time
        self.assertTrue(a.startswith("scrypt$16384$8$1$"))
        self.assertTrue(auth_mod.verify_password(self.PW, a))
        self.assertFalse(auth_mod.verify_password(self.PW + "x", a))
        for junk in (None, "", "scrypt$1$2", "plain"):
            self.assertFalse(auth_mod.verify_password(self.PW, junk))

    def test_off_by_default(self):
        with self.assertLogs("travelarr", "INFO") as cm:
            c = self.client()
        self.assertTrue(any(r.levelname == "WARNING" and "sign-in is off" in r.getMessage() for r in cm.records))
        self.assertEqual(c.get("/api/state").status_code, 200)
        self.assertEqual(c.get("/api/session").json(), {"authRequired": False, "signedIn": False, "user": None})
        self.assertEqual(c.post("/api/login", json={"user": "admin", "password": "x"}).status_code, 409)

    def test_turn_on_sign_in_and_out(self):
        c = self.client()
        for body, why in [({"user": "ad min", "password": self.PW, "repeat": self.PW}, "User name"),
                          ({"user": "admin", "password": "short", "repeat": "short"}, "8 to 256"),
                          ({"user": "admin", "password": self.PW, "repeat": "other"}, "do not match")]:
            r = c.post("/api/security", json={"action": "enable", **body})
            self.assertEqual(r.status_code, 400)
            self.assertIn(why, r.json()["error"])
        r = self.enable(c)
        cookie = r.headers["set-cookie"]
        for flag in ("travelarr_session=", "HttpOnly", "SameSite=strict", "Path=/", "Max-Age=2592000"):
            self.assertIn(flag, cookie)
        self.assertNotIn("Secure", cookie)
        self.assertEqual(c.get("/api/state").json()["authUser"], "admin")
        self.assertEqual(c.get("/api/session").json(), {"authRequired": True, "signedIn": True, "user": "admin"})
        self.assertEqual(self.enable_again(c), 409)

        other = TestClient(c.app)                                     # another browser, no session
        r = other.get("/api/state")
        self.assertEqual((r.status_code, r.json()["code"]), (401, "signin"))
        self.assertEqual(other.get("/api/thumb?path=/library/x").status_code, 401)
        self.assertEqual(other.get("/").status_code, 200)
        self.assertEqual(other.get("/static/app.js").status_code, 200)
        self.assertEqual(other.get("/api/session").json(), {"authRequired": True, "signedIn": False, "user": None})
        r = other.post("/api/login", json={"user": "admin", "password": "nope"})
        self.assertEqual((r.status_code, r.json()["code"]), (401, "wrong"))
        self.assertEqual(other.post("/api/login", json={"user": "Admin", "password": self.PW}).status_code, 401)
        self.assertEqual(other.post("/api/login", json={"user": "admin", "password": self.PW}).json(), {"user": "admin"})
        self.assertEqual(other.get("/api/state").status_code, 200)

        r = c.post("/api/logout", json={})
        self.assertIn('travelarr_session=""', r.headers["set-cookie"])
        self.assertEqual(c.get("/api/state").status_code, 401)
        self.assertEqual(other.get("/api/state").status_code, 200)    # only that browser signed out

    @staticmethod
    def at(t):
        return mock.patch.object(auth_mod, "time", types.SimpleNamespace(time=lambda: t, monotonic=time.monotonic))

    def enable_again(self, c):
        return c.post("/api/security", json={"action": "enable", "user": "x", "password": self.PW,
                                             "repeat": self.PW}).status_code

    def test_change_password_signs_out_other_browsers(self):
        c = self.client()
        self.enable(c)
        other = TestClient(c.app)
        other.post("/api/login", json={"user": "admin", "password": self.PW})
        new = "new-" + self.PW
        r = c.post("/api/security", json={"action": "change", "current": "nope", "password": new, "repeat": new})
        self.assertEqual((r.status_code, r.json()["code"]), (403, "wrong"))
        r = c.post("/api/security", json={"action": "change", "current": self.PW, "password": "short", "repeat": "short"})
        self.assertEqual(r.status_code, 400)
        r = c.post("/api/security", json={"action": "change", "current": self.PW, "password": new, "repeat": new})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(c.get("/api/state").status_code, 200)        # this browser stays signed in
        self.assertEqual(other.get("/api/state").status_code, 401)
        self.assertEqual(other.post("/api/login", json={"user": "admin", "password": self.PW}).status_code, 401)
        self.assertEqual(other.post("/api/login", json={"user": "admin", "password": new}).status_code, 200)

    def test_change_and_disable_need_a_session(self):
        c = self.client()
        self.enable(c)
        other = TestClient(c.app)
        for body in ({"action": "change", "current": self.PW, "password": "x" * 9, "repeat": "x" * 9},
                     {"action": "disable", "current": self.PW}):
            self.assertEqual(other.post("/api/security", json=body).status_code, 401)
        self.assertEqual(c.post("/api/security", json={"action": "nope"}).status_code, 400)

    def test_turn_off(self):
        c = self.client()
        self.enable(c)
        self.assertEqual(c.post("/api/security", json={"action": "disable", "current": "nope"}).status_code, 403)
        r = c.post("/api/security", json={"action": "disable", "current": self.PW})
        self.assertEqual(r.status_code, 200)
        self.assertIn('travelarr_session=""', r.headers["set-cookie"])
        self.assertEqual(TestClient(c.app).get("/api/state").status_code, 200)
        self.assertEqual(c.app.state.store.rows("SELECT * FROM sessions"), [])
        self.assertIsNone(c.app.state.store.get("auth")["hash"])
        self.enable(c, user="road")                                   # and on again, with a new user name
        self.assertEqual(c.get("/api/state").json()["authUser"], "road")

    def test_lockout_after_five_wrong_passwords(self):
        c = self.client()
        self.enable(c)
        other = TestClient(c.app)
        clock = [1000.0]
        fake = types.SimpleNamespace(time=time.time, monotonic=lambda: clock[0])
        with mock.patch.object(auth_mod, "time", fake):
            codes = [other.post("/api/login", json={"user": "admin", "password": "nope"}).status_code
                     for _ in range(5)]
            self.assertEqual(codes, [401] * 5)
            r = other.post("/api/login", json={"user": "admin", "password": self.PW})   # right, but locked
            self.assertEqual((r.status_code, r.json()["code"], r.headers["retry-after"]), (429, "throttled", "300"))
            clock[0] += auth_mod.LOCK
            self.assertEqual(other.post("/api/login", json={"user": "admin", "password": self.PW}).status_code, 200)

    def test_session_expiry_and_renewal(self):
        c = self.client()
        t0 = time.time()
        with self.at(t0):
            self.enable(c)
        day = 24 * 3600
        with self.at(t0 + 10 * day):
            r = c.get("/api/state")
            self.assertEqual(r.status_code, 200)
            self.assertNotIn("set-cookie", r.headers)
        with self.at(t0 + 20 * day):
            r = c.get("/api/state")                                   # past half its life: renewed
            self.assertIn("travelarr_session=", r.headers["set-cookie"])
        with self.at(t0 + 50 * day):                                  # 30 days after the renewal
            self.assertEqual(c.get("/api/state").status_code, 401)
        with self.at(t0 + 50 * day + 1):
            self.assertEqual(c.get("/api/state").status_code, 401)

    def test_reset_variable(self):
        c = self.client()
        self.enable(c)
        with self.assertLogs("travelarr", "WARNING") as cm:
            c = self.client({"TRAVELARR_RESET_LOGIN": "yes"})
        self.assertTrue(any("TRAVELARR_RESET_LOGIN" in r.getMessage() for r in cm.records))
        self.assertEqual(TestClient(c.app).get("/api/state").status_code, 200)
        self.assertEqual(c.app.state.store.rows("SELECT * FROM sessions"), [])
        c = self.client()                                              # without it: still off
        self.assertFalse(c.get("/api/session").json()["authRequired"])

    def test_changes_need_the_page_header(self):
        c = self.client()
        bare = testclient.TestClient(c.app)                           # what another site can send
        r = bare.post("/api/security", json={"action": "enable", "user": "admin", "password": self.PW,
                                             "repeat": self.PW})
        self.assertEqual((r.status_code, r.json()["code"]), (403, "reload"))
        self.assertFalse(c.get("/api/session").json()["authRequired"])
        self.assertEqual(bare.post("/api/auth/signout").status_code, 403)
        self.assertEqual(bare.get("/api/state").status_code, 200)

    def test_reserve_must_be_a_number(self):
        c = self.client()
        for bad in ("inf", "nan", "", "ten", None, [1]):
            self.assertEqual(c.post("/api/settings", json={"reserveGb": bad}).status_code, 400, bad)
        self.assertEqual(c.post("/api/settings", json={"reserveGb": "-3"}).json()["reserveGb"], 0.0)

    def test_hash_and_password_never_leave(self):
        with self.assertLogs("travelarr", "INFO") as cm:
            c = self.client()
            self.enable(c)
            TestClient(c.app).post("/api/login", json={"user": "admin", "password": "wrong-" + self.PW})
        stored = c.app.state.store.get("auth")["hash"]
        self.assertFalse(any(self.PW in r.getMessage() for r in cm.records))
        body = c.get("/api/state").text + c.get("/api/session").text
        self.assertNotIn(stored.split("$")[-1], body)
        self.assertNotIn("hash", body)

    def test_jobs_limit_is_bounded(self):
        c = self.client()
        seen = []
        with mock.patch.object(c.app.state.store, "jobs", lambda *a: seen.append(a) or []):
            c.get("/api/jobs?view=history&limit=-1&offset=-5")
            c.get("/api/jobs?view=history&limit=5000")
        self.assertEqual([(a[2], a[3]) for a in seen], [(1, 0), (1000, 0)])


class PathsTest(unittest.TestCase):
    def test_dest_rel(self):
        f = paths.dest_rel_for
        self.assertEqual(f("/data/Media/TV-Shows/S/Season 01/e.mkv", ["/data/Media/TV-Shows"], "TV"),
                         "TV-Shows/S/Season 01/e.mkv")
        self.assertEqual(f("/data/a/TV/S/e.mkv", ["/data/a", "/data/a/TV"], "TV"), "TV/S/e.mkv")
        self.assertEqual(f("D:\\Media\\Movies\\M (1)\\m.mkv", ["D:\\Media\\Movies"], "Films"), "Movies/M (1)/m.mkv")
        self.assertEqual(f("/elsewhere/X/m.mkv", ["/data/Movies"], "Films: HD"), "Films  HD/X/m.mkv")
        self.assertEqual(f("/data/Movies/m.mkv", ["/data/Movies/"], "Films"), "Movies/m.mkv")
        with self.assertRaises(paths.PathError):
            paths.clean_rel("../x/y")

    def test_abs_path_and_remove_guard(self):
        tmp = tempfile.mkdtemp()
        try:
            for bad in ("Movies", "../x/y", "/etc/passwd", "Movies/../../x", ""):
                with self.assertRaises(paths.PathError):
                    paths.abs_path(tmp, bad)
            os.makedirs(os.path.join(tmp, "Movies", "A"))
            open(os.path.join(tmp, "Movies", "A", "a.mkv"), "w").close()
            paths.remove_files(tmp, ["Movies/A/a.mkv"])
            self.assertFalse(os.path.exists(os.path.join(tmp, "Movies", "A")))
            self.assertTrue(os.path.isdir(os.path.join(tmp, "Movies")))
            # a symlinked folder pointing outside must not be followed
            outside = tempfile.mkdtemp()
            os.symlink(outside, os.path.join(tmp, "Movies", "link"))
            with self.assertRaises(paths.PathError):
                paths.abs_path(tmp, "Movies/link/x.mkv")
            shutil.rmtree(outside)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_version_paths(self):
        opt = "/data/Movies/Heat (1995)/Plex Versions/Optimized for TV/Heat (1995).mp4"
        self.assertTrue(paths.in_versions(opt))
        self.assertFalse(paths.in_versions("/data/Movies/Plex Versions.mkv"))
        self.assertEqual(paths.unversioned(opt), "/data/Movies/Heat (1995)/Heat (1995).mp4")
        self.assertEqual(paths.unversioned("/data/Movies/M/Plex Versions/m.mp4"), "/data/Movies/M/m.mp4")
        self.assertEqual(paths.unversioned("/data/Movies/M/m.mkv"), "/data/Movies/M/m.mkv")
        self.assertEqual(paths.version_rel("Movies/Heat (1995)/Heat (1995).mkv", opt), "Movies/Heat (1995)/Heat (1995).mp4")

    def test_sidecar_names(self):
        used = set()
        s = lambda lang, forced=False, hi=False: paths.sidecar_rel("TV/S/e.mkv", lang, forced, hi, "srt", used)
        self.assertEqual(s("es"), "TV/S/e.es.srt")
        self.assertEqual(s("es"), "TV/S/e.es.2.srt")
        self.assertEqual(s("en", forced=True), "TV/S/e.en.forced.srt")
        self.assertEqual(s("en", hi=True), "TV/S/e.en.sdh.srt")
        self.assertEqual(s(None), "TV/S/e.und.srt")
        self.assertEqual(s("pt-BR"), "TV/S/e.pt-BR.srt")


if __name__ == "__main__":
    unittest.main()
