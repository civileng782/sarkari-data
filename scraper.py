import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ─── Config ───────────────────────────────────────────────────────────────────

OUTPUT_FILE = "jobs.json"
TIMEOUT = 45
MAX_PER_CAT = 50

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Referer": "https://www.google.co.in/",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

CATEGORIES = ["vacancy", "admit", "result", "answer"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Sources ──────────────────────────────────────────────────────────────────

SOURCES = [
    ("https://ssc.gov.in/", "SSC"),
    ("https://ssc.nic.in/", "SSC NIC"),
    ("https://uppsc.up.nic.in/", "UPPSC"),
    ("https://upsc.gov.in/", "UPSC"),
    ("https://upsconline.nic.in/", "UPSC online"),
    ("https://www.ibps.in/", "IBPS"),
    ("https://www.rrbbbs.gov.in/", "RRB Bhubaneswar"),
    ("https://www.rrbcdg.gov.in/", "RRB Chandigarh"),
    ("https://www.rrbmumbai.gov.in/", "RRB Mumbai"),
    ("https://www.rrbald.gov.in/", "RRB Allahabad"),
    ("https://www.rpfonlinereg.org/", "EPFO / RPF"),
    ("https://nta.ac.in/", "NTA"),
]

_DATE_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")

# ─── Helpers ──────────────────────────────────────────────────────────────────


def load_existing() -> dict:
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for cat in CATEGORIES:
                data.setdefault(cat, [])
            return data
        except Exception as exc:
            log.warning("Could not load existing data: %s", exc)

    return {cat: [] for cat in CATEGORIES}


def fetch_page(url: str):
    try:
        log.info("Fetching (static): %s", url)
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "lxml")

        if len(soup.find_all("a")) > 10:
            return soup
        else:
            raise Exception("Low content → fallback")

    except Exception as exc:
        log.warning("Static fetch failed (%s): %s", url, exc)

    try:
        log.info("Playwright fallback: %s", url)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )

            ctx = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                geolocation={"latitude": 28.6139, "longitude": 77.2090},
                permissions=["geolocation"],
                extra_http_headers={
                    "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
                    "Referer": "https://www.google.co.in/",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                },
            )

            page = ctx.new_page()
            page.goto(url, wait_until="networkidle", timeout=TIMEOUT * 1000)

            html = page.content()
            browser.close()

        soup = BeautifulSoup(html, "lxml")

        if len(soup.find_all("a")) < 5:
            log.warning("Playwright content still weak: %s", url)

        return soup

    except Exception as exc:
        log.error("Playwright failed (%s): %s", url, exc)
        return None


def classify(title: str) -> str:
    t = title.lower()

    if any(k in t for k in ("admit card", "hall ticket", "call letter", "interview letter", "प्रवेश पत्र")):
        return "admit"

    if any(k in t for k in ("answer key", "response sheet", "answer sheet", "उत्तर कुंजी")):
        return "answer"

    if any(k in t for k in ("result", "merit list", "cut off", "cutoff", "scorecard", "final list", "परिणाम")):
        return "result"

    return "vacancy"


# ─── Parser ───────────────────────────────────────────────────────────────────

_SKIP_WORDS = {"home", "login", "contact", "about", "register", "hindi", "english", "sitemap", "privacy"}
_SKIP_PATTERNS = ["{{", "}}", "javascript:void", "#"]
_SKIP_PHRASES = ["about us", "contact us", "terms", "privacy policy", "faq", "help", "feedback", "careers"]


def parse_notices(soup: BeautifulSoup, org: str, base_url: str) -> list:
    seen = set()
    found = []

    for tag in soup.find_all("a", href=True):
        text = tag.get_text(" ", strip=True)
        href = tag["href"].strip()

        if not text or len(text) < 18:
            continue

        if any(p in text for p in _SKIP_PATTERNS):
            continue

        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        text_lower = text.lower()

        if any(w in text_lower for w in _SKIP_WORDS):
            continue

        if any(ph in text_lower for ph in _SKIP_PHRASES):
            continue

        full_link = urljoin(base_url, href)

        if full_link in seen:
            continue

        seen.add(full_link)

        cat = classify(text)

        deadline = ""
        parent = tag.find_parent(["td", "li", "div", "tr"])

        if parent:
            raw = parent.get_text(" ", strip=True)
            dates = _DATE_RE.findall(raw)
            if dates:
                deadline = dates[-1]

        found.append(
            {
                "org": org,
                "title": text,
                "detailLink": full_link,
                "applyLink": full_link if cat == "vacancy" else "",
                "downloadLink": full_link if cat != "vacancy" else "",
                "deadline": deadline or "See Official Site",
                "category": cat,
            }
        )

    return found


# ─── Merge Engine ─────────────────────────────────────────────────────────────


def merge_data(existing: dict, scraped: list) -> dict:
    now = datetime.now(timezone.utc)
    uid_ctr = int(now.timestamp() * 1000)

    for item in scraped:
        cat = item["category"]
        link = item["detailLink"]

        if any(e["detailLink"] == link for e in existing[cat]):
            continue

        entry = {
            "id": uid_ctr,
            "org": item["org"],
            "title": item["title"],
            "detailLink": link,
            "applyLink": item["applyLink"],
            "downloadLink": item["downloadLink"],
            "deadline": item["deadline"],
            "isNew": True,
            "date_found": now.isoformat(),
            "category": cat,
        }

        uid_ctr += 1
        existing[cat].insert(0, entry)

    for cat in CATEGORIES:
        for entry in existing[cat]:
            raw = entry.get("date_found")

            if not raw:
                entry["date_found"] = now.isoformat()
                continue

            try:
                found_dt = datetime.fromisoformat(raw)
                if (now - found_dt) > timedelta(days=3):
                    entry["isNew"] = False
            except (ValueError, TypeError):
                entry["date_found"] = now.isoformat()

        existing[cat] = existing[cat][:MAX_PER_CAT]

    return existing


# ─── Main ─────────────────────────────────────────────────────────────────────


def main():
    log.info("=== Sarkari Job Scraper starting ===")

    data = load_existing()
    all_scraped = []

    for url, org in SOURCES:
        soup = fetch_page(url)
        if not soup:
            continue

        items = parse_notices(soup, org, url)
        log.info("  %-20s → %d notices found", org, len(items))
        all_scraped.extend(items)

    updated = merge_data(data, all_scraped)
    updated["last_updated"] = datetime.now(timezone.utc).isoformat()

    counts = {cat: len(updated[cat]) for cat in CATEGORIES}
    log.info("Totals: %s", counts)

    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(updated, f, indent=2, ensure_ascii=False)

        log.info("Saved → %s", OUTPUT_FILE)

    except (OSError, ValueError) as exc:
        log.error("Write failed: %s", exc)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
