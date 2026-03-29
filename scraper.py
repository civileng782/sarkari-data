import json
import logging
import os
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ─── Config ───────────────────────────────────────────────────────────────────

OUTPUT_FILE = "jobs.json"
TIMEOUT = 45
MAX_ITEMS_PER_CAT = 50
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Core Logic ───────────────────────────────────────────────────────────────

def load_existing() -> dict:
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for cat in ["vacancies", "admitCards", "results", "answerKeys"]:
                    if cat not in data:
                        data[cat] = []
                return data
        except Exception:                                    # Fix 4: removed redundant json.JSONDecodeError
            pass
    return {"vacancies": [], "admitCards": [], "results": [], "answerKeys": []}


def fetch_page_hybrid(url: str) -> BeautifulSoup | None:
    """Tries static fetch first, falls back to Playwright if JS-heavy."""
    try:
        log.info("Fetching (Static): %s", url)             # Fix 5: % style logging
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        if len(soup.find_all("a")) > 10:
            return soup
    except Exception as e:
        log.warning("Static failed for %s: %s", url, e)    # Fix 5

    try:
        log.info("Falling back to Playwright: %s", url)    # Fix 5
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=TIMEOUT * 1000)
            content = page.content()
            browser.close()
            return BeautifulSoup(content, "lxml")
    except Exception as e:
        log.error("Hybrid fetch failed for %s: %s", url, e) # Fix 5
        return None


def classify(title: str) -> str:
    t = title.lower()
    if any(k in t for k in ("admit card", "hall ticket", "call letter", "प्रवेश पत्र")):
        return "admitCards"
    if any(k in t for k in ("result", "merit list", "cut off", "scorecard", "परिणाम")):
        return "results"
    if any(k in t for k in ("answer key", "response sheet", "उत्तर कुंजी")):
        return "answerKeys"
    return "vacancies"


def parse_notices(soup: BeautifulSoup, org: str, base_url: str) -> list:
    found = []
    SKIP = {"home", "login", "contact", "about", "register", "hindi", "english"}

    for tag in soup.find_all("a", href=True):
        text = tag.get_text(" ", strip=True)
        href = tag["href"]
        if not text or len(text) < 12 or any(s in text.lower() for s in SKIP):
            continue
        found.append({
            "org": org,
            "title": text,
            "link": urljoin(base_url, href),
            "category": classify(text),
        })
    return found


# ─── Merge Engine ─────────────────────────────────────────────────────────────

def merge_data(existing: dict, scraped_items: list) -> dict:
    """
    1. Checks for duplicates by link.
    2. Keeps old jobs that are still relevant.
    3. Flips 'isNew' to False after 3 days.
    """
    now = datetime.now(timezone.utc)
    uid_counter = int(now.timestamp() * 1000)              # Fix 2: single base, incremented below

    for item in scraped_items:
        cat = item["category"]
        link = item["link"]

        exists = any(old["detailLink"] == link for old in existing[cat])
        if not exists:
            new_entry = {
                "id": uid_counter,                         # Fix 2: unique per item
                "org": item["org"],
                "title": item["title"],
                "detailLink": link,
                "isNew": True,
                "date_found": now.isoformat(),
            }
            uid_counter += 1                               # Fix 2: increment after each use

            if cat == "vacancies":
                new_entry.update({"applyLink": link, "deadline": "See Link"})
            else:
                new_entry["downloadLink"] = link

            existing[cat].insert(0, new_entry)

    # Post-processing: update isNew flag and trim size
    for cat in ["vacancies", "admitCards", "results", "answerKeys"]:
        for entry in existing[cat]:
            # Fix 1: gracefully handle missing date_found on old entries
            raw_date = entry.get("date_found")
            if raw_date is None:
                entry["date_found"] = now.isoformat()      # Fix 1: backfill missing field
                continue                                    # Fix 1: skip flip — age unknown
            try:
                found_date = datetime.fromisoformat(raw_date)
                if now - found_date > timedelta(days=3):
                    entry["isNew"] = False
            except (ValueError, TypeError):
                entry["date_found"] = now.isoformat()      # Fix 1: repair corrupted date

        existing[cat] = existing[cat][:MAX_ITEMS_PER_CAT]

    return existing


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("Starting Scraper...")
    data = load_existing()

    all_scraped = []
    sources = [
        ("https://ssc.gov.in/",        "SSC"),
        ("https://uppsc.up.nic.in/",   "UPPSC"),
        ("https://rrbbbs.gov.in/",   "RRB Bhubaneswar"),
        ("https://www.rrbcdg.gov.in/", "RRB Chandigarh"),
    ]

    for url, org in sources:
        soup = fetch_page_hybrid(url)
        if soup:
            all_scraped.extend(parse_notices(soup, org, url))

    updated_data = merge_data(data, all_scraped)
    updated_data["last_updated"] = datetime.now(timezone.utc).isoformat()

    try:                                                    # Fix 3: error handling on write
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(updated_data, f, indent=2, ensure_ascii=False)
        log.info("Done! Updated %s", OUTPUT_FILE)          # Fix 5
        log.info(
            "Final counts — vacancies: %d | admitCards: %d | results: %d | answerKeys: %d",
            len(updated_data["vacancies"]),
            len(updated_data["admitCards"]),
            len(updated_data["results"]),
            len(updated_data["answerKeys"]),
        )
    except (OSError, ValueError) as e:                     # Fix 3
        log.error("Failed to write %s: %s", OUTPUT_FILE, e)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
