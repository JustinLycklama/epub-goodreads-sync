"""
book-sync-zlib.py

Downloads missing Goodreads "want to read" books as ePubs via Z-Library's
mobile JSON API (/eapi/) — no HTML scraping, no antibot issues.

Modes:
  Local:  Checks Calibre library (calibredb) to determine what's missing,
          adds downloads directly to Calibre.
  CI:     Checks Google Drive (Incoming + Calibre Library folders) to determine
          what's missing. Downloads and uploads new epubs to Drive/Incoming.
          Credentials from env vars:
            ZLIBRARY_EMAIL, ZLIBRARY_PASSWORD, GDRIVE_SERVICE_ACCOUNT (JSON)

Dependencies:
    pip install feedparser requests beautifulsoup4 google-api-python-client google-auth
"""

import requests
import subprocess
import json
import os
import re
import sys
import io
from pathlib import Path

# Load .env if present (local development)
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# --- Config ---
GOODREADS_USER_ID = "197955244"
CALIBRE_LIBRARY = r"G:\My Drive\Books\Calibre Library"
CALIBREDB = r"C:\Program Files\Calibre2\calibredb.exe"

CI = os.getenv("CI", "false").lower() == "true"
REPO_ROOT = Path(__file__).parent
DOWNLOADED_FILE = REPO_ROOT / "downloaded.txt"
REVIEW_FILE = REPO_ROOT / "needs_review.txt"
LOCAL_TEMP_DIR = Path(r"C:\Users\Justin\Documents\callibre-temp")

GDRIVE_INCOMING_NAME = "Incoming"

ZLIB_MIRRORS = [
    "https://z-lib.sk",
    "https://z-lib.fm",
    "https://1lib.sk",
    "https://z-library.se",
    "https://singlelogin.re",
    "https://zlibrary.to",
    "https://lib.to",
]
ZLIB_BASE = None  # resolved at runtime
HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.cookies.set("siteLanguageV2", "en")


# --- Helpers ---

def normalize(text):
    text = text.lower()
    text = re.split(r"[:(]", text)[0]
    text = re.sub(r"[^\w\s]", "", text)
    return text.strip()


def clean_title(title):
    """Strip Goodreads series annotation e.g. '(Mistborn, #5)'"""
    return re.sub(r"\s*\(.*?\)\s*$", "", title).strip()


# --- Goodreads ---

def get_goodreads_books():
    from bs4 import BeautifulSoup

    cookie_str = os.getenv("GOODREADS_COOKIES")
    if not cookie_str:
        print("ERROR: GOODREADS_COOKIES must be set. Run get_cookies.py locally to generate it.")
        sys.exit(1)

    gr_session = requests.Session()
    gr_session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    })
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            gr_session.cookies.set(name.strip(), value.strip(), domain=".goodreads.com")

    print("Fetching Goodreads want-to-read shelf...")
    books = []
    page_num = 1
    while True:
        url = (
            f"https://www.goodreads.com/review/list/{GOODREADS_USER_ID}"
            f"?shelf=to-read&per_page=100&page={page_num}"
        )
        r = gr_session.get(url, timeout=20)
        r.raise_for_status()

        if "/user/sign_in" in r.url:
            print("ERROR: Goodreads cookies expired. Run get_cookies.py and update the GOODREADS_COOKIES secret.")
            sys.exit(1)

        soup = BeautifulSoup(r.text, "html.parser")
        rows = soup.select("div.bookalike.review")
        if not rows:
            break

        for row in rows:
            title_el = row.select_one(".field.title a")
            author_el = row.select_one(".field.author .value")
            if title_el:
                title = (title_el.get("title") or title_el.get_text()).strip()
                author = author_el.get_text(strip=True) if author_el else ""
                books.append({"title": title, "author": author})

        if len(rows) < 100:
            break
        page_num += 1

    print(f"  {len(books)} books on shelf")
    return books


# --- Existing book detection ---

def get_existing_books_downloaded_txt():
    """Check downloaded.txt for already-fetched books (CI mode)."""
    print("Reading downloaded.txt...")
    if not DOWNLOADED_FILE.exists():
        print("  (none yet)")
        return set()
    titles = {line.strip() for line in DOWNLOADED_FILE.read_text().splitlines() if line.strip()}
    print(f"  {len(titles)} books already downloaded")
    return titles


def get_existing_books_local():
    """Check Calibre library via SQLite (works while GUI is open)."""
    import sqlite3
    db_path = Path(CALIBRE_LIBRARY) / "metadata.db"
    if not db_path.exists():
        print(f"ERROR: Calibre metadata.db not found at {db_path}")
        sys.exit(1)
    print("Reading Calibre library...")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute("SELECT title FROM books").fetchall()
    con.close()
    titles = {normalize(r[0]) for r in rows}
    print(f"  {len(titles)} books in Calibre")
    return titles


# --- Google Drive ---

def get_drive_service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    client_id = os.getenv("GDRIVE_CLIENT_ID")
    client_secret = os.getenv("GDRIVE_CLIENT_SECRET")
    refresh_token = os.getenv("GDRIVE_REFRESH_TOKEN")
    if not all([client_id, client_secret, refresh_token]):
        print("ERROR: GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET, and GDRIVE_REFRESH_TOKEN must all be set.")
        sys.exit(1)
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("drive", "v3", credentials=creds)


def find_incoming_folder(drive_service):
    """Return the Drive folder ID for the Incoming folder."""
    resp = drive_service.files().list(
        q=f"name='{GDRIVE_INCOMING_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id, name)",
    ).execute()
    files = resp.get("files", [])
    if not files:
        print(f"ERROR: Could not find '{GDRIVE_INCOMING_NAME}' folder in Drive.")
        sys.exit(1)
    return files[0]["id"]


def upload_to_drive(drive_service, folder_id, filepath):
    from googleapiclient.http import MediaIoBaseUpload

    file_metadata = {
        "name": filepath.name,
        "parents": [folder_id],
    }
    try:
        with open(filepath, "rb") as f:
            media = MediaIoBaseUpload(f, mimetype="application/epub+zip", resumable=True)
            drive_service.files().create(
                body=file_metadata, media_body=media, fields="id"
            ).execute()
        print(f"  Uploaded to Drive/Incoming: {filepath.name}")
        return True
    except Exception as e:
        print(f"  Drive upload failed: {e}")
        return False


# --- Z-Library eapi ---

def zlib_login(base, email, password):
    """Attempt login on a specific mirror. Returns downloads_left or None on failure."""
    try:
        r = SESSION.post(f"{base}/eapi/user/login",
                         data={"email": email, "password": password}, timeout=20)
        resp = r.json()
    except Exception:
        return None

    if not resp.get("success"):
        return None

    user = resp["user"]
    SESSION.cookies.set("remix_userid", str(user["id"]))
    SESSION.cookies.set("remix_userkey", user["remix_userkey"])

    downloads_left = user.get("downloads_limit", 10) - user.get("downloads_today", 0)
    print(f"  Logged in as {user['email']}. Downloads remaining today: {downloads_left}")
    return downloads_left


def resolve_mirror_and_login():
    """Find a working mirror (login + search both succeed). Returns (base, downloads_left) or exits."""
    email = os.getenv("ZLIBRARY_EMAIL")
    password = os.getenv("ZLIBRARY_PASSWORD")
    if not email or not password:
        print("ERROR: ZLIBRARY_EMAIL and ZLIBRARY_PASSWORD must be set.")
        sys.exit(1)

    for mirror in ZLIB_MIRRORS:
        print(f"  Trying {mirror}...")
        downloads_left = zlib_login(mirror, email, password)
        if downloads_left is None:
            continue
        # Verify search also works on this mirror (login may succeed but search can be blocked)
        try:
            r = SESSION.post(f"{mirror}/eapi/book/search", data={
                "message": "test", "extensions[]": "epub", "limit": 1,
            }, timeout=20)
            r.json()  # will raise if response is not JSON
            print(f"  Using mirror: {mirror}")
            return mirror, downloads_left
        except Exception:
            print(f"  {mirror} login OK but search blocked (status {r.status_code}), trying next...")
            continue

    print("ERROR: Could not find a Z-Library mirror with working login and search.")
    sys.exit(1)


def zlib_search(query, base):
    r = SESSION.post(f"{base}/eapi/book/search", data={
        "message": query,
        "extensions[]": "epub",
        "languages[]": "english",
        "limit": 20,
    }, timeout=20)
    try:
        resp = r.json()
    except Exception:
        return []
    return resp.get("books", [])


def zlib_download(book, out_dir, base):
    book_id = book["id"]
    hash_id = book["hash"]
    r = SESSION.get(f"{base}/eapi/book/{book_id}/{hash_id}/file", timeout=20)
    resp = r.json()

    file_info = resp.get("file")
    if not file_info:
        return None

    dl_url = file_info.get("downloadLink")
    if not dl_url:
        return None

    res = SESSION.get(dl_url, timeout=90, stream=True)
    if res.status_code != 200:
        return None

    content = b"".join(res.iter_content(8192))
    if len(content) < 10_000 or not content.startswith(b"PK"):
        return None

    title = file_info.get("description", book.get("title", "book"))
    safe_name = re.sub(r"[^\w\s-]", "", title)[:80].strip()
    filepath = out_dir / f"{safe_name}.epub"
    filepath.write_bytes(content)
    return filepath


DERIVATIVE_WORDS = {"summary", "analysis", "guide", "review", "synopsis", "overview", "workbook", "study"}

def title_matches(search_title, result_title):
    """Return True if result_title is a plausible match for search_title."""
    s = normalize(search_title)
    r = normalize(result_title)
    if s == r:
        return True
    search_words = set(s.split())
    result_words = set(r.split())
    if not search_words:
        return False
    # Reject derivative works unless the search title itself is one
    if result_words & DERIVATIVE_WORDS and not (search_words & DERIVATIVE_WORDS):
        return False
    if search_words.issubset(result_words):
        return True
    union = search_words | result_words
    if len(union) > 0 and len(search_words & result_words) / len(union) >= 0.7:
        return True
    return False


def extract_last_name(author):
    """Extract last name, handling both 'First Last' and 'Last, First' formats."""
    author = author.strip()
    if "," in author:
        return normalize(author.split(",")[0])
    parts = author.strip().split()
    return normalize(parts[-1]) if parts else ""


def author_matches(search_author, result_author):
    """Return True if last names match."""
    if not search_author or not result_author:
        return False
    return bool(extract_last_name(search_author) == extract_last_name(result_author))


def download_epub(title, author, out_dir, base):
    search_title = clean_title(title)
    try:
        results = zlib_search(search_title, base)
    except Exception as e:
        print(f"  Search error: {e}")
        return None

    if not results:
        print(f"  Not found on Z-Library: {search_title}")
        return None

    title_matched = [b for b in results if title_matches(search_title, b.get("title", ""))]
    if not title_matched:
        best = results[0]
        reason = f"no title match (top result: \"{best.get('title', '?')}\" by {best.get('author', '?')})"
        print(f"  Skipped — {reason}")
        mark_for_review(title, author, reason)
        return None

    # Exact title matches first (API ranking isn't reliable)
    title_matched.sort(key=lambda b: normalize(b.get("title", "")) == normalize(search_title), reverse=True)

    author_confirmed = [b for b in title_matched if author_matches(author, b.get("author", ""))]

    if not author_confirmed:
        reason = f"title matched but no author match (expected: {author})"
        print(f"  Skipped — {reason}")
        mark_for_review(title, author, reason)
        return None

    for book in author_confirmed:
        try:
            filepath = zlib_download(book, out_dir, base)
            if filepath:
                print(f"  Downloaded: {filepath.name}")
                return filepath
        except Exception:
            continue

    mark_for_review(title, author, "download failed (all attempts)")
    print(f"  All download attempts failed: {search_title}")
    return None


# --- Local delivery ---

def deliver_local(filepath):
    """Copy epub into Drive/Incoming for Calibre auto-add."""
    import shutil
    incoming = Path(CALIBRE_LIBRARY).parent / "Incoming"
    incoming.mkdir(exist_ok=True)
    dest = incoming / filepath.name
    shutil.move(str(filepath), dest)
    print(f"  Moved to Incoming: {filepath.name}")
    return True


# --- Main ---

def mark_downloaded(title):
    with DOWNLOADED_FILE.open("a") as f:
        f.write(normalize(clean_title(title)) + "\n")


def mark_for_review(title, author, reason):
    """Append to needs_review.txt if not already present (deduped by title)."""
    entry = f"{clean_title(title)} | {author} | {reason}"
    if REVIEW_FILE.exists():
        existing = REVIEW_FILE.read_text().splitlines()
        # Dedup by title+author prefix
        key = f"{clean_title(title)} | {author}"
        if any(line.startswith(key) for line in existing):
            return
    with REVIEW_FILE.open("a") as f:
        f.write(entry + "\n")


def main():
    if CI:
        print("Running in CI mode — uploading to Google Drive/Incoming")
        drive_service = get_drive_service()
        incoming_folder_id = find_incoming_folder(drive_service)
        existing = get_existing_books_downloaded_txt()
        out_dir = REPO_ROOT / "downloads"
    else:
        print("Running in local mode — reading Calibre library, dropping EPUBs into Incoming")
        drive_service = None
        incoming_folder_id = None
        existing = get_existing_books_local()
        out_dir = LOCAL_TEMP_DIR

    out_dir.mkdir(parents=True, exist_ok=True)

    print("\nLogging in to Z-Library...")
    zlib_base, downloads_left = resolve_mirror_and_login()

    gr_books = get_goodreads_books()

    missing = [b for b in gr_books if normalize(clean_title(b["title"])) not in existing]

    if not missing:
        print("\nAll shelf books are already downloaded.")
        return

    if CI and downloads_left < len(missing):
        print(f"\nNote: {len(missing)} books to download but only {downloads_left} downloads left today.")
        print("Will download as many as possible; re-run tomorrow for the rest.\n")

    print(f"\n{len(missing)} book(s) to download:")
    for b in missing:
        print(f"  - {b['title']} ({b['author']})")

    print()
    skipped = []
    for book in missing:
        title, author = book["title"], book["author"]
        print(f"[{title}]")
        filepath = download_epub(title, author, out_dir, zlib_base)
        if filepath:
            if CI:
                if upload_to_drive(drive_service, incoming_folder_id, filepath):
                    filepath.unlink()
                    mark_downloaded(title)
                else:
                    skipped.append(title)
            else:
                if deliver_local(filepath):
                    mark_downloaded(title)
                else:
                    skipped.append(title)
        else:
            skipped.append(title)

    print("\nDone.")
    if skipped:
        print(f"\nNot found / failed ({len(skipped)}):")
        for t in skipped:
            print(f"  - {t}")


if __name__ == "__main__":
    main()
