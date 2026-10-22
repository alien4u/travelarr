"""
Browser smoke test: drives the real UI in headless Chromium against the app
running on a fake Plex. Fails on any JavaScript error.

Run:  python tests/ui_smoke.py [screenshot_dir]
"""
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright, expect  # noqa: E402

from app.main import create_app  # noqa: E402
from app.plexgw import PlexGateway  # noqa: E402
from tests.fakeplex import FakeAccount, FakePlex  # noqa: E402

shots = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
os.makedirs(shots, exist_ok=True)
tmp = tempfile.mkdtemp()
media = os.path.join(tmp, "media")
os.makedirs(os.path.join(media, "Movies"))
os.makedirs(os.path.join(media, "TV-Shows"))
fake = FakePlex()
# make files big enough that the queue shows progress for a moment
for pid in list(fake.parts):
    fake.parts[pid] = fake.parts[pid] * 40
for v in fake.movies + fake.episodes:
    v["size"] = len(fake.parts[v["part"]])


def factory(store):
    gw = PlexGateway(store, "ui-test")
    gw.plex_tv = fake.url
    gw.account = lambda: FakeAccount(fake, gw.session)
    return gw


app = create_app(data_dir=os.path.join(tmp, "config"), media_root=media, password="",
                 gateway_factory=factory, start_worker=True, device="N2")
app.state.dl.chunk = 64 * 1024
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8599, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
while not server.started:
    time.sleep(0.05)

errors = []
BASE = "http://127.0.0.1:8599"
with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1280, "height": 860}, color_scheme="dark")
    ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith(BASE) else r.abort())
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text
            and errors.append(f"console: {m.text}"))

    page.goto(BASE)
    expect(page.get_by_text("Connect travelarr to your Plex")).to_be_visible()
    page.screenshot(path=f"{shots}/1-signin.png")
    with ctx.expect_page() as popup:
        page.get_by_role("button", name="Sign in with Plex").click()
    popup.value.close()
    expect(page.get_by_text("Choose your Plex server")).to_be_visible(timeout=15000)
    page.screenshot(path=f"{shots}/2-server.png")
    page.get_by_role("button", name="Save and connect").click()

    expect(page.get_by_text("on your Plex watchlist")).to_be_visible(timeout=10000)
    expect(page.locator(".card")).to_have_count(3)
    expect(page.get_by_text("Not in your library")).to_be_visible()
    page.screenshot(path=f"{shots}/3-watchlist.png")

    page.get_by_role("button", name="Movies").click()
    expect(page.locator(".card")).to_have_count(3)
    page.get_by_placeholder("Search Movies").fill("heath")
    expect(page.locator(".card")).to_have_count(1, timeout=5000)
    page.get_by_placeholder("Search Movies").fill("")
    expect(page.locator(".card")).to_have_count(3, timeout=5000)
    page.locator(".card", has_text="Heat").first.click()
    expect(page.get_by_text("N2: Movies/Heat (1995)/Heat (1995).mkv")).to_be_visible()
    page.get_by_role("button", name="Copy to N2").click()
    expect(page.locator(".sticky-actions .chip")).to_be_visible(timeout=5000)
    page.keyboard.press("Escape")

    page.get_by_role("button", name="TV Shows").click()
    page.locator(".card", has_text="Show A").click()
    expect(page.get_by_text("Season 2")).to_be_visible()
    expect(page.get_by_text("null", exact=True)).to_have_count(0)
    copy = page.locator(".sticky-actions .btn.primary")
    expect(copy).to_contain_text("Copy 2 episode(s)")      # unwatched preselected
    page.screenshot(path=f"{shots}/4-show-detail.png")
    page.get_by_role("button", name="All", exact=True).click()
    expect(copy).to_contain_text("Copy 3 episode(s)")
    page.get_by_role("button", name="None", exact=True).click()
    expect(copy).to_be_disabled()
    page.get_by_role("button", name="Unwatched", exact=True).click()
    copy.click()
    expect(page.get_by_text("2 queued").or_(page.get_by_text("on N2"))).to_be_visible(timeout=5000)
    page.keyboard.press("Escape")

    page.get_by_role("button", name="Queue").click()
    page.screenshot(path=f"{shots}/5-queue.png")
    expect(page.get_by_text("Nothing queued.")).to_be_visible(timeout=30000)

    page.get_by_role("button", name="History").click()
    expect(page.locator(".row")).to_have_count(3, timeout=5000)
    expect(page.get_by_text("2/4 subtitles")).to_be_visible()
    page.screenshot(path=f"{shots}/6-history.png", full_page=True)

    page.get_by_role("button", name="Local Files").click()
    page.locator("#view").get_by_role("button", name="Movies", exact=True).click()
    page.get_by_label("Select Heat (1995)").check()
    page.once("dialog", lambda d: d.accept())
    page.get_by_role("button", name="Delete 1 selected").click()
    expect(page.get_by_text("Deleted 1 item(s)")).to_be_visible(timeout=5000)
    expect(page.get_by_text("Heat (1995)")).to_have_count(0)

    page.get_by_role("button", name="TV Shows").click()
    expect(page.locator(".badge", has_text="2 on N2")).to_be_visible(timeout=5000)
    page.locator(".card", has_text="Show A").click()
    page.once("dialog", lambda d: d.accept())
    page.get_by_role("button", name="Remove show from N2").click()
    expect(page.get_by_text("Nothing on N2 yet")).to_be_visible(timeout=5000)
    page.keyboard.press("Escape")

    page.get_by_role("button", name="Settings").click()
    expect(page.get_by_text("Signed in as alien")).to_be_visible()
    expect(page.get_by_text("Connected via local")).to_be_visible()
    page.screenshot(path=f"{shots}/7-settings.png", full_page=True)

    phone = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="light",
                                device_scale_factor=2)
    phone.route("**/*", lambda r: r.continue_() if r.request.url.startswith(BASE) else r.abort())
    m = phone.new_page()
    m.on("pageerror", lambda e: errors.append(f"pageerror(mobile): {e}"))
    m.goto(BASE + "/#tv")
    m.locator(".card", has_text="Show A").click()
    expect(m.get_by_text("Season 1")).to_be_visible()
    m.screenshot(path=f"{shots}/8-phone-show.png")
    expect(m.get_by_text("null", exact=True)).to_have_count(0)
    m.get_by_role("button", name="Close").click()
    m.get_by_role("button", name="History").click()
    expect(m.locator(".row").first).to_be_visible()
    m.screenshot(path=f"{shots}/9-phone-history.png")
    browser.close()

server.should_exit = True
fake.stop()
shutil.rmtree(tmp, ignore_errors=True)
if errors:
    print("JS ERRORS:\n" + "\n".join(errors))
    sys.exit(1)
print(f"UI smoke test passed; screenshots in {shots}")
