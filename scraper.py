import json
import logging
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ─── Config ───────────────────────────────────────────────────────────────────

OUTPUT_FILE = "jobs.json"
TIMEOUT = 15
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def load_existing() -> dict:
    """Load existing jobs.json as fallback if a scrape fails."""
    try:
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "vacancies": [],
            "admitCards": [],
            "results": [],
            "answerKeys": [],
        }


def fetch_page(url: str) -> BeautifulSoup | None:
    """GET a page and return BeautifulSoup, or None on any failure."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as e:
        log.warning("Could not fetch %s — %s", url, e)
        return None


def classify(title: str) -> str:
    """Classify a notice title into one of the four categories."""
    t = title.lower()
    if any(k in t for k in ("admit card", "hall ticket", "call letter", "प्रवेश पत्र")):
        return "admitCards"
    if any(k in t for k in ("result", "merit list", "cut off", "cutoff", "scorecard", "परिणाम")):
        return "results"
    if any(k in t for k in ("answer key", "response sheet", "objection", "उत्तर कुंजी")):
        return "answerKeys"
    return "vacancies"


def make_entry(uid: int, org: str, title: str, link: str, category: str) -> dict:
    """Build a normalised entry dict for any category."""
    base = {"id": uid, "org": org, "title": title, "isNew": True, "detailLink": link}
    if category == "vacancies":
        base["applyLink"] = link
        base["posts"] = "See notification"
        base["deadline"] = "See notification"
    else:
        base["downloadLink"] = link
    return base


def parse_notices(
    soup: BeautifulSoup,
    org: str,
    base_url: str,
    uid_start: int,
    limit: int = 30,
) -> dict:
    """
    Generic notice-board parser.
    Walks all <a> tags, classifies each by title, caps at `limit` entries.
    """
    categories: dict[str, list] = {
        "vacancies": [],
        "admitCards": [],
        "results": [],
        "answerKeys": [],
    }
    seen: set[str] = set()
    uid = uid_start
    total = 0

    SKIP = {
        "home", "contact us", "login", "register", "hindi", "english",
        "back", "more", "click here", "view more", "download", "notification",
    }

    for tag in soup.find_all("a", href=True):
        text = tag.get_text(" ", strip=True)
        href = tag["href"]

        # Skip too-short, nav-only, or already-seen links
        if not text or len(text) < 12:
            continue
        if text.lower().strip() in SKIP:
            continue
        if text in seen:
            continue
        seen.add(text)

        # Build absolute URL
        if href.startswith("http"):
            full_url = href
        elif href.startswith("/"):
            full_url = base_url.rstrip("/") + href
        else:
            full_url = base_url.rstrip("/") + "/" + href

        category = classify(text)
        entry = make_entry(uid, org, text, full_url, category)
        categories[category].append(entry)

        uid += 1
        total += 1
        if total >= limit:
            break

    log.info("%s: %d notices parsed (vacancies=%d, admitCards=%d, results=%d, answerKeys=%d)",
             org,
             total,
             len(categories["vacancies"]),
             len(categories["admitCards"]),
             len(categories["results"]),
             len(categories["answerKeys"]))
    return categories


# ─── Scrapers ─────────────────────────────────────────────────────────────────

def scrape_ssc() -> dict:
    """Scrape https://ssc.gov.in notice board."""
    empty = {"vacancies": [], "admitCards": [], "results": [], "answerKeys": []}
    soup = fetch_page("https://ssc.gov.in/")
    if soup is None:
        log.warning("SSC: site unreachable — keeping existing data for this source.")
        return empty
    return parse_notices(soup, "SSC", "https://ssc.gov.in", uid_start=100, limit=30)


def scrape_uppsc() -> dict:
    """Scrape https://uppsc.up.nic.in notice board."""
    empty = {"vacancies": [], "admitCards": [], "results": [], "answerKeys": []}
    soup = fetch_page("https://uppsc.up.nic.in/")
    if soup is None:
        log.warning("UPPSC: site unreachable — keeping existing data for this source.")
        return empty
    return parse_notices(soup, "UPPSC", "https://uppsc.up.nic.in", uid_start=200, limit=30)


def scrape_rrb() -> dict:
    """
    Scrape RRB notices.
    RRB has no single central site — 21 regional boards exist.
    We scrape the central apply portal + two major regional boards.
    """
    empty = {"vacancies": [], "admitCards": [], "results": [], "answerKeys": []}
    combined: dict[str, list] = {"vacancies": [], "admitCards": [], "results": [], "answerKeys": []}

    sources = [
        ("https://rrbapply.gov.in/",   "RRB",             300),   # central apply portal
        ("https://www.rrbbbs.gov.in/", "RRB Bhubaneswar", 340),   # major regional board
        ("https://www.rrbcdg.gov.in/", "RRB Chandigarh",  370),   # another regional board
    ]

    all_unreachable = True
    for url, org, uid_start in sources:
        soup = fetch_page(url)
        if soup is None:
            log.warning("%s: unreachable, skipping.", org)
            continue
        all_unreachable = False
        result = parse_notices(soup, org, url, uid_start=uid_start, limit=15)
        for cat in combined:
            combined[cat].extend(result.get(cat, []))

    if all_unreachable:
        log.warning("RRB: all sources unreachable — keeping existing data.")
        return empty

    return combined


# ─── Merge ────────────────────────────────────────────────────────────────────

def merge(existing: dict, *sources: dict) -> dict:
    """
    Merge all scraped sources into one dict.
    Per category: use fresh scraped data if any entries found,
    otherwise fall back to existing jobs.json data (skip & keep old).
    """
    merged: dict[str, list] = {
        "vacancies": [],
        "admitCards": [],
        "results": [],
        "answerKeys": [],
    }
    for category in merged:
        fresh = []
        for source in sources:
            fresh.extend(source.get(category, []))
        # If scraping produced nothing for this category, keep old data
        merged[category] = fresh if fresh else existing.get(category, [])
    return merged


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    existing = load_existing()
    log.info("Loaded existing jobs.json as fallback.")

    ssc_data   = scrape_ssc()
    uppsc_data = scrape_uppsc()
    rrb_data   = scrape_rrb()

    data = merge(existing, ssc_data, uppsc_data, rrb_data)
    data["last_updated"] = datetime.now(timezone.utc).isoformat()

    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        log.info("jobs.json written successfully.")
        log.info(
            "Final counts — vacancies: %d | admitCards: %d | results: %d | answerKeys: %d",
            len(data["vacancies"]),
            len(data["admitCards"]),
            len(data["results"]),
            len(data["answerKeys"]),
        )
    except (OSError, ValueError) as e:
        log.error("Failed to write jobs.json: %s", e)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
```

---

**`requirements.txt`**
```
requests==2.32.3
beautifulsoup4==4.12.3
lxml==5.3.0
