<p align="center">
  <img src="unraid/travelarr.png" width="110" alt="travelarr logo">
</p>

<h1 align="center">travelarr</h1>

<p align="center">
  <a href="https://github.com/alien4u/travelarr/actions/workflows/image.yml"><img alt="Build" src="https://github.com/alien4u/travelarr/actions/workflows/image.yml/badge.svg"></a>
  <a href="https://github.com/alien4u/travelarr/pkgs/container/travelarr"><img alt="Version" src="https://img.shields.io/github/v/tag/alien4u/travelarr?label=version&logo=github"></a>
  <a href="https://github.com/alien4u/travelarr/pkgs/container/travelarr"><img alt="GHCR" src="https://img.shields.io/badge/ghcr.io-alien4u%2Ftravelarr-blue?logo=docker&logoColor=white"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/github/license/alien4u/travelarr"></a>
</p>

**travelarr** fills your travel NAS with movies and TV shows from your home Plex server, so you can watch them on the
road without streaming.

Hotel Wi-Fi, Starlink and mobile hotspots are poor places to stream from home. travelarr runs on the NAS you take
with you: sign in with Plex, pick what you want to watch, and it copies the **original files** into a share on the
NAS. Play them with Infuse or any player that reads a network share. No transcoding, and no Plex server needed on the
travel NAS.

## Features

- Sign in with your Plex account and pick one of your servers.
- Browse your Watchlist, Movies and TV Shows with posters and search, sorted A-Z or by Recently added.
- Copy movies, whole shows, seasons or single episodes. Unwatched episodes are preselected and the total size is shown
  before you start.
- **Select** in Movies, TV Shows and Watchlist to pick many titles at once (shift-click for a range) and copy them in
  one go; selected shows copy their unwatched or all episodes.
- **Re-sync from Plex** in Local Files, for everything on the NAS or one folder: picks up videos upgraded by
  Radarr/Sonarr, copies missing files again, and fetches every copy's subtitles again (Lingarr). It shows which
  videos changed before anything is downloaded, and an old copy is only replaced once the new file is in place.
- Original files only, plus the external subtitles Plex knows about (`Movie.es.srt`, `Movie.en.forced.srt`).
  Subtitles inside the file come along automatically.
- Same folder layout as your home library (`Movies/...`, `TV-Shows/...`).
- Downloads resume after dropped connections and restarts, and every resume is checked against the file on Plex, so
  a bad connection never leaves a corrupt file.
- Uses your home LAN when you are home and Plex Remote Access when you are away. Plex Relay stays off unless you allow
  it.
- Queue with live speed and time left, and a History of everything copied.
- Local Files: browse the share by folder with sizes, and delete movies, shows, seasons or episodes once you have
  watched them.
- Keeps a free-space reserve on the NAS (10 GB by default).
- Optional sign-in page with a user name and password, turned on in Settings.

## Installation

The image is published for `linux/amd64` at `ghcr.io/alien4u/travelarr`. It runs as UID 99 / GID 100 (Unraid's
`nobody:users`), so the files it copies are owned like everything else on your shares.

### Unraid

travelarr is not in Community Applications. Add the template once and it then installs, updates and shows up like
any other Docker container, with its icon, WebUI and Logs.

1. Open the terminal (the `>_` icon at the top right of the Unraid web UI) and download the template:

   ```bash
   mkdir -p /boot/config/plugins/dockerMan/templates-user && \
   wget -O /boot/config/plugins/dockerMan/templates-user/my-travelarr.xml \
     https://raw.githubusercontent.com/alien4u/travelarr/main/unraid/travelarr.xml
   ```

2. **Docker > Add Container**, then choose **travelarr** under **Template**.
3. Check the **Media share** path and click **Apply**.
4. Open it from the travelarr icon > **WebUI**.

New versions show as **update ready** in the Docker tab.

> **Updating from 2.x:** the browser password prompt is replaced by a sign-in page. Your password carries over on the
> first start; after that the Username and Password variables can be removed, and the password is changed in
> **Settings > Authentication**.

### Docker Compose

```yaml
services:
  travelarr:
    image: ghcr.io/alien4u/travelarr:latest
    container_name: travelarr
    ports:
      - 8484:8484
    environment:
      - TRAVELARR_NAME=Travel NAS
      - TZ=Etc/UTC
    volumes:
      - /path/to/appdata/travelarr:/config
      - /path/to/media:/media
    restart: unless-stopped
```

Both folders must be writable by UID 99 / GID 100.

## Configuration

| Type | Name | Description |
|---|---|---|
| Port | `8484` | Web UI |
| Path | `/config` | Settings, copy history and the Plex sign-in (`travelarr.db`) |
| Path | `/media` | Where copies land: `Movies/...`, `TV-Shows/...`, created as needed |
| Variable | `TRAVELARR_RESET_LOGIN` | Forgot the password? Set to `yes` and restart: sign-in turns off. Remove it afterwards |
| Variable | `TRAVELARR_NAME` | What the UI calls this NAS ("Copy to ..."). Default: the Unraid server name, else the host name |
| Variable | `TRAVELARR_LOG` | `DEBUG`, `INFO` (default), `WARNING` or `ERROR` |
| Variable | `TZ` | Time zone for the times in History |

Everything else is set in the web UI under **Settings**.

## Getting started

1. **Sign in with Plex.** A Plex page opens; approve travelarr there. It then shows under Plex > Settings >
   Authorized Devices, where you can revoke it at any time.
2. **Choose your server.** Leave the fixed address empty.
3. **Turn on sign-in** in **Settings > Authentication**: choose a user name and password.
4. **Pick titles** and watch them copy in the **Queue**.

Fill the NAS at home if you can: copying over your own network is much faster than anything on the road.

## Away from home

- With the address left empty, travelarr finds your server through Plex. Away from home that needs Plex **Remote
  Access** showing as fully accessible, since Plex Relay is throttled. travelarr only uses Relay if you tick the box in
  Settings.
- On a VPN back home, leave the address empty too: travelarr tries the home LAN address first.
- The pill at the top shows how it is connected: `local`, `remote`, `manual` or `relay`.
- **Local falls back to remote at home?** Plex's LAN address is a name like `192-168-0-10.<id>.plex.direct`, and
  routers with DNS rebinding protection (Firewalla, pfSense, Pi-hole, many travel routers) block it. Allow
  `plex.direct` there (Unbound: `private-domain: "plex.direct"`).
- **Fixed address:** only for when the automatic choice fails. On a server that requires secure connections it must be
  the `https://...plex.direct` address shown in Settings; a bare IP fails the certificate check.

## Good to know

- After you sign in, travelarr holds a Plex token for your account. Turn on sign-in, keep the port on your LAN or
  travel router, and do not forward it to the internet.
- "Unwatched" is the watched state of your Plex account at home. What you watch from the NAS is not sent back to
  Plex.
- A file that is already on the NAS with the same name and size is added to the History instead of being copied
  again.
- Image-based subtitles stored as separate files (PGS, VobSub) are skipped and noted in the History.
- Re-sync spots a new video version by its Plex file, name and size (Plex offers no checksums), and covers only
  what travelarr copied.

## Support

travelarr is a personal project, shared as is. Bug reports are welcome in
[Issues](https://github.com/alien4u/travelarr/issues).

## Credits

Co-Authored with Claude Code.

## License

[MIT](LICENSE)
