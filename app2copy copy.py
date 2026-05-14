import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import List

import libtorrent as lt


SPINNER_FRAMES = "|/-\\"


@dataclass
class TorrentJob:
    source: str
    label: str
    handle: lt.torrent_handle | None = None
    temp_path: str | None = None
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


def parse_torrent_links(raw_value: str) -> List[str]:
    """Split comma-separated or newline-separated torrent sources into a clean list."""

    return [item.strip() for item in re.split(r"[\r\n,]+", raw_value) if item.strip()]


def load_torrent_links_from_text_file(file_path: str) -> List[str]:
    """Load torrent sources from a local text file."""

    with open(file_path, "r", encoding="utf-8") as handle:
        filtered_lines = []
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            filtered_lines.append(line)
    return parse_torrent_links("".join(filtered_lines))


def resolve_torrent_sources(raw_value: str) -> List[str]:
    """Expand pasted torrent sources and local .txt files into one flat list."""

    resolved_links: List[str] = []
    for item in parse_torrent_links(raw_value):
        if os.path.isfile(item) and item.lower().endswith(".txt"):
            resolved_links.extend(load_torrent_links_from_text_file(item))
            continue
        resolved_links.append(item)
    return resolved_links


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

    if job.error:
        return f"{prefix} FAILED | {label} | {job.error}"

    if job.completed:
        return f"{prefix} DONE   | {label}"

    if not job.started:
        return f"{prefix} QUEUED | {label}"

    if job.handle is None:
        return f"{prefix} WAIT   | {label}"

    status = job.handle.status()
    return f"{prefix} ACTIVE | {label} | {format_status_line(status, tick)}"


def download_torrents(torrent_links: List[str], download_path: str = "./downloads"):
    """Queue multiple torrents and run one active download at a time."""

    os.makedirs(download_path, exist_ok=True)

    session = lt.session()
    jobs = [TorrentJob(source=torrent_link, label=derive_torrent_label(torrent_link)) for torrent_link in torrent_links]
    previous_line_count = 0

    try:
        print(f"Queued {len(jobs)} torrent(s)")
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
        "Paste torrent links, .torrent paths/URLs, or a .txt list file, separated by commas or new lines: "
    ).strip()
    torrent_links = resolve_torrent_sources(torrent_input)

    if not torrent_links:
        raise SystemExit("No torrent source was provided.")

    download_torrents(torrent_links)



