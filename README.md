# travelarr

A small web app that runs on a travel NAS. Sign in with Plex, browse your watchlist, movies and shows on the **home** Plex server, pick what you want, and travelarr downloads the **original files** over HTTPS into the NAS's `Media` share. No Plex needed on the NAS; Infuse plays straight from the share.

* **Movies and TV Shows: A-Z or Recently added.** Recently added puts the newest movies first (cards show the date) and, for TV, the shows with the most recently added episodes first, so new episodes of an old show come up too. The choice is kept per tab until the page is reloaded.
* **Watchlist, Movies, TV Shows:** browse with posters and search. Shows open to a season and episode picker with unwatched episodes preselected and the total size shown.
* **Queue:** one file at a time, with progress, live speed (always shown, including 0 B/s while stalled) and time left. While a connection is being re-established the Queue says so and counts down to the next try. Downloads resume where they stopped after a dropped connection or a restart (HTTP Range), without risking the file:
  * progress is only recorded after the data is flushed to disk (fsync), so after a crash or power cut anything past that point is discarded and fetched again;
  * a resume is only made against the same file version (Plex's ETag, which Plex itself does not enforce on ranges) and the same Content-Range, and the last 64 KB before the resume point are downloaded again and compared with the disk;
  * if the file changed on Plex (Sonarr/Radarr upgrade), a same-size file starts over from zero and a different-size one fails with "The file changed on Plex since it was queued"; **Retry** then queues the item again as it is now;
  * a partial file that is a link or a hard link is replaced, never followed. Concurrent tampering with the share while a file downloads is out of scope.
* **Subtitles:** external subtitle files Plex knows about (your Lingarr `.es.srt` etc.) are saved next to the video as `Name.es.srt`, `Name.en.forced.srt`. Subtitles inside the MKV come along automatically.
* **History:** every file copied, with when it was queued, copied and removed, size, subtitle count and any problem. Stored in `/config/travelarr.db` (SQLite).
* **Remove from <device>** (detail panel): deletes only files travelarr copied, plus folders left empty. Never a library root, never anything you put there yourself. A file that is still copying is asked to stop instead; if it had already finished it is kept, and the toast says so.
* **Local Files:** browse the Media share by folder, with sizes, and delete a whole show, a season, single episodes, a movie folder or any file inside a library folder, including files travelarr did not copy. Select up to 500 at a time. The Media share itself and the library folders (`Movies`, `TV-Shows`) cannot be deleted, a link is removed without touching what it points to, and folders left empty are kept. Queued downloads inside a deleted folder are cancelled; a folder something is downloading into is refused until you cancel that download. Hidden files (`.DS_Store`) are not listed but count in folder sizes. Very large folders load 200 entries at a time, and sizes that took too long to count are shown as "at least".

Files land in the same layout as at home: Plex library folder `/data/Media/TV-Shows` becomes `TV-Shows/...` under the NAS's Media share. The detail panel shows the exact destination path before you copy.

## Install on Unraid (no terminal)

travelarr is a regular Unraid Docker container: it shows in the Docker tab with its icon, a **WebUI** button, **Logs**, start/stop and update notifications. The image is published at `ghcr.io/alien4u/travelarr`.

1. **Put the template on the NAS.** Enable the flash share once (Main > Boot Device > Flash > SMB Security Settings > Export: Yes). From your computer, open `smb://<nas-ip>/flash` (Windows: `\\<nas-ip>\flash`) and copy [`unraid/travelarr.xml`](unraid/travelarr.xml) into `config/plugins/dockerMan/templates-user/`. You can set the flash share back to not exported afterwards.
2. **Docker tab > Add Container**, then pick **travelarr** in the Template list. The form is filled in:
   * WebUI port `8484`
   * Appdata `/mnt/user/appdata/travelarr` (created on first start, owned by `nobody:users`)
   * Media share `/mnt/user/Media`: the NAS needs a `Media` share; `Movies` and `TV-Shows` folders are created as needed
   * **Password**: required. The browser asks for it (any username works).
3. **Apply.** Unraid pulls the image and starts the container. Click the travelarr icon > **WebUI**.

No template file? The same works by hand: Add Container, Name `travelarr`, Repository `ghcr.io/alien4u/travelarr:latest`, then add a Port `8484`, Paths `/config` and `/media`, and a Variable `TRAVELARR_PASSWORD`; in the advanced view set WebUI to `http://[IP]:[PORT:8484]/` and Icon URL to `https://raw.githubusercontent.com/alien4u/travelarr/main/unraid/travelarr.png`. Unraid saves it as a template either way.

**Device name:** the UI calls the NAS by the Unraid server name ("Copy to N2", "On N2"), which Unraid passes to every container. To use a different name, set **Device name** in the template's advanced view. Settings > Storage shows the name in use.

**Logs:** Docker tab > travelarr icon > **Logs**. For more detail, set **Log level** (advanced view) to `DEBUG`.

**Updates:** the Docker tab shows "update ready" when a new image is published; click **apply update**. History and the Plex sign-in live in the appdata folder and survive updates.

**Why the password matters:** after sign-in travelarr holds a Plex token for your account. Keep the port on the LAN or travel router network only; do not forward it to the internet.

## First run

1. **Sign in with Plex.** A Plex tab opens; approve travelarr there. It then shows under Plex > Settings > Authorized Devices, where you can revoke it any time.
2. **Choose your server.** Leave the fixed address empty unless you want to force a VPN address (see below).
3. Pick titles, watch the Queue.

## At home vs. on the trip

* **At home:** travelarr connects to Plex over the LAN automatically. Fill the NAS here; it is far faster than anything on the road.
* **Away:** with the address left empty, travelarr uses Plex Remote Access. Remote Access must show as fully accessible (direct), because Plex Relay is throttled. travelarr refuses Relay unless you tick the box. If you use the travel router's VPN back home instead, leave the address empty: travelarr tries the home LAN address first.
* **Local needs `plex.direct` to resolve.** With secure connections on, Plex's LAN address is a name like `192-168-0-10.<id>.plex.direct` that points to a private IP. Routers with DNS rebinding protection (Firewalla Unbound, pfSense, Pi-hole, many travel routers) block those answers, and travelarr falls back to remote. Allow `plex.direct` there (Unbound: `private-domain: "plex.direct"`).
* **Fixed address:** only needed when the automatic choice fails. On a server that requires secure connections it must be `https://` with a name the certificate covers, i.e. the `plex.direct` address shown in Settings. `http://` addresses are ignored, and a bare IP fails the certificate check.
* **Your Plex token:** travelarr checks addresses without it and sends it only to the one it picked. If the server requires secure connections, that is always HTTPS, and redirects to plain HTTP are refused. Only a server that itself allows plain HTTP can end up on `http://`, as a last resort. Relay addresses (only with the box ticked) get the token over HTTPS before they are checked.
* The pill at the top shows the connection: `local`, `remote`, `manual` or `relay`.
* Drops on hotel Wi-Fi or Starlink are retried with back-off, resuming mid-file. A download is marked failed only after about 7 minutes of consecutive failures without any new progress; drops that recover do not count against it. **Retry** in the Queue resumes from the partial file.

## Things to know

* "Unwatched" is the watched state of the Plex account you signed in with, at home.
* Infuse playing from the NAS keeps its own watch history. Your home Plex will not know what you watched on the trip.
* If a file with the same name and exact size is already on the NAS, travelarr adopts it into the history instead of downloading it again.
* Image-based subtitles (PGS, VobSub) stored as separate files are skipped and noted in the history.
* Fetching external subtitles uses Plex's `/library/streams/<id>` address. That is what Plex clients use, but Plex does not document it publicly, so every subtitle response is checked before saving. If the history shows "response is not a subtitle file" on every title, tell me and I'll adjust it.
* Downloads keep a reserve of free space on the NAS (10 GB by default; Settings).

## Development

```bash
cd src
pip install -r requirements.txt -r requirements-dev.txt
python -m unittest discover -s tests -t . -v     # 91 tests against a fake Plex server
pip install playwright && playwright install chromium
python tests/ui_smoke.py                          # drives the UI in headless Chromium
```

Run locally: `TRAVELARR_DATA=../local/config TRAVELARR_MEDIA=../local/media python -m app` (keep that data outside `src/`, the Docker build context; `local/` is git-ignored), then open `http://localhost:8484`.

**Publishing:** every push to `main` runs the tests and publishes `ghcr.io/alien4u/travelarr:latest` ([workflow](.github/workflows/image.yml)). For a numbered release, bump `VERSION` in `src/app/main.py`, then push a tag `vX.Y.Z` with the same number; that publishes `:X.Y.Z`, and version tags are never overwritten. If `:latest` ever lags behind `main` (GitHub can queue overlapping runs out of order), Actions > image > Run workflow on `main` publishes the current tip.

---

Co-Authored with Claude Code.
