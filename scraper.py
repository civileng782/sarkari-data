import json
from datetime import datetime, timezone

data = {
    "vacancies": [
        {
            "id": 1,
            "org": "SSC",
            "title": "SSC CGL 2026 भर्ती",
            "posts": "To be announced",
            "deadline": "30 Apr 2026",
            "applyLink": "https://ssc.gov.in",
            "detailLink": "https://ssc.gov.in",
            "isNew": True
        }
    ],
    "admitCards": [
        {
            "id": 2,
            "org": "UPPSC",
            "title": "UPPSC Pre Admit Card",
            "examDate": "10 May 2026",
            "downloadLink": "#",
            "detailLink": "#",
            "isNew": True
        }
    ],
    "results": [
        {
            "id": 3,
            "org": "Railway",
            "title": "RRB Group D Result",
            "board": "RRB",
            "downloadLink": "#",
            "detailLink": "#",
            "isNew": False
        }
    ],
    "last_updated": datetime.now(timezone.utc).isoformat()
}

try:
    with open("jobs.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print("jobs.json written successfully.")
except (OSError, ValueError) as e:
    print(f"ERROR: Failed to write jobs.json: {e}")
    raise SystemExit(1)
