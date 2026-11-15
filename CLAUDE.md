# epub-goodreads-sync

Automated pipeline: Goodreads "want to read" shelf → Z-Library download → Calibre library.

## Architecture

- **book-sync-zlib.py** — main script. Two modes: local (reads Calibre SQLite, drops EPUBs into Drive/Incoming) and CI (uses Google Drive API). CI mode is currently broken due to Z-Library blocking GitHub Actions IPs.
- **get_cookies.py** — local helper to export Goodreads session cookies via Playwright. Run when GOODREADS_COOKIES expires.
- **get_token.py** — one-time helper to generate Google OAuth refresh token for CI mode.

## Z-Library

Uses the `/eapi/` mobile JSON API. No scraping. Login → search → download.

**Known issue:** Z-Library mirrors periodically block search with DiamWall bot challenges (HTTP 513). When this happens, check https://github.com/heartleo/zlib/issues for currently working mirrors and update `ZLIB_MIRRORS` in the script. `z-library.ec` was the fix as of late 2026.

## Goodreads

Fetches the want-to-read shelf using session cookies (GOODREADS_COOKIES). The shelf requires login; the login page is a JS SPA that blocks headless browsers from CI IPs. Session cookies from a real browser session are injected into requests instead.

Cookies expire periodically. Run `python get_cookies.py` locally to refresh, then update the GitHub secret.

## Matching logic

- Title normalized: lowercase, strip subtitles/series, strip punctuation
- Author matched by last name only (avoids false positives like "John Stuart Mill" matching "Douglas Stuart")
- Title-only matches are hard-skipped — both title AND author must match
- Skipped books logged to `needs_review.txt`

## Local setup

Credentials go in `.env` (gitignored). See `.env.example`.

Calibre library path: `G:\My Drive\Books\Calibre Library`
