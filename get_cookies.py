"""
get_cookies.py — run this locally whenever GOODREADS_COOKIES expires.

Requirements (local only, not needed in CI):
    pip install playwright
    playwright install chromium

Usage:
    python get_cookies.py

A Chromium window will open. Log in to Goodreads normally, then come back
here and press Enter. The script prints the cookie string to paste into
your GOODREADS_COOKIES GitHub secret.
"""

from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://www.goodreads.com/user/sign_in")

        print("Browser opened. Log in to Goodreads, then press Enter here.")
        input()

        cookies = context.cookies("https://www.goodreads.com")
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

        print("\nCopy the value below as your GOODREADS_COOKIES GitHub secret:")
        print("-" * 60)
        print(cookie_str)
        print("-" * 60)

        browser.close()


if __name__ == "__main__":
    main()
