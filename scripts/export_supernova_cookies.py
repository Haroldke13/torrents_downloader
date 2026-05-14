#!/usr/bin/env python3
import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path

import gi
from Cryptodome.Cipher import AES
from Cryptodome.Protocol.KDF import PBKDF2

gi.require_version("Secret", "1")
from gi.repository import Secret


COOKIE_HOST_SNIPPETS = ("supernova", "wootly")
COOKIE_DB_NAME = "Cookies"
DEFAULT_CHROME_DIR = Path.home() / ".config" / "google-chrome"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Supernova/Wootly cookies from a local Chrome profile."
    )
    parser.add_argument(
        "--profile",
        help="Chrome profile directory name, for example 'Profile 4' or 'Default'. "
        "If omitted, the script auto-detects a profile with matching cookies.",
    )
    parser.add_argument(
        "--chrome-dir",
        default=str(DEFAULT_CHROME_DIR),
        help="Chrome user-data directory. Defaults to ~/.config/google-chrome.",
    )
    return parser.parse_args()


def load_chrome_secret() -> str:
    service = Secret.Service.get_sync(Secret.ServiceFlags.LOAD_COLLECTIONS, None)
    for collection in service.get_collections():
        collection.load_items_sync(None)
        for item in collection.get_items():
            if item.get_label() != "Chrome Safe Storage":
                continue
            item.load_secret_sync(None)
            secret = item.get_secret()
            if secret is None:
                continue
            secret_text = secret.get_text()
            if secret_text:
                return secret_text
    raise RuntimeError("Chrome Safe Storage secret was not found in the desktop keyring.")


def find_profile(chrome_dir: Path, profile_name: str | None) -> Path:
    if profile_name:
        cookie_db = chrome_dir / profile_name / COOKIE_DB_NAME
        if not cookie_db.exists():
            raise FileNotFoundError(f"Chrome cookie database was not found for profile '{profile_name}'.")
        return cookie_db

    candidates = sorted(path for path in chrome_dir.glob(f"*/{COOKIE_DB_NAME}") if path.is_file())
    for cookie_db in candidates:
        with sqlite3.connect(f"file:{cookie_db}?mode=ro", uri=True) as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM cookies
                WHERE host_key LIKE '%supernova%' OR host_key LIKE '%wootly%'
                LIMIT 1
                """
            ).fetchone()
        if row:
            return cookie_db

    raise FileNotFoundError("No Chrome profile with Supernova or Wootly cookies was found.")


def decrypt_cookie_value(encrypted_value: bytes, host_key: str, secret_text: str) -> str:
    if not encrypted_value:
        return ""

    if not encrypted_value.startswith((b"v10", b"v11")):
        return encrypted_value.decode("utf-8", errors="ignore")

    key = PBKDF2(secret_text.encode(), b"saltysalt", dkLen=16, count=1)
    cipher = AES.new(key, AES.MODE_CBC, IV=b" " * 16)
    padded = cipher.decrypt(encrypted_value[3:])
    plaintext = padded[: -padded[-1]]

    host_hash = hashlib.sha256(host_key.encode()).digest()
    if plaintext.startswith(host_hash):
        plaintext = plaintext[len(host_hash) :]

    return plaintext.decode("utf-8", errors="ignore")


def export_cookie_header(cookie_db: Path, secret_text: str) -> str:
    pairs: list[tuple[str, str]] = []

    with sqlite3.connect(f"file:{cookie_db}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            """
            SELECT host_key, name, value, encrypted_value
            FROM cookies
            WHERE host_key LIKE '%supernova%' OR host_key LIKE '%wootly%'
            ORDER BY host_key, name
            """
        ).fetchall()

    for host_key, name, value, encrypted_value in rows:
        if not any(snippet in host_key for snippet in COOKIE_HOST_SNIPPETS):
            continue
        cookie_value = value or decrypt_cookie_value(encrypted_value, host_key, secret_text)
        if cookie_value:
            pairs.append((name, cookie_value))

    if not pairs:
        raise RuntimeError(f"No usable cookies were found in {cookie_db.parent.name}.")

    # Later duplicates override earlier ones so the most specific value wins.
    deduped: dict[str, str] = {}
    for name, value in pairs:
        deduped[name] = value

    return "; ".join(f"{name}={value}" for name, value in deduped.items())


def main() -> int:
    args = parse_args()
    chrome_dir = Path(args.chrome_dir).expanduser()
    if not chrome_dir.exists():
        raise FileNotFoundError(f"Chrome directory does not exist: {chrome_dir}")

    secret_text = load_chrome_secret()
    cookie_db = find_profile(chrome_dir, args.profile)
    print(export_cookie_header(cookie_db, secret_text))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
