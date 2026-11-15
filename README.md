# epub-goodreads-sync

Automatically downloads missing books from your Goodreads "want to read" shelf as EPUBs via Z-Library, then uploads them to Google Drive/Incoming for Calibre to pick up. Runs every Sunday at 8am UTC via GitHub Actions.

---

## Monthly: Refresh Goodreads Cookies

Goodreads session cookies expire periodically. When the pipeline fails with:
> `ERROR: Goodreads cookies expired. Run get_cookies.py and update the GOODREADS_COOKIES secret.`

Run this locally:

```bash
python get_cookies.py
```

A Chromium window opens. Log in to Goodreads, come back to the terminal, press Enter. Copy the printed string and paste it as the `GOODREADS_COOKIES` secret in GitHub:

> Repo → Settings → Secrets and variables → Actions → `GOODREADS_COOKIES` → Update

Done. Trigger a manual run to verify: Actions → Sync Books → Run workflow.

---

## First-Time Setup

### 1. Prerequisites

- Python 3.12+
- Calibre installed (for local mode)
- A Z-Library account
- A Google account with Google Drive

### 2. Clone and install

```bash
git clone https://github.com/JustinLycklama/epub-goodreads-sync.git
cd epub-goodreads-sync
pip install -r requirements.txt
```

### 3. GitHub Secrets

Add these in repo → Settings → Secrets and variables → Actions:

| Secret | Description |
|---|---|
| `ZLIBRARY_EMAIL` | Z-Library account email |
| `ZLIBRARY_PASSWORD` | Z-Library account password |
| `GDRIVE_CLIENT_ID` | Google OAuth client ID |
| `GDRIVE_CLIENT_SECRET` | Google OAuth client secret |
| `GDRIVE_REFRESH_TOKEN` | Google OAuth refresh token (see below) |
| `GOODREADS_COOKIES` | Goodreads session cookies (see below) |

### 4. Google Drive OAuth token

```bash
python get_token.py
```

Follow the console prompts. Copy the printed `REFRESH TOKEN`, `CLIENT ID`, and `CLIENT SECRET` into the corresponding secrets above.

The OAuth consent screen must be in **Production** mode (not Testing) or the token expires after 7 days. Set this in GCP Console → APIs & Services → OAuth consent screen → Audience → Publish App.

### 5. Goodreads cookies

Install Playwright locally (only needed for this step, not in CI):

```bash
pip install playwright
playwright install chromium
```

Then:

```bash
python get_cookies.py
```

Log in in the browser window, press Enter, paste the output as the `GOODREADS_COOKIES` secret.

---

## How It Works

1. Fetches your Goodreads "want to read" shelf using session cookies
2. Checks `downloaded.txt` to skip books already fetched
3. Searches Z-Library for each missing book (EPUB, English only)
4. Matches by title and author last name — skips if no confident match
5. Uploads matched EPUBs to `Google Drive/Incoming`
6. Updates `downloaded.txt` and `needs_review.txt`, commits them back to the repo

Books that couldn't be matched are logged in `needs_review.txt` with the reason.

---

## Local Mode (recommended)

Runs against your local Calibre library instead of Google Drive. Uses your residential IP, so Z-Library search is not blocked.

**Setup:**

Copy `.env.example` to `.env` and fill in your credentials:

```
ZLIBRARY_EMAIL=your@email.com
ZLIBRARY_PASSWORD=yourpassword
GOODREADS_COOKIES=paste_cookie_string_from_get_cookies.py
```

Run manually:

```bash
python book-sync-zlib.py
```

**Schedule with Task Scheduler (weekly):**

1. Open Task Scheduler → Create Basic Task
2. Trigger: Weekly, Sunday, 8:00 AM
3. Action: Start a program
   - Program: `C:\path\to\python.exe`
   - Arguments: `book-sync-zlib.py`
   - Start in: `C:\Users\Justin\Development\epub-goodreads-sync`
4. In Properties → General: check **Run whether user is logged on or not**

The `.env` file is loaded automatically — no need to set environment variables in Windows.
