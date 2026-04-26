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

# ─── GEO CACHE ──────────────────────────────────────────

GEO_CACHE = {}

def is_indian_ip(ip):
    if ip in GEO_CACHE:
        return GEO_CACHE[ip]

    try:
        r = requests.get(
            f"http://ip-api.com/json/{ip}?fields=countryCode",
            timeout=5
        )
        result = r.json().get("countryCode") == "IN"
        GEO_CACHE[ip] = result
        return result
    except:
        GEO_CACHE[ip] = False
        return False

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
    except:
        return None

    return f"http://{ip}:{port}"

# ─── FETCH PROXIES ─────────────────────────────────────

def fetch_free_proxies():
    proxies = set()

    for url in PROXY_SOURCES:
        try:
            log.info("Fetching proxies from: %s", url)
            r = requests.get(url, timeout=10)

            if not r.text.strip():
                log.warning("Empty proxy list from source")
                continue

            for line in r.text.splitlines():
                proxy = normalize_proxy(line)
                if not proxy:
                    continue

                ip = proxy.split("://")[1].split(":")[0]

                if not is_indian_ip(ip):
                    continue

                proxies.add(proxy)

        except Exception as e:
            log.warning("Proxy source failed: %s", e)

    proxies = list(proxies)
    random.shuffle(proxies)

    log.info("Total UNIQUE Indian proxies: %d", len(proxies))
    return proxies

# ─── PROXY POOL ─────────────────────────────────────────

PROXY_POOL = []

def build_proxies(proxy):
    return {"http": proxy, "https": proxy}

def test_proxy(proxy):
    try:
        r = requests.get(
            "http://httpbin.org/ip",
            proxies=build_proxies(proxy),
            timeout=6
        )
        if r.status_code == 200:
            log.info("[PROXY OK] %s", proxy)
            return True
    except:
        pass

    log.warning("[PROXY FAIL] %s", proxy)
    return False

def get_working_proxy():
    global PROXY_POOL

    if not PROXY_POOL:
        log.info("Fetching fresh proxy list...")
        PROXY_POOL = fetch_free_proxies()

    for proxy in PROXY_POOL:
        if test_proxy(proxy):
            return proxy

    if PROXY_POOL:
        fallback = random.choice(PROXY_POOL)
        log.warning("Using fallback proxy: %s", fallback)
        return fallback

    return None

# ─── FETCH PAGE (FULL 4-STAGE FLOW) ─────────────────────

def fetch_page(url):

    # STEP 1: DIRECT
    try:
        log.info("Direct fetch: %s", url)
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        log.warning("Direct failed: %s", e)

    # STEP 2: PLAYWRIGHT DIRECT
    try:
        log.info("Playwright direct: %s", url)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
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
        log.warning("Playwright direct failed: %s", e)

    # STEP 3: PROXY STATIC
    proxy = get_working_proxy()
    proxies = build_proxies(proxy) if proxy else None

    log.info("Using proxy: %s", proxy)

    try:
        resp = requests.get(
            url,
            headers=HEADERS,
            timeout=TIMEOUT + 10,
            verify=False,
            proxies=proxies
        )
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        log.warning("Proxy static failed: %s", e)

    # STEP 4: PLAYWRIGHT + PROXY
    try:
        log.info("Playwright proxy: %s", proxy)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                proxy={"server": proxy} if proxy else None,
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
        log.error("Playwright proxy failed: %s", e)

    return None

# ─── PARSER ─────────────────────────────────────────────

def classify(title):
    t = title.lower()
    if "admit" in t:
        return "admit"
    if "answer" in t:
        return "answer"
    if "result" in t:
        return "result"
    return "vacancy"

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
