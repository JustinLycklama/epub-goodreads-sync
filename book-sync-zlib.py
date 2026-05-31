"""
book-sync-zlib.py

Downloads missing Goodreads "want to read" books as ePubs via Z-Library.

Modes:
  Local:  Checks Calibre library to determine what's missing, adds downloads to Calibre.
  CI:     Uses downloaded.txt to track what's been fetched. Saves epubs to ./downloads/.
          Credentials read from ZLIBRARY_EMAIL / ZLIBRARY_PASSWORD env vars.

Dependencies:
    pip install feedparser requests beautifulsoup4 zlibrary
"""

import asyncio
import feedparser
import requests
import subprocess
import json
import os
import re
import sys
from pathlib import Path

import zlibrary
from zlibrary import Language, Extension

# --- Config ---
GOODREADS_USER_ID = "197955244"
CALIBRE_LIBRARY = r"C:\Users\Justin\Google Drive\Books\Calibre Library"
CALIBREDB = r"C:\Program Files\Calibre2\calibredb.exe"

CI = os.getenv("CI", "false").lower() == "true"
REPO_ROOT = Path(__file__).parent
DOWNLOAD_DIR = REPO_ROOT / "downloads"
DOWNLOADED_FILE = REPO_ROOT / "downloaded.txt"
LOCAL_TEMP_DIR = Path(r"C:\Users\Justin\Documents\callibre-temp")

RSS_URL = f"https://www.goodreads.com/review/list_rss/{GOODREADS_USER_ID}?shelf=to-read"

SESSION = requests.Session()
SESSION.headers["User-Agent"] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# --- Helpers ---

def normalize(text):
    text = text.lower()
    text = re.split(r"[:(]", text)[0]
    text = re.sub(r"[^\w\s]", "", text)
    return text.strip()


def clean_title(title):
    return re.sub(r"\s*\(.*?\)\s*$", "", title).strip()


# --- Book sources ---

def get_goodreads_books():
    print("Fetching Goodreads want-to-read shelf...")
    feed = feedparser.parse(RSS_URL)
    if feed.bozo and not feed.entries:
        print("ERROR: Could not fetch Goodreads RSS. Is your shelf set to public?")
        sys.exit(1)
    books = []
    for entry in feed.entries:
        title = entry.get("title", "").strip()
        author = (entry.get("author_name") or entry.get("author") or "").strip()
        if title:
            books.append({"title": title, "author": author})
    print(f"  {len(books)} books on shelf")
    return books


def get_existing_books():
    if CI:
        print("Reading downloaded.txt...")
        if not DOWNLOADED_FILE.exists():
            print("  (none yet)")
            return set()
        titles = {line.strip() for line in DOWNLOADED_FILE.read_text().splitlines() if line.strip()}
        print(f"  {len(titles)} books already downloaded")
        return titles
    else:
        print("Reading Calibre library...")
        result = subprocess.run(
            [CALIBREDB, "list", "--fields", "title,authors", "--for-machine",
             "--library-path", CALIBRE_LIBRARY],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"ERROR: calibredb failed: {result.stderr}")
            sys.exit(1)
        data = json.loads(result.stdout)
        titles = {normalize(book["title"]) for book in data}
        print(f"  {len(titles)} books in Calibre")
        return titles


def mark_downloaded(title):
    with DOWNLOADED_FILE.open("a") as f:
        f.write(normalize(clean_title(title)) + "\n")


# --- Z-Library ---

async def zlib_login():
    email = os.getenv("ZLIBRARY_EMAIL")
    password = os.getenv("ZLIBRARY_PASSWORD")
    if not email or not password:
        print("ERROR: ZLIBRARY_EMAIL and ZLIBRARY_PASSWORD must be set.")
        sys.exit(1)
    lib = zlibrary.AsyncZlib()
    await lib.login(email, password)
    try:
        limits = await lib.profile.get_limits()
        remaining = limits.get("daily_remaining", "?")
        print(f"  Logged in. Daily downloads remaining: {remaining}")
    except Exception:
        print("  Logged in.")
    return lib


async def zlib_search_and_download(lib, title, author, out_dir):
    search_title = clean_title(title)

    try:
        paginator = await lib.search(
            q=search_title,
            count=5,
            lang=[Language.ENGLISH],
            extensions=[Extension.EPUB],
        )
        results = await paginator.next()
    except Exception as e:
        print(f"  Search failed: {e}")
        return None

    if not results:
        print(f"  Not found on Z-Library: {search_title}")
        return None

    # Try results in order until one downloads successfully
    for result in results:
        try:
            book = await result.fetch()
        except Exception:
            continue

        dl_url = book.get("download_url")
        if not dl_url:
            continue

        try:
            r = SESSION.get(dl_url, timeout=90, stream=True, allow_redirects=True)
            if r.status_code != 200:
                continue
            content = b"".join(r.iter_content(8192))
            if len(content) < 10_000 or not content.startswith(b"PK"):
                continue
            safe_name = re.sub(r"[^\w\s-]", "", search_title)[:80].strip()
            filepath = out_dir / f"{safe_name}.epub"
            filepath.write_bytes(content)
            print(f"  Downloaded: {filepath.name}")
            return filepath
        except Exception:
            continue

    print(f"  All download attempts failed: {search_title}")
    return None


# --- Calibre (local only) ---

def add_to_calibre(filepath):
    result = subprocess.run(
        [CALIBREDB, "add", str(filepath), "--library-path", CALIBRE_LIBRARY],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"  Added to Calibre")
        filepath.unlink()
        return True
    else:
        print(f"  calibredb add failed: {result.stderr.strip()}")
        return False


# --- Main ---

async def main():
    out_dir = DOWNLOAD_DIR if CI else LOCAL_TEMP_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    if CI:
        print("Running in CI mode — downloads go to ./downloads/, tracking via downloaded.txt")
    else:
        print("Running in local mode — checking Calibre, adding downloaded books automatically")

    print("Logging in to Z-Library...")
    lib = await zlib_login()

    gr_books = get_goodreads_books()
    existing = get_existing_books()

    missing = [b for b in gr_books if normalize(clean_title(b["title"])) not in existing]

    if not missing:
        print("\nAll shelf books are already downloaded.")
        return

    print(f"\n{len(missing)} book(s) to download:")
    for b in missing:
        print(f"  - {b['title']} ({b['author']})")

    print()
    skipped = []
    for book in missing:
        title, author = book["title"], book["author"]
        print(f"[{title}]")
        filepath = await zlib_search_and_download(lib, title, author, out_dir)
        if filepath:
            if CI:
                mark_downloaded(title)
            else:
                add_to_calibre(filepath)
        else:
            skipped.append(title)

    print("\nDone.")
    if skipped:
        print(f"\nNot found / failed ({len(skipped)}):")
        for t in skipped:
            print(f"  - {t}")


if __name__ == "__main__":
    asyncio.run(main())
