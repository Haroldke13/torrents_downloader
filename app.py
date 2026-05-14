import atexit
import csv
import html
import json
import os
import re
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, send_from_directory, url_for
import libtorrent as lt


HTTP_SCHEMES = {"http", "https"}
DIRECT_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
MAGNET_URI_PATTERN = re.compile(r"magnet:\?[^\s<>'\"]+", flags=re.IGNORECASE)
HTTP_URL_PATTERN = re.compile(r"https?://[^\s<>'\"]+", flags=re.IGNORECASE)
LOCAL_SOURCE_LIST_FILE_EXTENSIONS = {".txt", ".csv", ".tsv", ".json", ".html", ".htm"}
UPLOADABLE_SOURCE_FILE_EXTENSIONS = LOCAL_SOURCE_LIST_FILE_EXTENSIONS | {".torrent"}
ACTIVE_STATES = {"starting", "metadata", "downloading"}
FINAL_STATES = {"completed", "failed"}
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_DHT_ROUTERS = (
    ("router.bittorrent.com", 6881),
    ("router.utorrent.com", 6881),
    ("dht.transmissionbt.com", 6881),
    ("dht.libtorrent.org", 25401),
)


class LocalHtmlLinkParser(HTMLParser):
    """Collect likely download links from a saved local HTML page."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() not in {"a", "area"}:
            return

        for name, value in attrs:
            if name.lower() == "href" and value:
                self.hrefs.append(value)


@dataclass
class PendingSource:
    source: str
    label: str | None = None
    cleanup_path: str | None = None


@dataclass
class DownloadJob:
    id: str
    source: str
    label: str
    kind: str
    source_cleanup_path: str | None = None
    handle: lt.torrent_handle | None = None
    temp_path: str | None = None
    output_path: str | None = None
    state: str = "queued"
    message: str = "Queued"
    error: str | None = None
    progress: float = 0.0
    bytes_done: int = 0
    bytes_total: int | None = None
    download_rate: float = 0.0
    upload_rate: float = 0.0
    peers: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None


def sanitize_filename(filename: str) -> str:
    """Remove path separators and reserved characters from a filename."""

    cleaned = re.sub(r'[<>:"/\\\\|?*\x00-\x1f]+', "_", filename).strip().strip(".")
    return cleaned or "download"


def format_byte_count(byte_count: int | None) -> str:
    """Render bytes in a compact human-readable form."""

    if byte_count is None:
        return "unknown"

    value = float(byte_count)
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    return f"{value:.1f} {unit}"


def resolve_download_path() -> str:
    """Choose the actual device Downloads directory when one is available."""

    override = os.environ.get("DOWNLOADS_DIR") or os.environ.get("MOVIES_DOWNLOADS_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))

    home = Path.home()
    prefix = os.environ.get("PREFIX", "")
    is_android_like = bool(
        os.environ.get("TERMUX_VERSION")
        or os.environ.get("ANDROID_ROOT")
        or "com.termux" in prefix
    )

    existing_candidates: list[Path] = []
    creatable_candidates: list[Path] = []

    if is_android_like:
        existing_candidates.extend(
            [
                home / "storage" / "downloads",
                Path("/storage/emulated/0/Download"),
                Path("/sdcard/Download"),
            ]
        )

    if os.name == "nt":
        userprofile = os.environ.get("USERPROFILE")
        if userprofile:
            creatable_candidates.append(Path(userprofile) / "Downloads")
    else:
        creatable_candidates.append(home / "Downloads")

    creatable_candidates.append(Path.cwd() / "downloads")

    for candidate in existing_candidates + creatable_candidates:
        if candidate.exists() and os.access(candidate, os.W_OK):
            return str(candidate.resolve())

    for candidate in creatable_candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if os.access(candidate, os.W_OK):
            return str(candidate.resolve())

    return str((Path.cwd() / "downloads").resolve())


def clean_extracted_source(source: str) -> str:
    """Trim markup and trailing punctuation from an extracted source candidate."""

    source = html.unescape(source).strip().strip("<>\"'")
    return source.rstrip("),.;]}")


def normalize_display_text(value: str) -> str:
    """Decode display text and collapse accidental spacing noise."""

    normalized = html.unescape(value or "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", normalized).strip()


def extract_sources_from_text_blob(raw_text: str, include_http_urls: bool = True) -> list[str]:
    """Extract magnet links and optionally HTTP(S) URLs from arbitrary text."""

    decoded = html.unescape(raw_text)
    sources = [clean_extracted_source(item) for item in MAGNET_URI_PATTERN.findall(decoded)]

    if include_http_urls:
        sources.extend(clean_extracted_source(item) for item in HTTP_URL_PATTERN.findall(decoded))

    return [source for source in sources if source]


def parse_torrent_links(raw_value: str) -> list[str]:
    """Split pasted sources into a clean list without breaking embedded magnet URIs."""

    magnet_links = MAGNET_URI_PATTERN.findall(raw_value)
    remainder = MAGNET_URI_PATTERN.sub("\n", raw_value)
    other_sources = [item.strip() for item in re.split(r"[\r\n,]+", remainder) if item.strip()]
    return magnet_links + other_sources


def resolve_pasted_sources(raw_value: str) -> list[str]:
    """Resolve text-area input into queued sources without reading server file paths."""

    raw_candidates = parse_torrent_links(raw_value)
    raw_candidates.extend(extract_sources_from_text_blob(raw_value))
    return dedupe_sources(raw_candidates)


def read_local_text_file(file_path: str) -> str:
    """Read a local text-like file tolerantly for source extraction."""

    with open(file_path, "r", encoding="utf-8", errors="ignore") as handle:
        return handle.read()


def choose_csv_dialect(sample: str, file_path: str):
    """Pick a reasonable CSV dialect for a local delimited file."""

    if file_path.lower().endswith(".tsv"):
        return csv.excel_tab

    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def load_torrent_links_from_text_file(file_path: str) -> list[str]:
    """Load torrent sources from a local plain-text file."""

    filtered_lines = []
    for line in read_local_text_file(file_path).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        filtered_lines.append(line)

    return dedupe_sources(parse_torrent_links("\n".join(filtered_lines)))


def load_torrent_links_from_csv_file(file_path: str) -> list[str]:
    """Load torrent sources from a local CSV or TSV file."""

    sample = read_local_text_file(file_path)
    dialect = choose_csv_dialect(sample[:4096], file_path)
    sources: list[str] = []

    with open(file_path, "r", encoding="utf-8", errors="ignore", newline="") as handle:
        reader = csv.reader(handle, dialect)
        for row in reader:
            for cell in row:
                stripped = cell.strip()
                if not stripped or stripped.startswith("#"):
                    continue

                embedded_sources = extract_sources_from_text_blob(stripped)
                if embedded_sources:
                    sources.extend(embedded_sources)
                    continue

                if stripped.startswith("magnet:?") or stripped.startswith("http://") or stripped.startswith("https://"):
                    sources.extend(parse_torrent_links(stripped))

    return dedupe_sources(sources)


def iter_json_strings(value) -> Iterable[str]:
    """Yield every string value stored anywhere inside decoded JSON data."""

    if isinstance(value, str):
        yield value
        return

    if isinstance(value, dict):
        for nested in value.values():
            yield from iter_json_strings(nested)
        return

    if isinstance(value, list):
        for nested in value:
            yield from iter_json_strings(nested)


def load_torrent_links_from_json_file(file_path: str) -> list[str]:
    """Load torrent sources from a local JSON file."""

    raw_text = read_local_text_file(file_path)

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return dedupe_sources(extract_sources_from_text_blob(raw_text))

    sources: list[str] = []
    for text_value in iter_json_strings(payload):
        embedded_sources = extract_sources_from_text_blob(text_value)
        if embedded_sources:
            sources.extend(embedded_sources)
            continue

        stripped = text_value.strip()
        if stripped.startswith("magnet:?") or stripped.startswith("http://") or stripped.startswith("https://"):
            sources.extend(parse_torrent_links(stripped))

    return dedupe_sources(sources)


def load_torrent_links_from_html_file(file_path: str) -> list[str]:
    """Load torrent sources from a saved local HTML page without scraping the remote site."""

    raw_text = read_local_text_file(file_path)
    parser = LocalHtmlLinkParser()
    parser.feed(raw_text)

    sources: list[str] = []
    for href in parser.hrefs:
        sources.extend(extract_sources_from_text_blob(href))

    sources.extend(clean_extracted_source(item) for item in MAGNET_URI_PATTERN.findall(html.unescape(raw_text)))
    return dedupe_sources([source for source in sources if source])


def load_torrent_links_from_source_file(file_path: str) -> list[str]:
    """Expand a local list/export file into queued download sources."""

    suffix = Path(file_path).suffix.lower()
    if suffix == ".txt":
        return load_torrent_links_from_text_file(file_path)
    if suffix in {".csv", ".tsv"}:
        return load_torrent_links_from_csv_file(file_path)
    if suffix == ".json":
        return load_torrent_links_from_json_file(file_path)
    if suffix in {".html", ".htm"}:
        return load_torrent_links_from_html_file(file_path)
    return []


def canonicalize_source(source: str) -> str:
    """Build a stable dedupe key for a queued download source."""

    source = source.strip()
    if not source:
        return ""

    if source.startswith("magnet:?"):
        parsed = urllib.parse.urlparse(source)
        params = urllib.parse.parse_qs(parsed.query)
        xt_values = [value.strip().lower() for value in params.get("xt", []) if value.strip()]
        btih_values = [value.rsplit(":", 1)[-1] for value in xt_values if value.startswith("urn:btih:")]
        if btih_values:
            return f"magnet:btih:{btih_values[0]}"
        return source

    if os.path.isfile(source):
        return os.path.abspath(source)

    parsed = urllib.parse.urlparse(source)
    if parsed.scheme in HTTP_SCHEMES:
        normalized = parsed._replace(fragment="")
        return urllib.parse.urlunparse(normalized)

    return source


def dedupe_sources(sources: list[str]) -> list[str]:
    """Keep the first instance of each source while removing repeats."""

    unique_sources: list[str] = []
    seen_keys: set[str] = set()

    for source in sources:
        key = canonicalize_source(source)
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        unique_sources.append(source.strip())

    return unique_sources


def dedupe_pending_sources(entries: list[PendingSource]) -> list[PendingSource]:
    """Keep the first instance of each pending source entry while removing repeats."""

    unique_entries: list[PendingSource] = []
    seen_keys: set[str] = set()

    for entry in entries:
        key = canonicalize_source(entry.source)
        if not key or key in seen_keys:
            if entry.cleanup_path and os.path.exists(entry.cleanup_path):
                os.remove(entry.cleanup_path)
            continue
        seen_keys.add(key)
        unique_entries.append(entry)

    return unique_entries


def derive_torrent_label(torrent_link: str) -> str:
    """Choose a readable label for a queued source before metadata is available."""

    if torrent_link.startswith("magnet:?"):
        parsed = urllib.parse.urlparse(torrent_link)
        display_name = urllib.parse.parse_qs(parsed.query).get("dn", [])
        if display_name and display_name[0].strip():
            return normalize_display_text(display_name[0])
        info_hash = urllib.parse.parse_qs(parsed.query).get("xt", [torrent_link])[-1]
        return info_hash.rsplit(":", 1)[-1]

    parsed = urllib.parse.urlparse(torrent_link)
    if parsed.scheme in HTTP_SCHEMES:
        filename = os.path.basename(parsed.path.rstrip("/"))
        return urllib.parse.unquote(filename) or torrent_link

    return os.path.basename(torrent_link.rstrip(os.sep)) or torrent_link


def looks_like_torrent_source(source: str) -> bool:
    """Return True when a source should be handled by libtorrent."""

    if source.startswith("magnet:?"):
        return True

    if os.path.isfile(source):
        return source.lower().endswith(".torrent")

    parsed = urllib.parse.urlparse(source)
    return parsed.scheme in HTTP_SCHEMES and parsed.path.lower().endswith(".torrent")


def unique_output_path(download_path: str, filename: str) -> str:
    """Avoid overwriting an existing file in the target download directory."""

    base_name = sanitize_filename(filename)
    stem, suffix = os.path.splitext(base_name)
    candidate = Path(download_path) / base_name
    counter = 1

    while candidate.exists():
        candidate = Path(download_path) / f"{stem} ({counter}){suffix}"
        counter += 1

    return str(candidate)


def derive_direct_download_name(url: str, headers) -> str:
    """Pick a filename from Content-Disposition first, then from the URL path."""

    disposition = headers.get("Content-Disposition", "")
    utf8_match = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", disposition, flags=re.IGNORECASE)
    if utf8_match:
        return sanitize_filename(urllib.parse.unquote(utf8_match.group(1)))

    filename_match = re.search(r'filename\s*=\s*"?(?P<name>[^";]+)"?', disposition, flags=re.IGNORECASE)
    if filename_match:
        return sanitize_filename(filename_match.group("name"))

    parsed = urllib.parse.urlparse(url)
    fallback_name = urllib.parse.unquote(os.path.basename(parsed.path.rstrip("/")))
    return sanitize_filename(fallback_name or "download")


def get_error_message(status) -> str | None:
    """Return a torrent error message only when libtorrent reports a non-zero code."""

    errc = getattr(status, "errc", None)
    if errc is None:
        return None

    try:
        if errc.value() == 0:
            return None
    except Exception:
        return None

    return errc.message()


def build_torrent_handle(session: lt.session, torrent_link: str, download_path: str):
    """Create a torrent handle from a magnet URI, local .torrent file, or .torrent URL."""

    parsed = urllib.parse.urlparse(torrent_link)
    storage_mode = lt.storage_mode_t.storage_mode_sparse

    if torrent_link.startswith("magnet:?"):
        params = lt.parse_magnet_uri(torrent_link)
        params.save_path = download_path
        params.storage_mode = storage_mode
        return session.add_torrent(params), None

    temp_path = None
    source_path = torrent_link

    if parsed.scheme in HTTP_SCHEMES:
        request_headers = {"User-Agent": "Mozilla/5.0"}
        with urllib.request.urlopen(urllib.request.Request(torrent_link, headers=request_headers)) as response:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".torrent") as temp_file:
                temp_file.write(response.read())
                temp_path = temp_file.name
                source_path = temp_path

    info = lt.torrent_info(source_path)
    params = lt.add_torrent_params()
    params.ti = info
    params.save_path = download_path
    params.storage_mode = storage_mode
    return session.add_torrent(params), temp_path


def bootstrap_torrent_session(session: lt.session) -> None:
    """Enable common discovery services so magnet jobs can find metadata sooner."""

    try:
        session.start_dht()
    except RuntimeError:
        pass

    for host, port in DEFAULT_DHT_ROUTERS:
        try:
            session.add_dht_router(host, port)
        except RuntimeError:
            continue

    for starter_name in ("start_lsd", "start_upnp", "start_natpmp"):
        starter = getattr(session, starter_name, None)
        if starter is None:
            continue
        try:
            starter()
        except RuntimeError:
            continue


class DownloadManager:
    """Coordinate queued torrent and direct-file downloads in the background."""

    def __init__(self, download_path: str, max_concurrent: int = 1, poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS):
        self.download_path = os.path.abspath(download_path)
        self.max_concurrent = max(1, max_concurrent)
        self.poll_interval = max(0.25, poll_interval)
        self.session = lt.session()
        bootstrap_torrent_session(self.session)
        self.jobs: list[DownloadJob] = []
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.worker_thread = threading.Thread(target=self._run, name="torrent-download-manager", daemon=True)
        self.started = False
        os.makedirs(self.download_path, exist_ok=True)

    def start(self) -> None:
        with self.lock:
            if self.started:
                return
            self.started = True
            self.worker_thread.start()

    def shutdown(self) -> None:
        self.stop_event.set()
        if self.worker_thread.is_alive():
            self.worker_thread.join(timeout=5)

        with self.lock:
            for job in self.jobs:
                self._release_torrent_resources_locked(job)
                self._cleanup_source_artifacts_locked(job)

            try:
                self.session.pause()
            except RuntimeError:
                pass

    def submit(self, entries: list[PendingSource]) -> list[str]:
        submitted_ids: list[str] = []

        with self.lock:
            for entry in entries:
                job = DownloadJob(
                    id=uuid.uuid4().hex,
                    source=entry.source,
                    label=entry.label or derive_torrent_label(entry.source),
                    kind="torrent" if looks_like_torrent_source(entry.source) else "direct",
                    source_cleanup_path=entry.cleanup_path,
                )
                self.jobs.append(job)
                submitted_ids.append(job.id)

        return submitted_ids

    def snapshot(self) -> dict:
        with self.lock:
            jobs = [self._serialize_job_locked(job) for job in self.jobs]

        summary = {
            "queued": sum(1 for job in jobs if job["state"] == "queued"),
            "active": sum(1 for job in jobs if job["state"] in ACTIVE_STATES),
            "completed": sum(1 for job in jobs if job["state"] == "completed"),
            "failed": sum(1 for job in jobs if job["state"] == "failed"),
            "total": len(jobs),
        }
        return {
            "download_path": self.download_path,
            "max_concurrent": self.max_concurrent,
            "poll_interval_ms": int(self.poll_interval * 1000),
            "summary": summary,
            "jobs": jobs,
        }

    def get_job(self, job_id: str) -> DownloadJob | None:
        with self.lock:
            return next((job for job in self.jobs if job.id == job_id), None)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            with self.lock:
                self._start_queued_jobs_locked()
                self._refresh_torrent_jobs_locked()
            self.stop_event.wait(self.poll_interval)

    def _active_job_count_locked(self) -> int:
        return sum(1 for job in self.jobs if job.state in ACTIVE_STATES)

    def _start_queued_jobs_locked(self) -> None:
        while self._active_job_count_locked() < self.max_concurrent:
            job = next((candidate for candidate in self.jobs if candidate.state == "queued"), None)
            if job is None:
                break
            self._start_job_locked(job)

    def _start_job_locked(self, job: DownloadJob) -> None:
        job.state = "starting"
        job.message = "Starting download"
        job.started_at = job.started_at or time.time()

        if job.kind == "direct":
            worker = threading.Thread(
                target=self._run_direct_download,
                args=(job.id,),
                name=f"direct-download-{job.id}",
                daemon=True,
            )
            worker.start()
            return

        try:
            handle, temp_path = build_torrent_handle(self.session, job.source, self.download_path)
            job.handle = handle
            job.temp_path = temp_path
            job.state = "metadata" if job.source.startswith("magnet:?") else "downloading"
            job.message = (
                "Waiting for torrent metadata from peers or trackers"
                if job.state == "metadata"
                else "Downloading"
            )
        except Exception as exc:
            job.state = "failed"
            job.error = str(exc)
            job.message = "Failed to start"
            job.finished_at = time.time()
        finally:
            self._cleanup_uploaded_source_locked(job)

    def _run_direct_download(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if job is None:
            return

        request_headers = {"User-Agent": "Mozilla/5.0"}

        try:
            with urllib.request.urlopen(urllib.request.Request(job.source, headers=request_headers)) as response:
                filename = derive_direct_download_name(job.source, response.headers)
                output_path = unique_output_path(self.download_path, filename)
                total_bytes_header = response.headers.get("Content-Length", "").strip()
                total_bytes = int(total_bytes_header) if total_bytes_header.isdigit() else None

                with self.lock:
                    job.output_path = output_path
                    job.bytes_total = total_bytes
                    job.state = "downloading"
                    job.message = "Downloading file"

                downloaded_bytes = 0
                started_at = time.time()

                with open(output_path, "wb") as handle:
                    while True:
                        chunk = response.read(DIRECT_DOWNLOAD_CHUNK_SIZE)
                        if not chunk:
                            break

                        handle.write(chunk)
                        downloaded_bytes += len(chunk)
                        elapsed = max(time.time() - started_at, 0.001)
                        speed = downloaded_bytes / elapsed

                        with self.lock:
                            job.bytes_done = downloaded_bytes
                            job.bytes_total = total_bytes
                            job.download_rate = speed
                            job.progress = downloaded_bytes / total_bytes if total_bytes else 0.0
                            job.message = "Downloading file"

                with self.lock:
                    job.bytes_done = downloaded_bytes
                    job.download_rate = 0.0
                    job.progress = 1.0
                    job.state = "completed"
                    job.message = "Completed"
                    job.finished_at = time.time()
        except Exception as exc:
            with self.lock:
                job.state = "failed"
                job.error = str(exc)
                job.message = "Download failed"
                job.finished_at = time.time()
        finally:
            with self.lock:
                self._cleanup_source_artifacts_locked(job)

    def _refresh_torrent_jobs_locked(self) -> None:
        for job in self.jobs:
            if job.kind != "torrent" or job.handle is None or job.state in FINAL_STATES:
                continue

            try:
                status = job.handle.status()
            except RuntimeError as exc:
                self._fail_job_locked(job, str(exc))
                continue

            error_message = get_error_message(status)
            if error_message:
                self._fail_job_locked(job, f"Torrent error: {error_message}")
                continue

            name = getattr(status, "name", "").strip()
            if name:
                job.label = name

            job.peers = getattr(status, "num_peers", 0) or 0
            job.download_rate = float(getattr(status, "download_rate", 0) or 0)
            job.upload_rate = float(getattr(status, "upload_rate", 0) or 0)
            job.bytes_done = int(getattr(status, "total_done", 0) or 0)
            total_wanted = int(getattr(status, "total_wanted", 0) or 0)
            job.bytes_total = total_wanted or None
            job.progress = float(getattr(status, "progress", 0.0) or 0.0)

            if not getattr(status, "has_metadata", False):
                job.state = "metadata"
                if job.peers:
                    job.message = f"Waiting for torrent metadata from {job.peers} peer(s)"
                else:
                    job.message = "Waiting for torrent metadata from peers or trackers"
                continue

            job.state = "downloading"
            job.message = "Downloading torrent"

            if getattr(status, "is_seeding", False) or (job.bytes_total and job.bytes_done >= job.bytes_total and job.progress >= 0.999):
                job.state = "completed"
                job.message = "Completed"
                job.progress = 1.0
                job.finished_at = time.time()
                job.output_path = str(Path(self.download_path) / (name or job.label))
                self._release_torrent_resources_locked(job)
                self._cleanup_source_artifacts_locked(job)

    def _fail_job_locked(self, job: DownloadJob, message: str) -> None:
        job.state = "failed"
        job.error = message
        job.message = "Download failed"
        job.finished_at = time.time()
        self._release_torrent_resources_locked(job)
        self._cleanup_source_artifacts_locked(job)

    def _cleanup_uploaded_source_locked(self, job: DownloadJob) -> None:
        if job.source_cleanup_path and os.path.exists(job.source_cleanup_path):
            os.remove(job.source_cleanup_path)
        job.source_cleanup_path = None

    def _cleanup_source_artifacts_locked(self, job: DownloadJob) -> None:
        if job.temp_path and os.path.exists(job.temp_path):
            os.remove(job.temp_path)
        job.temp_path = None
        self._cleanup_uploaded_source_locked(job)

    def _release_torrent_resources_locked(self, job: DownloadJob) -> None:
        if job.handle is not None:
            try:
                self.session.remove_torrent(job.handle)
            except RuntimeError:
                pass
            job.handle = None

        if job.temp_path and os.path.exists(job.temp_path):
            os.remove(job.temp_path)
        job.temp_path = None

    def _serialize_job_locked(self, job: DownloadJob) -> dict:
        return {
            "id": job.id,
            "label": job.label,
            "source": job.source,
            "kind": job.kind,
            "state": job.state,
            "message": job.message,
            "error": job.error,
            "progress": round(job.progress * 100, 2),
            "bytes_done": job.bytes_done,
            "bytes_done_text": format_byte_count(job.bytes_done),
            "bytes_total": job.bytes_total,
            "bytes_total_text": format_byte_count(job.bytes_total),
            "download_rate": int(job.download_rate),
            "download_rate_text": format_byte_count(int(job.download_rate)) + "/s" if job.download_rate else "0 B/s",
            "upload_rate": int(job.upload_rate),
            "upload_rate_text": format_byte_count(int(job.upload_rate)) + "/s" if job.upload_rate else "0 B/s",
            "peers": job.peers,
            "output_path": job.output_path,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
        }


def save_uploaded_file(uploaded_file, suffix: str) -> str:
    """Persist an uploaded file to a temporary location."""

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
        uploaded_file.stream.seek(0)
        temp_file.write(uploaded_file.stream.read())
        return temp_file.name


def load_uploaded_entries(uploaded_files) -> tuple[list[PendingSource], list[str]]:
    """Expand uploaded list files and uploaded .torrent files into pending sources."""

    entries: list[PendingSource] = []
    warnings: list[str] = []

    for uploaded_file in uploaded_files:
        if uploaded_file is None or not uploaded_file.filename:
            continue

        filename = sanitize_filename(Path(uploaded_file.filename).name)
        suffix = Path(filename).suffix.lower()

        if suffix not in UPLOADABLE_SOURCE_FILE_EXTENSIONS:
            warnings.append(f"Skipped unsupported upload: {filename}")
            continue

        temp_path = save_uploaded_file(uploaded_file, suffix)

        try:
            if suffix == ".torrent":
                entries.append(
                    PendingSource(
                        source=temp_path,
                        label=Path(filename).stem,
                        cleanup_path=temp_path,
                    )
                )
                continue

            for source in load_torrent_links_from_source_file(temp_path):
                entries.append(PendingSource(source=source))
        finally:
            if suffix != ".torrent" and os.path.exists(temp_path):
                os.remove(temp_path)

    return dedupe_pending_sources(entries), warnings


def build_artifact_list(job_snapshot: dict) -> list[dict]:
    """Expose downloadable files for completed jobs."""

    output_path = job_snapshot.get("output_path")
    if not output_path:
        return []

    path = Path(output_path)
    if not path.exists():
        return []

    if path.is_file():
        return [
            {
                "name": path.name,
                "size_text": format_byte_count(path.stat().st_size),
                "download_url": f"/downloads/{job_snapshot['id']}",
            }
        ]

    files: list[dict] = []
    for child in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        relative_path = child.relative_to(path).as_posix()
        files.append(
            {
                "name": relative_path,
                "size_text": format_byte_count(child.stat().st_size),
                "download_url": f"/downloads/{job_snapshot['id']}/{relative_path}",
            }
        )
        if len(files) >= 200:
            break

    return files


def build_status_payload() -> dict:
    """Return the current app status payload for the API and UI."""

    payload = DOWNLOAD_MANAGER.snapshot()
    for job in payload["jobs"]:
        job["artifacts"] = build_artifact_list(job) if job["state"] == "completed" else []
    return payload


def queue_request_sources(form_data, uploaded_files) -> tuple[dict | None, int]:
    """Queue posted sources and return the response payload plus status code."""

    entries = [PendingSource(source=source) for source in resolve_pasted_sources(form_data.get("sources", ""))]
    uploaded_entries, warnings = load_uploaded_entries(uploaded_files.getlist("source_files"))
    entries.extend(uploaded_entries)
    entries = dedupe_pending_sources(entries)

    if not entries:
        return {"error": "No valid magnet links, torrent sources, or upload entries were provided."}, 400

    job_ids = DOWNLOAD_MANAGER.submit(entries)
    payload = build_status_payload()
    payload["message"] = f"Queued {len(job_ids)} download source(s)."
    payload["warnings"] = warnings
    payload["submitted_job_ids"] = job_ids
    return payload, 200


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024 * 1024

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            download_path=DOWNLOAD_MANAGER.download_path,
            max_concurrent=DOWNLOAD_MANAGER.max_concurrent,
            poll_interval_ms=int(DOWNLOAD_MANAGER.poll_interval * 1000),
            api_submit_url=url_for("submit"),
            fallback_submit_url=url_for("submit_fallback"),
            server_message=request.args.get("message", ""),
            server_message_kind=request.args.get("message_kind", ""),
        )

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "download_path": DOWNLOAD_MANAGER.download_path})

    @app.post("/api/submit")
    def submit():
        payload, status_code = queue_request_sources(request.form, request.files)
        return jsonify(payload), status_code

    @app.post("/submit")
    def submit_fallback():
        payload, status_code = queue_request_sources(request.form, request.files)
        message = payload.get("message") or payload.get("error") or "Queue request finished."
        warnings = payload.get("warnings") or []
        if warnings:
            message = f"{message} {' '.join(warnings)}"

        return redirect(
            url_for(
                "index",
                message=message,
                message_kind="success" if status_code < 400 else "error",
            )
        )

    @app.get("/api/status")
    def status():
        return jsonify(build_status_payload())

    @app.get("/downloads/<job_id>")
    def download_single_artifact(job_id: str):
        job = DOWNLOAD_MANAGER.get_job(job_id)
        if job is None or not job.output_path:
            abort(404)

        path = Path(job.output_path)
        if not path.exists():
            abort(404)
        if path.is_dir():
            abort(400, description="This download contains multiple files. Request a specific file path instead.")

        return send_file(path, as_attachment=True, download_name=path.name)

    @app.get("/downloads/<job_id>/<path:relative_path>")
    def download_artifact(job_id: str, relative_path: str):
        job = DOWNLOAD_MANAGER.get_job(job_id)
        if job is None or not job.output_path:
            abort(404)

        base_path = Path(job.output_path)
        if not base_path.exists():
            abort(404)

        if base_path.is_file():
            if relative_path != base_path.name:
                abort(404)
            return send_file(base_path, as_attachment=True, download_name=base_path.name)

        return send_from_directory(str(base_path), relative_path, as_attachment=True)

    return app


DOWNLOAD_MANAGER = DownloadManager(
    download_path=resolve_download_path(),
    max_concurrent=int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "1")),
    poll_interval=float(os.environ.get("DOWNLOAD_POLL_INTERVAL", str(DEFAULT_POLL_INTERVAL_SECONDS))),
)
DOWNLOAD_MANAGER.start()
atexit.register(DOWNLOAD_MANAGER.shutdown)

app = create_app()


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", os.environ.get("FLASK_PORT", "5000")))
    app.run(host=host, port=port, debug=False, threaded=True)
