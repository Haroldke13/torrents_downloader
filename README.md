# torrents_downloader

A Flask + libtorrent web UI that queues magnet links, `.torrent` files and direct
HTTP downloads to a server-side disk, shows live progress, and lets you browse
and delete what has been fetched.

> **Legal notice.** `links.txt` in this repository is a ~99 KB list of magnet URIs
> for commercial films and television series. Those links are not the author's to
> distribute, and downloading the content they point to is copyright infringement
> in most jurisdictions. The proprietary `LICENSE` in this repository covers the
> **software only** — it grants nothing in respect of that list or the media it
> references. Removing `links.txt` is strongly recommended, particularly while
> this repository is public. See [Status](#status).

## What it does

`app.py` (~1,180 lines) builds the whole application through `create_app()`.

### Routes

| Route | Method | Purpose |
| --- | --- | --- |
| `/` | GET | Submission form and live queue |
| `/health` | GET | Health check, used by the Render blueprint |
| `/submit` | POST | Form submission; queues sources and redirects |
| `/api/submit` | POST | Same, returning JSON |
| `/api/status` | GET | Current queue and per-job progress as JSON (polled by the page) |
| `/downloads` | GET | Browse everything on the download disk |
| `/downloads/file/<path>` | GET | Serve one finished file |
| `/downloads/delete/<path>` | POST | Delete one file, pruning empty parent directories |
| `/downloads/delete-all` | POST | Clear the download directory |
| `/downloads/<job_id>` | GET | Artifacts produced by a single job |
| `/downloads/<job_id>/<path>` | GET | Serve one artifact from a job |

### Accepted sources

A submission can mix any of these, and duplicates are canonicalised away
(`canonicalize_source`, `dedupe_sources`):

- **Magnet URIs** — matched with a regex and handed to libtorrent.
- **`.torrent` files** — uploaded through the form.
- **Direct HTTP/HTTPS URLs** — streamed to disk in 1 MiB chunks, with the
  filename derived from the URL or the `Content-Disposition` header.
- **Source lists** — `.txt`, `.csv`, `.tsv`, `.json` and `.html`/`.htm` files,
  either uploaded or read from a local path. CSV dialect is sniffed; JSON is
  walked recursively for strings; HTML is parsed for `<a>`/`<area>` hrefs by a
  small `HTMLParser` subclass.

### Download engine

One libtorrent session is created per process and bootstrapped against four DHT
routers (`router.bittorrent.com`, `router.utorrent.com`, `dht.transmissionbt.com`,
`dht.libtorrent.org`). Jobs move through `queued → starting → metadata →
downloading → completed|failed`, and a poller thread updates progress, byte
counts, transfer rates and peer counts on a fixed interval.

**Queue state lives in process memory**, which is why `start.sh` pins Gunicorn to
a single worker with threads rather than multiple worker processes.

### Where files land

`resolve_download_path()` picks the destination in this order:

1. `DOWNLOADS_DIR` or `MOVIES_DOWNLOADS_DIR` if either is set.
2. An Android/Termux layout, when `TERMUX_VERSION`, `ANDROID_ROOT` or `PREFIX`
   indicate one.
3. The user's home `Downloads` directory otherwise.

Filenames are sanitised (`sanitize_filename`) and collisions get a numeric
suffix (`unique_output_path`).

## Tech stack

`requirements.txt`, all bounded:

```
beautifulsoup4>=4.12.3,<4.13
Flask>=3.0.3,<3.1
gunicorn>=25.3.0,<25.4
libtorrent>=2.0.11,<2.1
requests>=2.32.3,<2.33
```

`libtorrent` is a compiled extension — on Debian/Ubuntu install
`python3-libtorrent` (or build it) if the wheel does not resolve for your
platform.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Or with Gunicorn, as the container does:

```bash
./start.sh          # honours PORT, GUNICORN_THREADS, GUNICORN_TIMEOUT
```

Docker:

```bash
docker build -t torrents-downloader .
docker run -p 10000:10000 -v "$PWD/downloads:/app/downloads" torrents-downloader
```

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `DOWNLOADS_DIR` / `MOVIES_DOWNLOADS_DIR` | platform-dependent | Destination directory |
| `MAX_CONCURRENT_DOWNLOADS` | `1` in Docker | Jobs run at once |
| `DOWNLOAD_POLL_INTERVAL` | `1.0` | Seconds between progress updates |
| `FLASK_HOST`, `FLASK_PORT`, `PORT` | — | Bind address and port |
| `GUNICORN_THREADS`, `GUNICORN_TIMEOUT` | `4`, `120` | Used by `start.sh` |

## Deployment

`render.yaml` describes a single Docker web service with a persistent disk
mounted at `/app/downloads` and `/health` as the health check path, and
`RENDER_DEPLOY.md` explains the reasoning. Two caveats:

- The blueprint requests `sizeGB: 50000` — 50 TB. That is far beyond what the
  named plan offers and will not provision as written; set a realistic size.
- BitTorrent peer discovery from inside a PaaS container is not verified and may
  not work, as `RENDER_DEPLOY.md` itself notes. The direct-HTTP path is
  unaffected.

## Repository contents worth pruning

- `links.txt` — the magnet-link list described in the notice above.
- `app copy.py` (25 KB) and `app2copy copy.py` (12 KB) — stale duplicates of
  earlier versions of `app.py`. Only `app.py` is used.
- `WhatsApp Image 2026-05-14 at 16.00.14.jpeg` (140 KB) — unrelated to the app.
- `.codex` — a zero-byte file.
- `scripts/supernova_browser_fetch.py` and `scripts/export_supernova_cookies.py`
  — a Selenium scraper for `supernova.to` and a helper that decrypts cookies out
  of a local Chrome profile. Neither is imported by the application, both pull in
  `selenium` and `beautifulsoup4` dependencies that are not declared for them,
  and the cookie exporter reads the browser's saved credentials store.

## Status

**Working prototype.** The queue, the libtorrent path, the direct-HTTP path, the
source-list parsers and the file browser are all implemented and readable in the
source. Not verified here: an end-to-end download against a live swarm, and the
Render deployment. There are no tests.

## Licence

**Proprietary software — all rights reserved.** Copyright © 2026 Joel Harold Onyango.

This repository is not open source. The full terms are in [LICENSE](LICENSE); in
summary, you may not copy, redistribute, modify, sublicense, publish, re-host or
commercially exploit this software, in whole or in part, without the prior
written permission of the copyright holder. Access to this repository does not
grant any licence beyond reading it.

That licence applies to the code in this repository. It does not apply to, and
grants no rights in, third-party media referenced by `links.txt`.
