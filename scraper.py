import json
import logging
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ─── Config ───────────────────────────────────────────────────────────────────

OUTPUT_FILE = "jobs.json"
TIMEOUT = 20  # Seconds
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─── Core Logic ───────────────────────────────────────────────────────────────

def load_existing() -> dict:
    """Loads previous results to avoid losing data if a scrape fails."""
    try:
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"vacancies": [], "admitCards": [], "results": [], "answerKeys": []}


def fetch_page_hybrid(url: str) -> BeautifulSoup | None:
    """
    Tries static requests first. If the page is empty/JS-heavy, 
    falls back to Playwright.
    """
    # 1. Try Static Fetch
    try:
        log.info(f"Fetching (Static): {url}")
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        
        # Check if we found substantial content
        if len(soup.find_all("a")) > 10:
            return soup
        log.info(f"Page {url} appears JS-heavy. Switching to Playwright...")
    except Exception as e:
        log.warning(f"Static fetch failed for {url}: {e}")

    # 2. Try Dynamic Fetch (Playwright)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = context.new_page()
            # Wait for network to settle so JS content loads
            page.goto(url, wait_until="networkidle", timeout=TIMEOUT * 1000)
            content = page.content()
            browser.close()
            return BeautifulSoup(content, "lxml")
    except Exception as e:
        log.error(f"Dynamic fetch also failed for {url}: {e}")
        return None


def classify(title: str) -> str:
    t = title.lower()
    if any(k in t for k in ("admit card", "hall ticket", "call letter", "प्रवेश पत्र")):
        return "admitCards"
    if any(k in t for k in ("result", "merit list", "cut off", "cutoff", "scorecard", "परिणाम")):
        return "results"
    if any(k in t for k in ("answer key", "response sheet", "objection", "उत्तर कुंजी")):
        return "answerKeys"
    return "vacancies"


def parse_notices(soup: BeautifulSoup, org: str, base_url: str, uid_start: int, limit: int = 20) -> dict:
    categories = {"vacancies": [], "admitCards": [], "results": [], "answerKeys": []}
    seen_links = set()
    uid = uid_start
    count = 0

    SKIP_TEXTS = {"home", "login", "contact", "about", "register", "back", "more", "hindi", "english"}

    for tag in soup.find_all("a", href=True):
        text = tag.get_text(" ", strip=True)
        href = tag["href"]

        # Filter out junk
        if not text or len(text) < 12 or any(s in text.lower() for s in SKIP_TEXTS):
            continue
        
        full_url = urljoin(base_url, href)
        if full_url in seen_links:
            continue
        seen_links.add(full_url)

        cat = classify(text)
        entry = {
            "id": uid,
            "org": org,
            "title": text,
            "detailLink": full_url,
            "isNew": True,
            "date_found": datetime.now(timezone.utc).strftime("%Y-%m-%d")
        }
        
        # Add category-specific keys
        if cat == "vacancies":
            entry.update({"applyLink": full_url, "deadline": "See Link"})
        else:
            entry["downloadLink"] = full_url

        categories[cat].append(entry)
        uid += 1
        count += 1
        if count >= limit:
            break

    log.info(f"Processed {org}: {count} items found.")
    return categories


# ─── Scrapers ─────────────────────────────────────────────────────────────────

def scrape_ssc() -> dict:
    soup = fetch_page_hybrid("https://ssc.gov.in/")
    return parse_notices(soup, "SSC", "https://ssc.gov.in", 100) if soup else {}

def scrape_uppsc() -> dict:
    soup = fetch_page_hybrid("https://uppsc.up.nic.in/")
    return parse_notices(soup, "UPPSC", "https://uppsc.up.nic.in", 200) if soup else {}

def scrape_rrb() -> dict:
    combined = {"vacancies": [], "admitCards": [], "results": [], "answerKeys": []}
    # RRB has many regional sites; these are the main hubs
    sources = [
        ("https://rrbapply.gov.in/", "RRB Central", 300),
        ("https://www.rrbcdg.gov.in/", "RRB Chandigarh", 400)
    ]
    for url, name, start_id in sources:
        soup = fetch_page_hybrid(url)
        if soup:
            res = parse_notices(soup, name, url, start_id, limit=10)
            for k in combined: combined[k].extend(res.get(k, []))
    return combined

# ─── Merge & Main ─────────────────────────────────────────────────────────────

def merge_data(existing: dict, *new_sources: dict) -> dict:
    final = {"vacancies": [], "admitCards": [], "results": [], "answerKeys": []}
    for cat in final:
        all_new = []
        for src in new_sources:
            all_new.extend(src.get(cat, []))
        
        # Update logic: If we got new data today, use it. 
        # Otherwise, keep the old data so the JSON isn't empty.
        final[cat] = all_new if all_new else existing.get(cat, [])
    return final

def main():
    log.info("Starting Job Scraper...")
    existing = load_existing()
    
    ssc = scrape_ssc()
    uppsc = scrape_uppsc()
    rrb = scrape_rrb()
    
    final_data = merge_data(existing, ssc, uppsc, rrb)
    final_data["last_updated"] = datetime.now(timezone.utc).isoformat()
    
    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(final_data, f, indent=2, ensure_ascii=False)
        log.info(f"Successfully saved to {OUTPUT_FILE}")
    except Exception as e:
        log.error(f"Save failed: {e}")

if __name__ == "__main__":
    main()
