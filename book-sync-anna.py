"""
book-sync-anna.py

Downloads missing Goodreads "want to read" books as ePubs via Anna's Archive.

Modes:
  Local:  Checks Calibre library to determine what's missing, adds downloads to Calibre.
          Requires WARP/VPN active if your ISP blocks Anna's Archive.
  CI:     Uses downloaded.txt to track what's been fetched. Saves epubs to ./downloads/.
          Runs unblocked on GitHub Actions.

Dependencies:
    pip install feedparser requests beautifulsoup4
"""

import feedparser
import requests
import subprocess
import json
import os
import re
import sys
import time
from pathlib import Path
from bs4 import BeautifulSoup

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
ANNA_BASE = "https://annas-archive.org"

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
    """Return set of normalised titles already owned, from Calibre (local) or downloaded.txt (CI)."""
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
    """Append normalised title to downloaded.txt (CI only)."""
    with DOWNLOADED_FILE.open("a") as f:
        f.write(normalize(clean_title(title)) + "\n")


# --- Anna's Archive ---

def anna_search(query):
    url = f"{ANNA_BASE}/search"
    params = {"q": query, "ext": "epub", "lang": "en"}
    try:
        r = SESSION.get(url, params=params, timeout=20)
        r.raise_for_status()
    except Exception as e:
        raise ConnectionError(f"Anna's Archive search failed: {e}")

    soup = BeautifulSoup(r.text, "html.parser")
    md5s = []
    for a in soup.find_all("a", href=True):
        m = re.match(r"/md5/([a-f0-9]{32})", a["href"], re.I)
        if m and m.group(1) not in md5s:
            md5s.append(m.group(1))
    return md5s


def get_download_urls(md5):
    urls = [f"{ANNA_BASE}/slow_download/{md5}/0/0"]
    try:
        r = SESSION.get(f"{ANNA_BASE}/md5/{md5}", timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if any(x in href for x in ["get.php", "ipfs.io", "cloudflare-ipfs", "library.lol"]):
                full = href if href.startswith("http") else ANNA_BASE + href
                if full not in urls:
                    urls.append(full)
    except Exception:
        pass
    return urls


def try_download(url, filepath):
    try:
        r = SESSION.get(url, timeout=90, stream=True, allow_redirects=True)
        if r.status_code != 200:
            return False
        content = b"".join(r.iter_content(8192))
        if len(content) < 10_000 or not content.startswith(b"PK"):
            return False
        filepath.write_bytes(content)
        return True
    except Exception:
        return False


def download_epub(title, author, out_dir):
    search_title = clean_title(title)
    try:
        md5s = anna_search(search_title)
    except ConnectionError as e:
        print(f"  {e}")
        return None

    if not md5s:
        print(f"  Not found on Anna's Archive: {search_title}")
        return None

    safe_name = re.sub(r"[^\w\s-]", "", search_title)[:80].strip()
    filepath = out_dir / f"{safe_name}.epub"

    for md5 in md5s[:3]:
        for url in get_download_urls(md5):
            if try_download(url, filepath):
                print(f"  Downloaded: {filepath.name}")
                return filepath
            time.sleep(1)

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

def main():
    out_dir = DOWNLOAD_DIR if CI else LOCAL_TEMP_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    if CI:
        print("Running in CI mode — downloads go to ./downloads/, tracking via downloaded.txt")
    else:
        print("Running in local mode — checking Calibre, adding downloaded books automatically")

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
        filepath = download_epub(title, author, out_dir)
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
    main()
