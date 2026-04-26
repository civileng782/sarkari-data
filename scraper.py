import json
import logging
import os
import re
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

import requests
import urllib3
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ─── CONFIG ─────────────────────────────────────────────

OUTPUT_FILE         = "jobs.json"
PROXY_CACHE_FILE    = "proxy_cache.json"
CACHE_MAX_AGE_HOURS = 24    # Cache expires after this many hours
TIMEOUT             = 20    # S1 requests timeout (was 60 — saves 40s per failed source)
MAX_PROXY_TEST      = 300   # Max proxies to test per run (caps unbounded sequential scan)
PROXY_TEST_WORKERS  = 25    # Concurrent proxy test threads

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
    "Referer": "https://www.google.co.in/",
}

# ─── LOGGING ────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── SOURCES ────────────────────────────────────────────

SOURCES = [
    ("https://upsc.gov.in/", "UPSC"),
    ("https://ssc.gov.in/", "SSC"),
    ("https://uppsc.up.nic.in/", "UPPSC"),
]

# ─── PROXY SOURCES ─────────────────────────────────────

PROXY_SOURCES = [
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http",
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks4",
    "https://api.openproxylist.xyz/http.txt",
    "https://api.openproxylist.xyz/socks5.txt",
    "https://www.proxyscan.io/download?type=http",
    "https://www.proxyscan.io/download?type=https",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://www.proxy-list.download/api/v1/get?type=http",
    "https://www.proxy-list.download/api/v1/get?type=https",
]

# ─── NORMALIZE PROXY ────────────────────────────────────

def normalize_proxy(p):
    p = p.strip()
    if not p or ":" not in p:
        return None
    if "://" in p:
        p = p.split("://", 1)[1]
    if "@" in p:
        p = p.split("@")[-1]
    parts = p.split(":")
    if len(parts) != 2:
        return None
    ip, port = parts
    if not ip.replace(".", "").isdigit():
        return None
    try:
        port = int(port)
        if not (1 <= port <= 65535):
            return None
    except Exception:
        return None
    return f"http://{ip}:{port}"

# ─── FETCH PROXIES (fast collection, no geo check here) ─

def fetch_free_proxies():
    proxies = set()
    for url in PROXY_SOURCES:
        try:
            log.info("Fetching proxies from: %s", url)
            r = requests.get(url, timeout=10)
            if not r.text.strip():
                log.warning("Empty response from: %s", url)
                continue
            for line in r.text.splitlines():
                proxy = normalize_proxy(line)
                if proxy:
                    proxies.add(proxy)
        except Exception as e:
            log.warning("Proxy source failed (%s): %s", url, e)

    proxies = list(proxies)
    random.shuffle(proxies)
    log.info("Collected %d raw unique proxies", len(proxies))
    return proxies

# ─── PROXY POOL ─────────────────────────────────────────

PROXY_POOL     = []
WORKING_PROXIES = []   # Collects every proxy confirmed working this run → saved to cache at end

def build_proxies(proxy):
    return {"http": proxy, "https": proxy}

# ─── PROXY CACHE ─────────────────────────────────────────
#
# proxy_cache.json stores proxies confirmed working in the LAST run.
# On startup we test these first — if any still work we skip scraping
# the full proxy list entirely, saving ~30–60s of fetch + test time.
# Cache auto-expires after CACHE_MAX_AGE_HOURS so stale proxies don't
# accumulate forever.

def load_proxy_cache():
    """Return list of cached proxies if cache exists and hasn't expired."""
    try:
        with open(PROXY_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        saved_at = datetime.fromisoformat(data["saved_at"])
        age      = datetime.now(timezone.utc) - saved_at
        max_age  = timedelta(hours=CACHE_MAX_AGE_HOURS)

        if age > max_age:
            log.info("Proxy cache expired (%.1f hrs old, limit %d hrs) — will scrape fresh",
                     age.total_seconds() / 3600, CACHE_MAX_AGE_HOURS)
            return []

        proxies = data.get("proxies", [])
        log.info("Loaded %d cached proxies (%.1f hrs old)", len(proxies), age.total_seconds() / 3600)
        return proxies

    except FileNotFoundError:
        log.info("No proxy cache found — will scrape fresh")
        return []
    except Exception as e:
        log.warning("Could not read proxy cache: %s", e)
        return []


def save_proxy_cache(proxies):
    """Persist this run's confirmed-working proxies for the next run."""
    if not proxies:
        log.info("No working proxies to cache.")
        return
    try:
        data = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "count":    len(proxies),
            "proxies":  proxies,
        }
        with open(PROXY_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        log.info("Proxy cache saved → %s (%d proxies)", PROXY_CACHE_FILE, len(proxies))
    except Exception as e:
        log.warning("Failed to save proxy cache: %s", e)

# ─── TEST PROXY + VERIFY INDIAN IP (single call) ────────
#
# Uses ip-api.com through the proxy itself.
# One request does TWO things:
#   1. Proves the proxy is alive and reachable
#   2. Confirms the exit IP is in India (countryCode == "IN")
# No httpbin.org. No separate is_indian_ip() call.

def test_proxy(proxy):
    try:
        r = requests.get(
            "http://ip-api.com/json?fields=countryCode",
            proxies=build_proxies(proxy),
            timeout=6
        )
        if r.status_code == 200:
            country = r.json().get("countryCode", "")
            if country == "IN":
                log.info("[PROXY OK 🇮🇳] %s", proxy)
                # Track every confirmed-working proxy for end-of-run cache save
                if proxy not in WORKING_PROXIES:
                    WORKING_PROXIES.append(proxy)
                return True
            else:
                log.debug("[PROXY NOT IN] %s → %s", proxy, country)
    except Exception:
        pass
    return False

def get_working_proxy():
    global PROXY_POOL

    # ── Step 1: Try cached proxies from last run first ────
    cached = load_proxy_cache()
    if cached:
        log.info("Testing %d cached proxies before scraping fresh list...", len(cached))
        for proxy in cached:
            if test_proxy(proxy):
                log.info("Cache HIT — skipping proxy list scrape")
                return proxy
        log.info("All cached proxies dead — falling back to fresh scrape")

    # ── Step 2: Scrape fresh proxy list if pool empty ─────
    if not PROXY_POOL:
        log.info("Fetching fresh proxy list...")
        PROXY_POOL = fetch_free_proxies()

    # ── Step 3: Concurrent batch test (fast) ─────────────
    # Test up to MAX_PROXY_TEST proxies with PROXY_TEST_WORKERS
    # threads in parallel. Sequential testing of 10k proxies at
    # 6s each would take hours — concurrent cuts it to ~72s max.
    sample = PROXY_POOL[:MAX_PROXY_TEST]
    log.info("Concurrent-testing %d proxies (%d workers)...", len(sample), PROXY_TEST_WORKERS)

    with ThreadPoolExecutor(max_workers=PROXY_TEST_WORKERS) as executor:
        future_to_proxy = {executor.submit(test_proxy, p): p for p in sample}
        for future in as_completed(future_to_proxy):
            if future.result():
                found = future_to_proxy[future]
                # Cancel remaining pending futures (best-effort)
                for f in future_to_proxy:
                    f.cancel()
                log.info("Found working Indian proxy after concurrent scan")
                return found

    log.warning("No verified Indian proxy found in %d tested.", len(sample))
    return None

# ─── FETCH PAGE (4-STAGE WATERFALL, NO RETRY ON SUCCESS) ─
#
# Each stage returns immediately on success.
# The next stage only runs if the previous one raised an exception.
#
#   Stage 1 success → return soup  (S2, S3, S4 never run)
#   Stage 2 success → return soup  (S3, S4 never run)
#   Stage 3 success → return soup  (S4 never runs)
#   Stage 4 success → return soup
#   All fail        → return None

def fetch_page(url):

    # ── STAGE 1: Direct requests (fastest) ───────────────
    try:
        log.info("[S1] Direct fetch: %s", url)
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
        resp.raise_for_status()
        log.info("[S1] Success")
        return BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        log.warning("[S1] Failed: %s", e)

    # ── STAGE 2: Playwright / Chromium (no proxy) ─────────
    try:
        log.info("[S2] Playwright direct: %s", url)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = browser.new_context(
                ignore_https_errors=True,
                user_agent=HEADERS["User-Agent"],
                locale="en-IN",
                timezone_id="Asia/Kolkata"
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            html = page.content()
            browser.close()
        log.info("[S2] Success")
        return BeautifulSoup(html, "lxml")
    except Exception as e:
        log.warning("[S2] Failed: %s", e)

    # ── Get Indian proxy once for both S3 and S4 ──────────
    proxy = get_working_proxy()
    if not proxy:
        log.warning("No Indian proxy available — skipping S3 & S4 for: %s", url)
        return None

    # ── STAGE 3: Proxy + requests ─────────────────────────
    try:
        log.info("[S3] Proxy static: %s via %s", url, proxy)
        resp = requests.get(
            url,
            headers=HEADERS,
            timeout=TIMEOUT + 10,
            verify=False,
            proxies=build_proxies(proxy)
        )
        resp.raise_for_status()
        log.info("[S3] Success")
        return BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        log.warning("[S3] Failed: %s", e)

    # ── STAGE 4: Proxy + Playwright / Chromium ────────────
    try:
        log.info("[S4] Playwright proxy: %s via %s", url, proxy)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                proxy={"server": proxy},
                args=["--no-sandbox"]
            )
            ctx = browser.new_context(
                ignore_https_errors=True,
                user_agent=HEADERS["User-Agent"],
                locale="en-IN",
                timezone_id="Asia/Kolkata"
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            html = page.content()
            browser.close()
        log.info("[S4] Success")
        return BeautifulSoup(html, "lxml")
    except Exception as e:
        log.error("[S4] Failed: %s", e)

    log.error("All 4 stages failed for: %s", url)
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
        out.append({
            "org": org,
            "title": text,
            "detailLink": link,
            "category": classify(text)
        })
    return out

# ─── MAIN ───────────────────────────────────────────────

def main():
    log.info("=== SCRAPER START ===")

    # ── Load existing jobs.json to merge into ────────────
    existing_items = []
    existing_links = set()
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                existing_items = json.load(f)
            existing_links = {item["detailLink"] for item in existing_items}
            log.info("Loaded %d existing items from %s", len(existing_items), OUTPUT_FILE)
        except Exception as e:
            log.warning("Could not read existing %s (starting fresh): %s", OUTPUT_FILE, e)

    # ── Scrape new items ──────────────────────────────────
    new_items = []
    seen_links = set()

    for url, org in SOURCES:
        soup = fetch_page(url)
        if not soup:
            log.warning("Skipping %s — all fetch stages failed", org)
            continue

        items = parse_notices(soup, org, url)

        added = 0
        for item in items:
            if item["detailLink"] not in seen_links:
                seen_links.add(item["detailLink"])
                new_items.append(item)
                added += 1

        log.info("%s → %d items (%d unique this run)", org, len(items), added)

    # ── Merge: new items first, then existing not in new ─
    # New items take priority (fresher data). Existing items
    # that weren't re-scraped this run are preserved as-is.
    merged_links = {item["detailLink"] for item in new_items}
    preserved    = [item for item in existing_items if item["detailLink"] not in merged_links]
    all_items    = new_items + preserved

    log.info("Merge: %d new + %d preserved = %d total", len(new_items), len(preserved), len(all_items))

    # ── Write merged output ───────────────────────────────
    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(all_items, f, indent=2, ensure_ascii=False)
        log.info("Saved %d total items → %s", len(all_items), OUTPUT_FILE)
    except Exception as e:
        log.error("Failed to write output: %s", e)

    # ── Save working proxies for next run ─────────────────
    save_proxy_cache(WORKING_PROXIES)
    log.info("=== SCRAPER DONE ===")

if __name__ == "__main__":
    main()
