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

import feedparser
import requests
import subprocess
import json
import os
import re
import sys
import io
from pathlib import Path

# --- Config ---
GOODREADS_USER_ID = "197955244"
CALIBRE_LIBRARY = r"C:\Users\Justin\Google Drive\Books\Calibre Library"
CALIBREDB = r"C:\Program Files\Calibre2\calibredb.exe"

CI = os.getenv("CI", "false").lower() == "true"
REPO_ROOT = Path(__file__).parent
DOWNLOADED_FILE = REPO_ROOT / "downloaded.txt"
LOCAL_TEMP_DIR = Path(r"C:\Users\Justin\Documents\callibre-temp")

GDRIVE_INCOMING_NAME = "Incoming"

RSS_URL = f"https://www.goodreads.com/review/list_rss/{GOODREADS_USER_ID}?shelf=to-read"

ZLIB_DOMAIN = "1lib.sk"
ZLIB_BASE = f"https://{ZLIB_DOMAIN}"
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
    """Check Calibre library via calibredb (local mode)."""
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


# --- Google Drive ---

def get_drive_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    sa_json = os.getenv("GDRIVE_SERVICE_ACCOUNT")
    if not sa_json:
        print("ERROR: GDRIVE_SERVICE_ACCOUNT env var not set.")
        sys.exit(1)
    info = json.loads(sa_json)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive"]
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

def zlib_login():
    email = os.getenv("ZLIBRARY_EMAIL")
    password = os.getenv("ZLIBRARY_PASSWORD")
    if not email or not password:
        print("ERROR: ZLIBRARY_EMAIL and ZLIBRARY_PASSWORD must be set.")
        sys.exit(1)

    r = SESSION.post(f"{ZLIB_BASE}/eapi/user/login",
                     data={"email": email, "password": password}, timeout=20)
    resp = r.json()
    if not resp.get("success"):
        print(f"ERROR: Z-Library login failed: {resp.get('error', resp)}")
        sys.exit(1)

    user = resp["user"]
    SESSION.cookies.set("remix_userid", str(user["id"]))
    SESSION.cookies.set("remix_userkey", user["remix_userkey"])

    downloads_left = user.get("downloads_limit", 10) - user.get("downloads_today", 0)
    print(f"  Logged in as {user['email']}. Downloads remaining today: {downloads_left}")
    return downloads_left


def zlib_search(query):
    r = SESSION.post(f"{ZLIB_BASE}/eapi/book/search", data={
        "message": query,
        "extensions[]": "epub",
        "languages[]": "english",
        "limit": 5,
    }, timeout=20)
    resp = r.json()
    return resp.get("books", [])


def zlib_download(book, out_dir):
    book_id = book["id"]
    hash_id = book["hash"]
    r = SESSION.get(f"{ZLIB_BASE}/eapi/book/{book_id}/{hash_id}/file", timeout=20)
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


def download_epub(title, out_dir):
    search_title = clean_title(title)
    try:
        results = zlib_search(search_title)
    except Exception as e:
        print(f"  Search error: {e}")
        return None

    if not results:
        print(f"  Not found on Z-Library: {search_title}")
        return None

    for book in results:
        try:
            filepath = zlib_download(book, out_dir)
            if filepath:
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

def mark_downloaded(title):
    with DOWNLOADED_FILE.open("a") as f:
        f.write(normalize(clean_title(title)) + "\n")


def main():
    if CI:
        print("Running in CI mode — uploading to Google Drive/Incoming")
        drive_service = get_drive_service()
        incoming_folder_id = find_incoming_folder(drive_service)
        existing = get_existing_books_downloaded_txt()
        out_dir = REPO_ROOT / "downloads"
    else:
        print("Running in local mode — checking Calibre, adding downloaded books automatically")
        drive_service = None
        incoming_folder_id = None
        existing = get_existing_books_local()
        out_dir = LOCAL_TEMP_DIR

    out_dir.mkdir(parents=True, exist_ok=True)

    print("\nLogging in to Z-Library...")
    downloads_left = zlib_login()

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
        filepath = download_epub(title, out_dir)
        if filepath:
            if CI:
                if upload_to_drive(drive_service, incoming_folder_id, filepath):
                    filepath.unlink()
                    mark_downloaded(title)
                else:
                    skipped.append(title)
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
