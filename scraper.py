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
MAX_ITEMS_PER_CAT = 50  # Keeps the JSON file size manageable
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
                # Ensure structure is correct
                for cat in ["vacancies", "admitCards", "results", "answerKeys"]:
                    if cat not in data: data[cat] = []
                return data
        except (json.JSONDecodeError, Exception):
            pass
    return {"vacancies": [], "admitCards": [], "results": [], "answerKeys": []}

def fetch_page_hybrid(url: str) -> BeautifulSoup | None:
    """Tries static fetch, falls back to Playwright if JS-heavy."""
    try:
        log.info(f"Fetching (Static): {url}")
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        if len(soup.find_all("a")) > 10:
            return soup
    except Exception as e:
        log.warning(f"Static failed for {url}: {e}")

    try:
        log.info(f"Falling back to Playwright: {url}")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=TIMEOUT * 1000)
            content = page.content()
            browser.close()
            return BeautifulSoup(content, "lxml")
    except Exception as e:
        log.error(f"Hybrid fetch failed for {url}: {e}")
        return None

def classify(title: str) -> str:
    t = title.lower()
    if any(k in t for k in ("admit card", "hall ticket", "call letter", "प्रवेश पत्र")): return "admitCards"
    if any(k in t for k in ("result", "merit list", "cut off", "scorecard", "परिणाम")): return "results"
    if any(k in t for k in ("answer key", "response sheet", "उत्तर कुंजी")): return "answerKeys"
    return "vacancies"

def parse_notices(soup: BeautifulSoup, org: str, base_url: str) -> list:
    """Extracts raw data from the soup."""
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
            "category": classify(text)
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
    
    for item in scraped_items:
        cat = item["category"]
        link = item["link"]
        
        # Check if this link already exists in the existing data
        exists = any(old["detailLink"] == link for old in existing[cat])
        
        if not exists:
            # Create a brand new entry
            new_entry = {
                "id": int(now.timestamp() * 1000), # Unique ID based on time
                "org": item["org"],
                "title": item["title"],
                "detailLink": link,
                "isNew": True,
                "date_found": now.isoformat()
            }
            if cat == "vacancies":
                new_entry.update({"applyLink": link, "deadline": "See Link"})
            else:
                new_entry["downloadLink"] = link
            
            existing[cat].insert(0, new_entry) # Add to the top

    # Post-processing: Update 'isNew' and trim size
    for cat in ["vacancies", "admitCards", "results", "answerKeys"]:
        # 1. Update isNew flag (if older than 3 days)
        for entry in existing[cat]:
            found_date = datetime.fromisoformat(entry["date_found"])
            if now - found_date > timedelta(days=3):
                entry["isNew"] = False
        
        # 2. Trim the list so it doesn't grow forever
        existing[cat] = existing[cat][:MAX_ITEMS_PER_CAT]

    return existing

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("Starting Scraper...")
    data = load_existing()
    
    # 1. Scrape all sources
    all_scraped = []
    
    sources = [
        ("https://ssc.gov.in/", "SSC"),
        ("https://uppsc.up.nic.in/", "UPPSC"),
        ("https://rrbapply.gov.in/", "RRB Central"),
        ("https://www.rrbcdg.gov.in/", "RRB Chandigarh")
    ]
    
    for url, org in sources:
        soup = fetch_page_hybrid(url)
        if soup:
            all_scraped.extend(parse_notices(soup, org, url))
    
    # 2. Merge and Save
    updated_data = merge_data(data, all_scraped)
    updated_data["last_updated"] = datetime.now(timezone.utc).isoformat()
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(updated_data, f, indent=2, ensure_ascii=False)
    
    log.info(f"Done! Updated {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
