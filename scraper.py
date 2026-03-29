import json
from datetime import datetime

data = {
    "vacancies": [
        {
            "id": 1,
            "org": "SSC",
            "title": "SSC CGL 2026 भर्ती",
            "posts": "7500+ Posts",
            "deadline": "30 Apr 2026",
            "applyLink": "https://ssc.nic.in",
            "detailLink": "https://ssc.nic.in",
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
    "last_updated": datetime.utcnow().isoformat()
}

with open("jobs.json", "w") as f:
    json.dump(data, f, indent=2)
