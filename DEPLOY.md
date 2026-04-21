# Deployment Guide

## Overview

The running instance is hosted on dockhost (`10.10.0.10`). All files live under `/opt/docker_data/mtdp/`.

The `docker-compose.yml` for the whole stack is at `/opt/docker_data/docker-compose.yml`.

The `mtdp-webui` container is built locally from `/opt/docker_data/mtdp/webui/` — `app.py` and templates are baked into the image at build time and are **not** volume-mounted. A plain `restart` does nothing after a code change; the image must be rebuilt.

---

## Deploying web UI changes (app.py, templates)

Files are owned by `svc:multimedia_rw` on the host, so direct `scp` as `fastchar` will be denied. Copy to `/tmp` first, then move into place with `sudo`.

```bash
# 1. Copy changed file(s) to /tmp
scp webui/app.py fastchar@10.10.0.10:/tmp/app.py

# 2. Move into place
ssh fastchar@10.10.0.10 "sudo mv /tmp/app.py /opt/docker_data/mtdp/webui/app.py"

# 3. Rebuild the image and bring the container back up
ssh fastchar@10.10.0.10 "cd /opt/docker_data && sudo docker compose build mtdp-webui && sudo docker compose up -d mtdp-webui"
```

For templates, repeat step 1–2 for each changed file under `webui/templates/`, then rebuild once.

---

## Deploying backend script changes (Movies.py, TV.py)

These **are** volume-mounted, so no rebuild is needed — just copy and restart:

```bash
scp Modules/Movies.py fastchar@10.10.0.10:/tmp/Movies.py
ssh fastchar@10.10.0.10 "sudo mv /tmp/Movies.py /opt/docker_data/mtdp/Movies.py"

ssh fastchar@10.10.0.10 "cd /opt/docker_data && sudo docker compose restart mtdp"
```

---

## Verifying a deploy

After rebuilding, confirm the change is live inside the container before reporting success:

```bash
ssh fastchar@10.10.0.10 "sudo docker exec mtdp-webui grep -n 'your search string' /webui/app.py"
```

---

## Configuration

Config lives at `/opt/docker_data/mtdp/config/config.yml` (volume-mounted — changes take effect on next run, no restart needed).

Key settings:
- `USE_LABELS: true` — skip movies already labelled `MTDfP` in Plex
- `REFRESH_METADATA: true` — trigger a Plex metadata refresh after each trailer download so Plex picks up the new file immediately
- `CHECK_PLEX_PASS_TRAILERS: true` — skip movies that already have a Plex Pass trailer

---

## MTDfP Label behaviour

The `MTDfP` label is applied to Plex items after a trailer is downloaded, so MTDP skips them on future runs.

- **Labels survive metadata refresh** — Plex preserves user-set labels and collections through metadata refreshes (they are not sourced from external agents). `REFRESH_METADATA: true` is safe to use.
- **Web UI manual downloads** apply the label automatically via the Plex API after the download completes.
- **MTDP script downloads** apply the label via `plexapi` after each successful download.
- The fixed Plex client identifier `mtdp-trailer-downloader` is used for all Plex API calls (both script and web UI) so Plex does not register a new device on each run.

---

## Notes

- `fastchar` has passwordless sudo on dockhost
- The web UI runs on port `7879` → `http://10.10.0.10:7879`
- The docker-compose stack includes: radarr, sonarr, bazarr, prowlarr, qbittorrent, jellyfin, lidarr, readarr, whisparr, flaresolverr, ntfy, mtdp, mtdp-webui
- Plex runs separately on `10.10.0.200` (the NAS) — not on dockhost
