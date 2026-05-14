import csv
import html
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import List

import libtorrent as lt


SPINNER_FRAMES = "|/-\\"
HTTP_SCHEMES = {"http", "https"}
DIRECT_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
MAGNET_URI_PATTERN = re.compile(r"magnet:\?[^\s<>'\"]+", flags=re.IGNORECASE)
HTTP_URL_PATTERN = re.compile(r"https?://[^\s<>'\"]+", flags=re.IGNORECASE)
LOCAL_SOURCE_LIST_FILE_EXTENSIONS = {".txt", ".csv", ".tsv", ".json", ".html", ".htm"}


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
class TorrentJob:
    source: str
    label: str
    kind: str
    handle: lt.torrent_handle | None = None
    temp_path: str | None = None
    output_path: str | None = None
    error: str | None = None
    completed: bool = False
    started: bool = False


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

    if parsed.scheme in {"http", "https"}:
        with urllib.request.urlopen(torrent_link) as response:
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


def format_status_line(status, tick: int) -> str:
    """Render a compact single-line progress summary."""

    state_name = "downloading"
    try:
        state_name = str(status.states[status.state]).replace("_", " ")
    except (AttributeError, IndexError, TypeError):
        pass

    if not status.has_metadata:
        return f"{SPINNER_FRAMES[tick % len(SPINNER_FRAMES)]} Fetching metadata... Peers: {status.num_peers}"

    total_wanted = getattr(status, "total_wanted", 0) or 0
    total_done = getattr(status, "total_done", 0) or 0
    if total_wanted > 0:
        size_text = f"{total_done / (1024 * 1024):.1f}/{total_wanted / (1024 * 1024):.1f} MiB"
    else:
        size_text = f"{total_done / (1024 * 1024):.1f} MiB"

    return (
        f"{status.progress * 100:6.2f}% | "
        f"DL {status.download_rate / 1000:7.1f} kB/s | "
        f"UL {status.upload_rate / 1000:7.1f} kB/s | "
        f"Peers {status.num_peers:3d} | "
        f"{state_name} | "
        f"{size_text}"
    )


def print_status_line(message: str, previous_width: int) -> int:
    """Update a single terminal line without leaving old characters behind."""

    padded = message.ljust(previous_width)
    sys.stdout.write(f"\r{padded}")
    sys.stdout.flush()
    return max(previous_width, len(message))


def print_status_block(lines: List[str], previous_line_count: int) -> int:
    """Render a multi-line live status block in place."""

    if previous_line_count:
        sys.stdout.write(f"\x1b[{previous_line_count}F")

    for line in lines:
        sys.stdout.write("\x1b[2K")
        sys.stdout.write(f"{line}\n")

    for _ in range(max(0, previous_line_count - len(lines))):
        sys.stdout.write("\x1b[2K\n")

    sys.stdout.flush()
    return len(lines)


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


def looks_like_torrent_source(source: str) -> bool:
    """Return True when a pasted source should be handled by libtorrent."""

    if source.startswith("magnet:?"):
        return True

    if os.path.isfile(source):
        return source.lower().endswith(".torrent")

    parsed = urllib.parse.urlparse(source)
    return parsed.scheme in HTTP_SCHEMES and parsed.path.lower().endswith(".torrent")


def resolve_download_path() -> str:
    """Choose the real device Downloads directory when one is available."""

    override = os.environ.get("DOWNLOADS_DIR") or os.environ.get("MOVIES_DOWNLOADS_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))

    home = Path.home()
    prefix = os.environ.get("PREFIX", "")
    is_android_like = bool(os.environ.get("TERMUX_VERSION") or os.environ.get("ANDROID_ROOT") or "com.termux" in prefix)

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


def parse_torrent_links(raw_value: str) -> List[str]:
    """Split pasted sources into a clean list without breaking embedded magnet URIs."""

    magnet_links = MAGNET_URI_PATTERN.findall(raw_value)
    remainder = MAGNET_URI_PATTERN.sub("\n", raw_value)
    other_sources = [item.strip() for item in re.split(r"[\r\n,]+", remainder) if item.strip()]
    return magnet_links + other_sources


def canonicalize_source(source: str) -> str:
    """Build a stable dedupe key for a pasted download source."""

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


def dedupe_sources(sources: List[str]) -> List[str]:
    """Keep the first instance of each source while removing repeats."""

    unique_sources: List[str] = []
    seen_keys: set[str] = set()

    for source in sources:
        key = canonicalize_source(source)
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        unique_sources.append(source.strip())

    return unique_sources


def clean_extracted_source(source: str) -> str:
    """Trim markup and trailing punctuation from an extracted source candidate."""

    source = html.unescape(source).strip().strip("<>\"'")
    return source.rstrip("),.;]}")


def extract_sources_from_text_blob(raw_text: str, include_http_urls: bool = True) -> List[str]:
    """Extract magnet links and optionally HTTP(S) URLs from arbitrary local text."""

    decoded = html.unescape(raw_text)
    sources = [clean_extracted_source(item) for item in MAGNET_URI_PATTERN.findall(decoded)]

    if include_http_urls:
        sources.extend(clean_extracted_source(item) for item in HTTP_URL_PATTERN.findall(decoded))

    return [source for source in sources if source]


def read_local_text_file(file_path: str) -> str:
    """Read a local text-like file tolerantly for source extraction."""

    with open(file_path, "r", encoding="utf-8", errors="ignore") as handle:
        return handle.read()


def load_torrent_links_from_text_file(file_path: str) -> List[str]:
    """Load torrent sources from a local plain-text file."""

    filtered_lines = []
    for line in read_local_text_file(file_path).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        filtered_lines.append(line)

    return parse_torrent_links("\n".join(filtered_lines))


def choose_csv_dialect(sample: str, file_path: str):
    """Pick a reasonable CSV dialect for a local delimited source file."""

    if file_path.lower().endswith(".tsv"):
        return csv.excel_tab

    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def load_torrent_links_from_csv_file(file_path: str) -> List[str]:
    """Load torrent sources from a local CSV or TSV file."""

    sample = read_local_text_file(file_path)
    dialect = choose_csv_dialect(sample[:4096], file_path)
    sources: List[str] = []

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


def iter_json_strings(value):
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


def load_torrent_links_from_json_file(file_path: str) -> List[str]:
    """Load torrent sources from a local JSON file."""

    raw_text = read_local_text_file(file_path)

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return dedupe_sources(extract_sources_from_text_blob(raw_text))

    sources: List[str] = []
    for text_value in iter_json_strings(payload):
        embedded_sources = extract_sources_from_text_blob(text_value)
        if embedded_sources:
            sources.extend(embedded_sources)
            continue

        stripped = text_value.strip()
        if stripped.startswith("magnet:?") or stripped.startswith("http://") or stripped.startswith("https://"):
            sources.extend(parse_torrent_links(stripped))

    return dedupe_sources(sources)


def load_torrent_links_from_html_file(file_path: str) -> List[str]:
    """Load torrent sources from a saved local HTML page without scraping the remote site."""

    raw_text = read_local_text_file(file_path)
    parser = LocalHtmlLinkParser()
    parser.feed(raw_text)

    sources: List[str] = []
    for href in parser.hrefs:
        sources.extend(extract_sources_from_text_blob(href))

    sources.extend(clean_extracted_source(item) for item in MAGNET_URI_PATTERN.findall(html.unescape(raw_text)))
    return dedupe_sources([source for source in sources if source])


def load_torrent_links_from_source_file(file_path: str) -> List[str]:
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


def resolve_torrent_sources(raw_value: str) -> List[str]:
    """Expand pasted sources and supported local list files into one flat list."""

    resolved_links: List[str] = []
    for item in parse_torrent_links(raw_value):
        if os.path.isfile(item) and Path(item).suffix.lower() in LOCAL_SOURCE_LIST_FILE_EXTENSIONS:
            resolved_links.extend(load_torrent_links_from_source_file(item))
            continue
        resolved_links.append(item)
    return dedupe_sources(resolved_links)


def shorten_source_label(torrent_link: str, max_length: int = 58) -> str:
    """Trim long labels so status lines stay readable."""

    if len(torrent_link) <= max_length:
        return torrent_link
    return f"...{torrent_link[-(max_length - 3):]}"


def derive_torrent_label(torrent_link: str) -> str:
    """Choose a readable label for a torrent source before metadata is available."""

    if torrent_link.startswith("magnet:?"):
        parsed = urllib.parse.urlparse(torrent_link)
        display_name = urllib.parse.parse_qs(parsed.query).get("dn", [])
        if display_name and display_name[0].strip():
            return display_name[0].strip()
        info_hash = urllib.parse.parse_qs(parsed.query).get("xt", [torrent_link])[-1]
        return info_hash.rsplit(":", 1)[-1]

    parsed = urllib.parse.urlparse(torrent_link)
    if parsed.scheme in {"http", "https"}:
        filename = os.path.basename(parsed.path.rstrip("/"))
        return urllib.parse.unquote(filename) or torrent_link

    return os.path.basename(torrent_link.rstrip(os.sep)) or torrent_link


def sanitize_filename(filename: str) -> str:
    """Remove path separators and reserved characters from a download name."""

    cleaned = re.sub(r'[<>:"/\\\\|?*\x00-\x1f]+', "_", filename).strip().strip(".")
    return cleaned or "download"


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


def download_direct_url(source_url: str, download_path: str) -> str:
    """Download a regular HTTP/HTTPS file straight to the chosen download folder."""

    request = urllib.request.Request(source_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request) as response:
        filename = derive_direct_download_name(source_url, response.headers)
        output_path = unique_output_path(download_path, filename)
        total_bytes_header = response.headers.get("Content-Length", "").strip()
        total_bytes = int(total_bytes_header) if total_bytes_header.isdigit() else None
        downloaded_bytes = 0
        previous_width = 0
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

                if total_bytes:
                    progress_percent = downloaded_bytes / total_bytes * 100
                    message = (
                        f"File | {os.path.basename(output_path)} | "
                        f"{progress_percent:6.2f}% | "
                        f"{format_byte_count(downloaded_bytes)}/{format_byte_count(total_bytes)} | "
                        f"{format_byte_count(int(speed))}/s"
                    )
                else:
                    message = (
                        f"File | {os.path.basename(output_path)} | "
                        f"{format_byte_count(downloaded_bytes)} downloaded | "
                        f"{format_byte_count(int(speed))}/s"
                    )

                previous_width = print_status_line(message, previous_width)

        if previous_width:
            sys.stdout.write("\n")
            sys.stdout.flush()

        return output_path


def get_job_display_name(job: TorrentJob) -> str:
    """Return the best available name for a torrent job."""

    if job.handle is None:
        return job.label

    try:
        status = job.handle.status()
    except RuntimeError:
        return job.label

    name = getattr(status, "name", "") or ""
    return name.strip() or job.label


def build_progress_header(jobs: List[TorrentJob]) -> str:
    """Summarize which torrent is active and how far the queue has progressed."""

    success_count = sum(1 for job in jobs if job.completed)
    failure_count = sum(1 for job in jobs if job.error)
    active_job = next((job for job in jobs if job.handle is not None and not job.error and not job.completed), None)

    if active_job is not None:
        return (
            f"Progress: {success_count}/{len(jobs)} done | "
            f"{failure_count} failed | "
            f"Now downloading: {shorten_source_label(get_job_display_name(active_job), 72)}"
        )

    return f"Progress: {success_count}/{len(jobs)} done | {failure_count} failed | Waiting for next item"


def cleanup_job(session: lt.session, job: TorrentJob) -> None:
    """Remove a torrent from the session and delete any temporary .torrent file."""

    if job.handle is not None:
        try:
            session.remove_torrent(job.handle)
        except RuntimeError:
            pass
        job.handle = None

    if job.temp_path and os.path.exists(job.temp_path):
        os.remove(job.temp_path)
    job.temp_path = None


def start_next_queued_job(session: lt.session, jobs: List[TorrentJob], download_path: str) -> None:
    """Start the next queued torrent if no torrent is currently active."""

    if any(job.started and not job.completed and not job.error and job.handle is not None for job in jobs):
        return

    for job in jobs:
        if job.started or job.completed or job.error:
            continue
        try:
            if job.kind == "direct":
                job.output_path = download_direct_url(job.source, download_path)
                job.completed = True
            else:
                handle, temp_path = build_torrent_handle(session, job.source, download_path)
                job.handle = handle
                job.temp_path = temp_path
            job.started = True
        except Exception as exc:
            job.error = str(exc)
            job.started = True
        return


def format_job_line(job: TorrentJob, index: int, total_jobs: int, tick: int) -> str:
    """Format one torrent row for the live queued download display."""

    label = shorten_source_label(get_job_display_name(job))
    prefix = f"[{index}/{total_jobs}]"
    source_type = "FILE" if job.kind == "direct" else "TORR"

    if job.error:
        return f"{prefix} {source_type} | FAILED | {label} | {job.error}"

    if job.completed:
        return f"{prefix} {source_type} | DONE   | {label}"

    if not job.started:
        return f"{prefix} {source_type} | QUEUED | {label}"

    if job.handle is None:
        return f"{prefix} {source_type} | WAIT   | {label}"

    status = job.handle.status()
    return f"{prefix} {source_type} | ACTIVE | {label} | {format_status_line(status, tick)}"


def download_torrents(torrent_links: List[str], download_path: str | None = None):
    """Queue torrent jobs plus direct-file URLs and save them sequentially."""

    download_path = download_path or resolve_download_path()
    os.makedirs(download_path, exist_ok=True)

    session = lt.session()
    jobs = [
        TorrentJob(
            source=torrent_link,
            label=derive_torrent_label(torrent_link),
            kind="torrent" if looks_like_torrent_source(torrent_link) else "direct",
        )
        for torrent_link in torrent_links
    ]
    previous_line_count = 0

    try:
        print(f"Queued {len(jobs)} download source(s)")
        print(f"Saving to: {download_path}\n")
        tick = 0

        try:
            while True:
                start_next_queued_job(session, jobs, download_path)

                active_jobs = 0
                for job in jobs:
                    if job.error or job.completed or job.handle is None:
                        continue

                    status = job.handle.status()
                    error_message = get_error_message(status)
                    if error_message:
                        job.error = f"Torrent error: {error_message}"
                        cleanup_job(session, job)
                        continue

                    if status.is_seeding:
                        job.completed = True
                        cleanup_job(session, job)
                        continue

                    active_jobs += 1

                lines = [build_progress_header(jobs), ""] + [
                    format_job_line(job, index, len(jobs), tick)
                    for index, job in enumerate(jobs, start=1)
                ]
                previous_line_count = print_status_block(lines, previous_line_count)
                tick += 1

                if active_jobs == 0 and all(job.started or job.error for job in jobs):
                    break

                time.sleep(1)
        except KeyboardInterrupt:
            print_status_block(
                ["Stopping torrents and leaving partial data in place..."],
                previous_line_count,
            )
            sys.stdout.flush()
            for job in jobs:
                if job.handle is None:
                    continue
                try:
                    job.handle.pause()
                except RuntimeError:
                    pass
            try:
                session.pause()
            except RuntimeError:
                pass
            return

        sys.stdout.write("\nCompleted downloads.\n")
        sys.stdout.flush()
    finally:
        for job in jobs:
            cleanup_job(session, job)

    success_count = sum(1 for job in jobs if job.completed)
    failure_count = sum(1 for job in jobs if job.error)
    print(f"Successes: {success_count}")
    print(f"Failures: {failure_count}")

    failures = [(job.source, job.error) for job in jobs if job.error]
    if failures:
        print("\nCompleted with failures:")
        for torrent_link, message in failures:
            print(f"- {torrent_link}: {message}")
        raise SystemExit(1)


if __name__ == "__main__":
    torrent_input = input(
        "Paste magnet links, .torrent paths/URLs, direct file URLs, or a local .txt/.csv/.tsv/.json/.html list file, separated by commas or new lines: "
    ).strip()
    torrent_links = resolve_torrent_sources(torrent_input)

    if not torrent_links:
        raise SystemExit("No download source was provided.")

    download_torrents(torrent_links)
