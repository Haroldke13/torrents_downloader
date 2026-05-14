# Render Deploy

This app is packaged for a Docker-based Render web service.

## Files

- `Dockerfile`: builds the runtime image with Python 3.13 and installs the bounded Python dependencies, including `libtorrent`
- `start.sh`: starts Gunicorn on Render's `PORT`
- `render.yaml`: Blueprint for a single web service with a persistent disk

## Why the config looks like this

- The app keeps queue state in process memory, so Gunicorn is fixed to `1` worker.
- Downloads must survive restarts and redeploys, so Render should mount a persistent disk at `/app/downloads`.
- `DOWNLOADS_DIR=/app/downloads` is required so the app writes to the mounted disk instead of the container home directory.

## Render setup

1. Push this folder to a Git repo.
2. In Render, create a new Blueprint or Web Service from that repo.
3. If using the Blueprint, Render will pick up `render.yaml`.
4. Keep the service at a single instance.
5. Confirm the disk is mounted at `/app/downloads`.
6. Deploy and verify `GET /health` returns `200`.

## Runtime notes

- Render only exposes one public HTTP port to the service; this app uses that for Flask/Gunicorn.
- Torrent payloads and finished downloads are stored on the persistent disk.
- BitTorrent behavior on Render itself is not fully verified here, especially any peer-discovery behavior that depends on network conditions outside plain HTTP serving.
