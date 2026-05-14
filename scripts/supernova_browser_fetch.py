#!/usr/bin/env python3
import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from export_supernova_cookies import (
    DEFAULT_CHROME_DIR,
    export_cookie_header,
    find_profile,
    load_chrome_secret,
)


BASE_URL = "https://supernova.to"
DEFAULT_LIMIT = 8


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch Supernova search results through a live Selenium browser session."
    )
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--query", help="Search query to run against Supernova.")
    input_group.add_argument("--url", help="Direct Supernova movie, episode, or series URL.")
    parser.add_argument("--profile", help="Chrome profile name, for example 'Profile 4'.")
    parser.add_argument(
        "--chrome-dir",
        default=None,
        help="Chrome user-data directory. Defaults to the same value used by the cookie export helper.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Maximum number of search results to enrich. Defaults to {DEFAULT_LIMIT}.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chrome in headless mode. This is usually less reliable for Wootly playback.",
    )
    return parser.parse_args()


def build_driver(headless: bool) -> webdriver.Chrome:
    options = Options()
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1440,1100")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
    return webdriver.Chrome(options=options)


def inject_cookies(driver: webdriver.Chrome, cookie_header: str) -> None:
    driver.execute_cdp_cmd("Network.enable", {})

    parsed: dict[str, str] = {}
    for item in cookie_header.split("; "):
        name, value = item.split("=", 1)
        parsed[name] = value

    for name in ("PLAY_IT", "cf_clearance"):
        if name in parsed:
            driver.execute_cdp_cmd(
                "Network.setCookie",
                {
                    "name": name,
                    "value": parsed[name],
                    "domain": ".supernova.to",
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                },
            )

    if "_0x9dc6a" in parsed:
        driver.execute_cdp_cmd(
            "Network.setCookie",
            {
                "name": "_0x9dc6a",
                "value": parsed["_0x9dc6a"],
                "domain": "supernova.to",
                "path": "/",
                "secure": True,
                "httpOnly": False,
            },
        )

    if "wootsses" in parsed:
        driver.execute_cdp_cmd(
            "Network.setCookie",
            {
                "name": "wootsses",
                "value": parsed["wootsses"],
                "domain": ".wootly.ch",
                "path": "/",
                "secure": True,
                "httpOnly": False,
            },
        )


def wait_for_ready(driver: webdriver.Chrome, seconds: float = 0.0) -> None:
    WebDriverWait(driver, 20).until(
        lambda current: current.execute_script("return document.readyState") == "complete"
    )
    if seconds > 0:
        time.sleep(seconds)


def clean_text(value: str) -> str:
    return " ".join((value or "").split())


def extract_image_url(image, page_url: str) -> str | None:
    if not image:
        return None
    for attribute in ("data-src", "data-lazy-src", "data-original", "src"):
        value = image.get(attribute)
        if value:
            return urljoin(page_url, value)
    return None


def extract_description(soup: BeautifulSoup) -> str | None:
    selectors = (
        ".marl p",
        ".fimm p",
        ".description",
        ".plot",
        ".entry-content p",
        ".content p",
    )
    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        text = clean_text(node.get_text(" ", strip=True))
        if text:
            return text
    return None


def extract_heading_text(soup: BeautifulSoup) -> str | None:
    for selector in ("#lysing h1", ".marl h1", "h1", "title"):
        node = soup.select_one(selector)
        if not node:
            continue
        text = clean_text(node.get_text(" ", strip=True))
        if text:
            return text
    return None


def extract_direct_video_url(soup: BeautifulSoup, page_url: str) -> str | None:
    selectors = [
        ".main-con #video-container .vid-holder video[src]",
        "#video-container .vid-holder video[src]",
        "#video-container video[src]",
        ".main-con video[src]",
        "video[src]",
        "#dld a[href]",
        "#video-container a[href]",
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        candidate = clean_text(node.get("src") or node.get("href") or "")
        if not candidate or candidate.lower().startswith("javascript:"):
            continue
        return urljoin(page_url, candidate)
    return None


def is_final_video_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    if "wootly.ch" in parsed.netloc:
        return parsed.path.startswith("/source")
    return True


def current_video_candidate(driver: webdriver.Chrome) -> str | None:
    selectors = [
        ".main-con #video-container .vid-holder video[src]",
        "#video-container .vid-holder video[src]",
        "#video-container video[src]",
        ".main-con video[src]",
        "video[src]",
        "#dld a[href]",
        "#video-container a[href]",
    ]
    for selector in selectors:
        nodes = driver.find_elements(By.CSS_SELECTOR, selector)
        if not nodes:
            continue
        candidate = nodes[0].get_attribute("src") or nodes[0].get_attribute("href") or ""
        candidate = clean_text(candidate)
        if not candidate:
            continue
        return urljoin(driver.current_url, candidate)
    return None


def slug_prefix(detail_url: str | None) -> str:
    slug = (detail_url or "").rstrip("/").rsplit("/", 1)[-1]
    return slug[:1].lower()


def season_episode_sort_key(record: dict[str, str | None]) -> tuple[int, int, str]:
    season = int(record.get("season_number") or 0)
    episode = int(record.get("episode_number") or 0)
    return (season, episode, record.get("detail_url") or "")


def build_record(detail_url: str, title: str | None = None) -> dict[str, str | None]:
    return {
        "title": title,
        "detail_url": detail_url,
        "poster_url": None,
        "description": None,
        "video_url": None,
        "season_number": None,
        "episode_number": None,
    }


def search_results(driver: webdriver.Chrome, query: str, limit: int) -> list[dict[str, str | None]]:
    driver.get(BASE_URL + "/")
    wait_for_ready(driver, 2)

    search_input = WebDriverWait(driver, 20).until(
        lambda current: current.find_element(By.ID, "putin")
    )
    search_input.clear()
    search_input.send_keys(query)

    result_panel = WebDriverWait(driver, 20).until(
        lambda current: current.find_element(By.ID, "result")
    )
    WebDriverWait(driver, 20).until(lambda _: result_panel.get_attribute("innerHTML").strip())

    soup = BeautifulSoup(result_panel.get_attribute("innerHTML"), "html.parser")
    records: list[dict[str, str | None]] = []
    seen: set[str] = set()

    for link in soup.select(".mfeed a[href], a[href]"):
        href = urljoin(BASE_URL + "/", link.get("href", "").strip())
        title = clean_text(link.get_text(" ", strip=True))
        if not href.startswith(BASE_URL) or not title or href in seen:
            continue
        seen.add(href)
        records.append(
            build_record(href, title=title)
        )
        if len(records) >= limit:
            break

    return records


def get_page_soup(driver: webdriver.Chrome, url: str, delay: float = 2.0) -> BeautifulSoup:
    driver.get(url)
    wait_for_ready(driver, delay)
    return BeautifulSoup(driver.page_source, "html.parser")


def parse_episode_entries(soup: BeautifulSoup, page_url: str, series_title: str | None) -> list[dict[str, str | None]]:
    episodes: list[dict[str, str | None]] = []
    for entry in soup.select("#seon .seho"):
        link = entry.select_one(".snfo h1 a[href]")
        if not link:
            continue

        detail_url = urljoin(page_url, link.get("href", "").strip())
        episode_title = clean_text(link.get_text(" ", strip=True))
        season_node = entry.select_one(".seep span:not(.sea)")
        episode_node = entry.select_one(".seep .sea")
        season_label = clean_text(season_node.get_text(" ", strip=True) if season_node else "")
        episode_label = clean_text(episode_node.get_text(" ", strip=True) if episode_node else "")

        season_match = re.search(r"(\d+)", season_label)
        episode_match = re.search(r"(\d+)", episode_label)
        season_number = int(season_match.group(1)) if season_match else None
        episode_number = int(episode_match.group(1)) if episode_match else None

        prefix = clean_text(series_title or "")
        if season_number is not None and episode_number is not None:
            title = f"{prefix} S{season_number:02d}E{episode_number:02d} - {episode_title}".strip()
        else:
            title = f"{prefix} - {episode_title}".strip(" -")

        episodes.append(
            {
                "title": title,
                "detail_url": detail_url,
                "poster_url": None,
                "description": None,
                "video_url": None,
                "season_number": season_number,
                "episode_number": episode_number,
            }
        )

    return episodes


def collect_series_episode_records(driver: webdriver.Chrome, series_url: str, series_soup: BeautifulSoup) -> list[dict[str, str | None]]:
    series_title = extract_heading_text(series_soup)
    records: dict[str, dict[str, str | None]] = {}

    def harvest_current_page() -> None:
        current_soup = BeautifulSoup(driver.page_source, "html.parser")
        for record in parse_episode_entries(current_soup, driver.current_url, series_title):
            records.setdefault(record["detail_url"], record)

    harvest_current_page()

    season_ids = []
    for button in driver.find_elements(By.CSS_SELECTOR, "#sesh button[data-season]"):
        season_id = button.get_attribute("data-season")
        if season_id:
            season_ids.append(season_id)

    for season_id in season_ids:
        button = driver.find_element(By.CSS_SELECTOR, f"#sesh button[data-season='{season_id}']")
        if button.get_attribute("disabled"):
            continue
        button.click()
        time.sleep(2)
        WebDriverWait(driver, 20).until(lambda current: current.find_elements(By.CSS_SELECTOR, "#seon .seho"))
        harvest_current_page()

    return sorted(records.values(), key=season_episode_sort_key)


def open_wootly_player(driver: webdriver.Chrome, iframe_url: str) -> str | None:
    soup = get_page_soup(driver, iframe_url, delay=2.5)
    direct = extract_direct_video_url(soup, driver.current_url)
    if is_final_video_url(direct):
        return direct

    try:
        play_button = WebDriverWait(driver, 20).until(
            lambda current: current.find_element(By.ID, "prime")
        )
    except TimeoutException:
        return direct

    play_button.click()

    try:
        WebDriverWait(driver, 20).until(
            lambda current: is_final_video_url(current_video_candidate(current))
        )
    except TimeoutException:
        pass

    deadline = time.time() + 10
    while time.time() < deadline:
        direct = current_video_candidate(driver)
        if is_final_video_url(direct):
            return direct
        time.sleep(1)

    return None


def enrich_record(driver: webdriver.Chrome, record: dict[str, str | None]) -> dict[str, str | None]:
    detail_url = record["detail_url"] or BASE_URL
    soup = get_page_soup(driver, detail_url, delay=2.5)

    record["title"] = record.get("title") or extract_heading_text(soup)
    image = soup.select_one("#poster img, .imrl img, img")
    record["poster_url"] = record["poster_url"] or extract_image_url(image, detail_url)
    record["description"] = record["description"] or extract_description(soup)

    direct_video = extract_direct_video_url(soup, detail_url)
    if is_final_video_url(direct_video):
        record["video_url"] = direct_video
        return record

    iframe = soup.select_one("#vidcon iframe[src], iframe[src*='wootly']")
    if iframe and iframe.get("src"):
        record["video_url"] = open_wootly_player(driver, urljoin(detail_url, iframe["src"]))
        return record

    episode_link = soup.select_one("#seon .snfo h1 a[href], .seho .snfo h1 a[href]")
    if episode_link and episode_link.get("href"):
        episode_url = urljoin(detail_url, episode_link["href"])
        episode_soup = get_page_soup(driver, episode_url, delay=2.5)
        iframe = episode_soup.select_one("#vidcon iframe[src], iframe[src*='wootly']")
        if iframe and iframe.get("src"):
            record["video_url"] = open_wootly_player(driver, urljoin(episode_url, iframe["src"]))

    return record


def resolve_url_records(driver: webdriver.Chrome, detail_url: str) -> list[dict[str, str | None]]:
    soup = get_page_soup(driver, detail_url, delay=2.5)
    prefix = slug_prefix(driver.current_url)

    if prefix == "t":
        records = collect_series_episode_records(driver, driver.current_url, soup)
        return [enrich_record(driver, record) for record in records]

    record = build_record(driver.current_url, title=extract_heading_text(soup))
    return [enrich_record(driver, record)]


def serialize_records(records: list[dict[str, str | None]]) -> list[dict[str, str | None]]:
    payload: list[dict[str, str | None]] = []
    for record in records:
        payload.append(
            {
                "title": record.get("title"),
                "detail_url": record.get("detail_url"),
                "poster_url": record.get("poster_url"),
                "description": record.get("description"),
                "video_url": record.get("video_url"),
            }
        )
    return payload


def main() -> int:
    args = parse_args()
    secret_text = load_chrome_secret()
    chrome_dir = Path(args.chrome_dir).expanduser() if args.chrome_dir else DEFAULT_CHROME_DIR
    cookie_db = find_profile(chrome_dir, args.profile)
    cookie_header = export_cookie_header(cookie_db, secret_text)

    driver = build_driver(headless=args.headless)
    try:
        inject_cookies(driver, cookie_header)
        if args.query:
            records = search_results(driver, args.query, max(1, args.limit))
            resolved = [enrich_record(driver, record) for record in records]
        else:
            resolved = resolve_url_records(driver, args.url)
        print(json.dumps(serialize_records(resolved)))
        return 0
    finally:
        driver.quit()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
