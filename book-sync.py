"""
book-sync.py

Fetches the Goodreads "want to read" shelf via RSS, compares against the
local Calibre library, and downloads missing books as ePubs from LibGen.

Dependencies:
    pip install feedparser requests beautifulsoup4
"""

import feedparser
import requests
import subprocess
import json
import re
import sys
from pathlib import Path
from bs4 import BeautifulSoup

GOODREADS_USER_ID = "197955244"
CALIBRE_LIBRARY = r"C:\Users\Justin\Google Drive\Books\Calibre Library"
TEMP_DIR = Path(r"C:\Users\Justin\Documents\callibre-temp")
CALIBREDB = r"C:\Program Files\Calibre2\calibredb.exe"

RSS_URL = f"https://www.goodreads.com/review/list_rss/{GOODREADS_USER_ID}?shelf=to-read"


# Mirrors that support the /fiction/ endpoint
LIBGEN_FICTION_MIRRORS = [
    "https://libgen.rs",
    "https://libgen.st",
]

# Fallback: general search index (catches non-fiction, misclassified books)
LIBGEN_SEARCH_MIRRORS = [
    "https://libgen.rs",
    "https://libgen.st",
    "https://libgen.li",
]

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "Mozilla/5.0"


def normalize(text):
    text = text.lower()
    text = re.split(r"[:(]", text)[0]  # strip subtitles and series info
    text = re.sub(r"[^\w\s]", "", text)
    return text.strip()


def clean_title(title):
    """Strip Goodreads series annotation e.g. 'Yumi and the Nightmare Painter (Hoid's Travails, #2)'"""
    return re.sub(r"\s*\(.*?\)\s*$", "", title).strip()


def get_goodreads_books():
    print("Fetching Goodreads want-to-read shelf...")
    feed = feedparser.parse(RSS_URL)
    if feed.bozo and not feed.entries:
        print("ERROR: Could not fetch Goodreads RSS. Is your shelf set to public?")
        sys.exit(1)
    books = []
    for entry in feed.entries:
        title = entry.get("title", "").strip()
        # Goodreads puts author in author_name or falls back to author
        author = (
            entry.get("author_name")
            or entry.get("author")
            or ""
        ).strip()
        if title:
            books.append({"title": title, "author": author})
    print(f"  {len(books)} books on shelf")
    return books


def get_calibre_books():
    print("Reading Calibre library...")
    result = subprocess.run(
        [CALIBREDB, "list", "--fields", "title,authors", "--for-machine",
         "--library-path", CALIBRE_LIBRARY],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: calibredb failed: {result.stderr}")
        sys.exit(1)
    data = json.loads(result.stdout)
    titles = {normalize(book["title"]) for book in data}
    print(f"  {len(titles)} books in Calibre")
    return titles


def search_fiction(query, mirror):
    """Search the /fiction/ index. Raises ConnectionError if mirror unreachable."""
    url = f"{mirror}/fiction/"
    params = {"q": query, "criteria": "title", "language": "English", "format": "epub"}
    try:
        r = SESSION.get(url, params=params, timeout=15)
        r.raise_for_status()
    except Exception as e:
        raise ConnectionError(f"{mirror}: {e}")

    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    for row in soup.select("table.catalog tbody tr"):
        cols = row.find_all("td")
        if len(cols) < 4:
            continue
        for a in cols[2].find_all("a"):
            m = re.search(r"/fiction/([a-f0-9]{32})", a.get("href", ""), re.I)
            if m:
                results.append({"md5": m.group(1), "mirror": mirror, "type": "fiction"})
                break
    return results


def search_general(query, mirror):
    """Search the general /search.php index as a fallback. Raises ConnectionError if unreachable."""
    url = f"{mirror}/search.php"
    params = {"req": query, "ext": "epub", "res": 25, "phrase": 1, "column": "title"}
    try:
        r = SESSION.get(url, params=params, timeout=15)
        r.raise_for_status()
    except Exception as e:
        raise ConnectionError(f"{mirror}: {e}")

    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    for row in soup.select("table#searchResultTable tbody tr, table.c tbody tr"):
        for a in row.find_all("a", href=True):
            m = re.search(r"md5=([a-f0-9]{32})", a["href"], re.I)
            if m:
                results.append({"md5": m.group(1), "mirror": mirror, "type": "general"})
                break
    return results


def resolve_download_url(result):
    """Resolve a search result to a direct epub download URL."""
    mirror, md5, kind = result["mirror"], result["md5"], result["type"]
    if kind == "fiction":
        page_url = f"{mirror}/fiction/{md5}"
    else:
        page_url = f"{mirror}/ads.php?md5={md5}"
    try:
        r = SESSION.get(page_url, timeout=15)
        r.raise_for_status()
    except Exception:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.endswith(".epub") or "get.php" in href or ("md5=" in href.lower() and "ads" not in href):
            return href if href.startswith("http") else mirror + href
    return None


def download_epub(title, author):
    search_title = clean_title(title)
    results = []
    last_error = None

    # Try fiction index first (better for novels)
    for mirror in LIBGEN_FICTION_MIRRORS:
        try:
            results = search_fiction(search_title, mirror)
            if results:
                break
        except ConnectionError as e:
            last_error = e

    # Fall back to general search if fiction index found nothing or was unreachable
    if not results:
        for mirror in LIBGEN_SEARCH_MIRRORS:
            try:
                results = search_general(search_title, mirror)
                if results:
                    break
            except ConnectionError as e:
                last_error = e

    if not results:
        if last_error:
            print(f"  Could not reach any LibGen mirror: {last_error}")
        else:
            print(f"  Not found on LibGen: {search_title}")
        return None

    for result in results[:3]:
        dl_url = resolve_download_url(result)
        if not dl_url:
            continue
        try:
            r = SESSION.get(dl_url, timeout=60, stream=True)
            if r.status_code == 200 and len(r.content) > 10_000:
                safe_name = re.sub(r'[^\w\s-]', '', search_title)[:80].strip()
                filepath = TEMP_DIR / f"{safe_name}.epub"
                with open(filepath, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
                print(f"  Downloaded: {filepath.name}")
                return filepath
        except Exception:
            continue

    print(f"  All download links failed: {search_title}")
    return None


def add_to_calibre(filepath):
    result = subprocess.run(
        [CALIBREDB, "add", str(filepath), "--library-path", CALIBRE_LIBRARY],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"  Added to Calibre")
        filepath.unlink()
        return True
    else:
        print(f"  calibredb add failed: {result.stderr.strip()}")
        return False


def main():
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    gr_books = get_goodreads_books()
    calibre_titles = get_calibre_books()

    missing = [b for b in gr_books if normalize(clean_title(b["title"])) not in calibre_titles]

    if not missing:
        print("\nAll shelf books are already in Calibre.")
        return

    print(f"\n{len(missing)} book(s) to download:")
    for b in missing:
        print(f"  - {b['title']} ({b['author']})")

    print()
    skipped = []
    for book in missing:
        title, author = book["title"], book["author"]
        print(f"[{title}]")
        filepath = download_epub(title, author)
        if filepath:
            add_to_calibre(filepath)
        else:
            skipped.append(title)

    print("\nDone.")
    if skipped:
        print(f"\nNot found on LibGen ({len(skipped)}):")
        for t in skipped:
            print(f"  - {t}")


if __name__ == "__main__":
    main()
