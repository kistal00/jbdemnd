"""Fetch open foreign-job lots from DoFE's job search and save them as JSON.

Writes data/jobs.json (current jobs) and data/history.json (daily counts).
Run:  CONTACT_EMAIL=you@example.com python fetch_jobs.py
"""
import json, os, re, sys, time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

URL = "https://foreignjob.dofe.gov.np/api/LotSearch/GetLotList"
SOURCE_PAGE = "https://foreignjob.dofe.gov.np/Home/Index"
# Values copied from the site's country dropdown. Any that return 0 jobs are
# reported at the end, so you can fix a wrong spelling.
COUNTRIES = [
    "Algeria", "Bahrain", "Cyprus", "Hong Kong SAR,China", "Japan", "Jordan",
    "Kuwait", "Lebanon", "Macau SAR,China", "Malaysia", "Maldives", "Mauritius",
    "Mongolia", "Oman", "Qatar", "Saudi Arabia", "South Africa", "Sri Lanka",
    "Turkey", "UAE",
]
GENDERS = ["Male", "Female"]
LIMIT = 100        # jobs per request (tested: 100 works)
PAUSE = 1.5        # seconds between requests; be gentle with a government site
MAX_PAGES = 60     # safety stop per country/gender

DATA = Path("data")
JOBS_FILE = DATA / "jobs.json"
HISTORY_FILE = DATA / "history.json"

contact = os.environ.get("CONTACT_EMAIL", "not-provided")
session = requests.Session()
session.headers.update({"User-Agent": f"job-demand-dashboard (contact: {contact})"})


def post(country, gender, page):
    payload = {
        "Offset": str(page), "Limit": str(LIMIT), "Country": country,
        "SkillType": "", "SkillName": "", "CompanyName": "", "SearchText": "",
        "SalaryFrom": "0", "SalaryTo": "0", "Gender": gender,
        "SortBy": "Deadline", "OrderBy": "0", "RaName": "",
        "InterviewVenue": "", "InterviewDateFrom": "", "InterviewDateTo": "",
        "TodayOnly": "false",
    }
    for attempt in range(3):
        try:
            r = session.post(URL, data={"postdata": json.dumps(payload), "isSearched": "true"}, timeout=30)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  retry {attempt + 1} for {country}/{gender}/page {page}: {e}")
            time.sleep(5 * (attempt + 1))
    return None


def days_left(text):
    t = (text or "").strip().lower()
    if t == "today":
        return 0
    m = re.search(r"\d+", t)
    return int(m.group()) if m else None


def to_float(s):
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


# Job-type groups for the dashboard dropdown. The first group whose keyword
# appears in the title wins, so order matters. Anything unmatched is "Other".
CATEGORIES = [
    ("Driver", ["driver", "chauffeur", "trailer", "truck"]),
    ("Heavy equipment operator", ["excavator", "crane", "forklift", "loader", "heavy equipment", "bulldozer", "grader"]),
    ("Electrician", ["electric"]),
    ("Plumber & pipe fitter", ["plumb", "pipe"]),
    ("Welder & fabrication", ["weld", "fabricat", "fitter", "steel"]),
    ("Construction trades", ["carpent", "mason", "plaster", "painter", "tiler", "tiling", "scaffold", "rigger"]),
    ("Security", ["security", "guard"]),
    ("Cleaner", ["clean", "housekeep", "janitor", "sweeper"]),
    ("Food & hospitality", ["cook", "chef", "kitchen", "baker", "butcher", "food", "waiter", "waitress", "barista", "restaurant", "hotel"]),
    ("Warehouse & loading", ["warehouse", "loading", "unloading", "packing", "packaging", "store keeper", "storekeeper"]),
    ("Factory & manufacturing", ["factory", "manufactur", "production", "machine operator"]),
    ("Agriculture & farm", ["agricultur", "farm", "poultry", "livestock", "gardener"]),
    ("Domestic & care", ["domestic", "housemaid", "maid", "caregiver", "care giver", "nanny", "nurse"]),
    ("Technician & mechanic", ["technician", "mechanic", "maintenance", "hvac", "air condition"]),
    ("Sales & office", ["sales", "cashier", "office", "clerk", "accountant", "receptionist"]),
    ("General labour & helper", ["labour", "labor", "helper", "worker", "general"]),
]


def categorize(title):
    t = (title or "").lower()
    for name, words in CATEGORIES:
        if any(w in t for w in words):
            return name
    return "Other"


def main():
    today = date.today()
    jobs, failed = {}, []
    for country in COUNTRIES:
        for gender in GENDERS:
            page, got = 1, 0
            while page <= MAX_PAGES:
                resp = post(country, gender, page)
                if resp is None:
                    failed.append(f"{country}/{gender}")
                    break
                rows = resp.get("result") or []
                if not rows:
                    break
                for x in rows:
                    d = days_left(x.get("deadLine"))
                    jobs[(x["lotDetailID"], gender)] = {
                        "id": x["lotDetailID"],
                        "lot_no": x.get("lotNo"),
                        "title": (x.get("skillName") or "").strip(),
                        "category": categorize(x.get("skillName")),
                        "employer": (x.get("companyName") or "").strip(),
                        "country": x.get("countryName") or country,
                        "salary": to_float(x.get("salaryStr")),
                        "currency": x.get("currency"),
                        "deadline_text": x.get("deadLine"),
                        "deadline_days": d,
                        # The site's own deadLineDate looked wrong in testing,
                        # so the closing date is calculated from "days left".
                        "closes_on": (today + timedelta(days=d)).isoformat() if d is not None else None,
                        "gender": gender,
                        "source_url": SOURCE_PAGE,
                    }
                got += len(rows)
                if got >= (rows[0].get("rowTotal") or 0):
                    break
                page += 1
                time.sleep(PAUSE)
            print(f"{country}/{gender}: {got} fetched")
            time.sleep(PAUSE)

    job_list = sorted(jobs.values(), key=lambda j: (j["country"], j["deadline_days"] if j["deadline_days"] is not None else 999))
    if not job_list:
        sys.exit("No jobs fetched; keeping existing data.")

    previous = 0
    if JOBS_FILE.exists():
        previous = json.loads(JOBS_FILE.read_text(encoding="utf-8")).get("count", 0)
    if failed and previous and len(job_list) < previous * 0.5:
        sys.exit(f"Only {len(job_list)} jobs vs {previous} before; failures: {failed}. Keeping old data.")

    by_country = {}
    for j in job_list:
        by_country[j["country"]] = by_country.get(j["country"], 0) + 1
    empty = [c for c in COUNTRIES if c not in by_country]
    if empty:
        print("WARNING: no jobs found for:", empty, "(may be genuine, or the country spelling is wrong)")

    DATA.mkdir(exist_ok=True)
    out = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": SOURCE_PAGE,
        "count": len(job_list),
        "by_country": by_country,
        "jobs": job_list,
    }
    tmp = JOBS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    tmp.replace(JOBS_FILE)

    history = json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else []
    history = [h for h in history if h["date"] != today.isoformat()]
    history.append({"date": today.isoformat(), "total": len(job_list), "by_country": by_country})
    HISTORY_FILE.write_text(json.dumps(history[-365:], ensure_ascii=False), encoding="utf-8")
    print(f"Saved {len(job_list)} jobs. Failures: {failed or 'none'}")


if __name__ == "__main__":
    main()
