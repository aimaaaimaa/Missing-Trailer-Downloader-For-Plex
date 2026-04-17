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

## Notes

- `fastchar` has passwordless sudo on dockhost
- The web UI runs on port `7879` → `http://10.10.0.10:7879`
- The docker-compose stack includes: radarr, sonarr, bazarr, prowlarr, qbittorrent, jellyfin, lidarr, readarr, whisparr, flaresolverr, ntfy, mtdp, mtdp-webui
