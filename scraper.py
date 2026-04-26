import json
import logging
import os
import re
import time
import random
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

import requests
import urllib3
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ─── CONFIG ─────────────────────────────────────────────

OUTPUT_FILE = "jobs.json"
TIMEOUT = 60
MAX_PER_CAT = 50

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
    "Referer": "https://www.google.co.in/",
}

CATEGORIES = ["vacancy", "admit", "result", "answer"]

# ─── PROXY POOL ─────────────────────────────────────────

PROXY_POOL = [
    "socks4://103.81.117.122:4153",
    "http://20.192.2.50:443",
]

def get_proxy():
    return random.choice(PROXY_POOL)

def build_proxies(proxy):
    return {"http": proxy, "https": proxy}

# ─── LOGGING ────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── SOURCES ────────────────────────────────────────────

SOURCES = [
    ("https://upsc.gov.in/", "UPSC"),
    ("https://ssc.gov.in/", "SSC"),
    ("https://uppsc.up.nic.in/", "UPPSC"),
]

_DATE_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")

# ─── PROXY TEST ─────────────────────────────────────────

def test_proxy(proxy):
    try:
        r = requests.get(
            "https://api.ipify.org",
            proxies=build_proxies(proxy),
            timeout=10,
            verify=False
        )
        log.info("[PROXY OK] %s → %s", proxy, r.text)
        return True
    except Exception as e:
        log.warning("[PROXY FAIL] %s → %s", proxy, e)
        return False

# ─── FETCH ──────────────────────────────────────────────

def fetch_page(url):
    proxy = get_proxy()
    proxies = build_proxies(proxy)

    # TEST proxy first
    if not test_proxy(proxy):
        log.warning("Skipping bad proxy: %s", proxy)
        proxies = None

    # ── STATIC ──
    try:
        log.info("Fetching: %s", url)

        resp = requests.get(
            url,
            headers=HEADERS,
            timeout=TIMEOUT,
            verify=False,
            proxies=proxies
        )
        resp.raise_for_status()

        return BeautifulSoup(resp.text, "lxml")

    except Exception as e:
        log.warning("HTTPS failed: %s", e)

    # ── HTTP FALLBACK ──
    try:
        http_url = url.replace("https://", "http://")
        log.info("Retry HTTP: %s", http_url)

        resp = requests.get(
            http_url,
            headers=HEADERS,
            timeout=TIMEOUT,
            verify=False,
            proxies=proxies
        )
        resp.raise_for_status()

        return BeautifulSoup(resp.text, "lxml")

    except Exception as e:
        log.warning("HTTP failed: %s", e)

    # ── PLAYWRIGHT ──
    try:
        log.info("Playwright fallback: %s", url)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                proxy={"server": proxy} if proxy.startswith("http") else None,
                args=["--no-sandbox"]
            )

            ctx = browser.new_context(
                ignore_https_errors=True,
                user_agent=HEADERS["User-Agent"],
                locale="en-IN",
                timezone_id="Asia/Kolkata"
            )

            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)

            html = page.content()
            browser.close()

        return BeautifulSoup(html, "lxml")

    except Exception as e:
        log.error("Playwright failed: %s", e)
        return None

# ─── CLASSIFY ───────────────────────────────────────────

def classify(title):
    t = title.lower()

    if "admit" in t:
        return "admit"
    if "answer" in t:
        return "answer"
    if "result" in t:
        return "result"

    return "vacancy"

# ─── PARSER ─────────────────────────────────────────────

def parse_notices(soup, org, base_url):
    seen = set()
    out = []

    for tag in soup.find_all("a", href=True):
        text = tag.get_text(strip=True)

        if len(text) < 15:
            continue

        link = urljoin(base_url, tag["href"])

        if link in seen:
            continue
        seen.add(link)

        cat = classify(text)

        out.append({
            "org": org,
            "title": text,
            "detailLink": link,
            "category": cat
        })

    return out

# ─── MAIN ───────────────────────────────────────────────

def main():
    log.info("=== SCRAPER START ===")

    all_items = []

    for url, org in SOURCES:
        soup = fetch_page(url)
        if not soup:
            continue

        items = parse_notices(soup, org, url)
        log.info("%s → %d items", org, len(items))
        all_items.extend(items)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_items, f, indent=2)

    log.info("Saved → %s", OUTPUT_FILE)

if __name__ == "__main__":
    main()
