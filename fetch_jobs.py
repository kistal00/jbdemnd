"""Fetch open foreign-job lots from DoFE's job search and save them as JSON.

Writes data/jobs.json (current jobs) and data/history.json (daily counts).
Run:  CONTACT_EMAIL=you@example.com python fetch_jobs.py
"""
import html, json, os, re, sys, time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

URL = "https://foreignjob.dofe.gov.np/api/LotSearch/GetLotList"
SOURCE_PAGE = "https://foreignjob.dofe.gov.np/Home/Index"
COUNTRIES = [
    "Algeria", "Bahrain", "Cyprus", "Hong Kong SAR,China", "Japan", "Jordan",
    "Kuwait", "Lebanon", "Macau SAR,China", "Malaysia", "Maldives", "Mauritius",
    "Mongolia", "Oman", "Qatar", "Saudi Arabia", "South Africa", "Sri Lanka",
    "Turkey", "UAE",
]
GENDERS = ["Male", "Female"]
LIMIT = 100
PAUSE = 1.5
MAX_PAGES = 60

DATA = Path("data")
JOBS_FILE = DATA / "jobs.json"
HISTORY_FILE = DATA / "history.json"

contact = os.environ.get("CONTACT_EMAIL", "not-provided")
session = requests.Session()
session.headers.update({"User-Agent": f"job-demand-dashboard (contact: {contact})"})

CURRENCY_CODES = {
    "QR (QATAR)": "QAR", "QR": "QAR", "SR": "SAR", "KD": "KWD", "BD": "BHD",
    "OR": "OMR", "RM": "MYR", "YEN": "JPY", "AED": "AED", "EUR": "EUR",
    "USD": "USD", "JOD": "JOD", "MUR": "MUR", "DZD": "DZD",
}

# First group whose keyword appears in the title wins, so order matters.
CATEGORIES = [
    ("Engineer & management", ["engineer", "engr", "supervisor", "superviser", "foreman", "forman", "foremen", "chargehand", "charge hand", "manager", "surveyor", "serveyor", "team leader"]),
    ("Beauty & salon", ["beautic", "hairdress", "hair dress", "hair stylist", "manicur", "pedicur", "nail", "massag", "masseu", "barber", "salon", "saloon", "spa therapist", "henna"]),
    ("Driver", ["driver", "chauffeur", "trailer", "truck"]),
    ("Heavy equipment operator", ["excavator", "crane", "forklift", "loader", "heavy equipment", "bulldozer", "grader", "bob cat", "bobcat", "paver", "roller", "drilling", "digger", "rock breaker", "backhoe", "asphalt plant"]),
    ("Electrician", ["electric"]),
    ("Plumber & pipe fitter", ["plumb", "pipe"]),
    ("Welder & fabrication", ["weld", "fabricat", "fitter", "steel", "steal", "smith", "reinforc", "sheet metal"]),
    ("Construction trades", ["carpent", "mason", "masion", "plaster", "painter", "panter", "tile", "scaffold", "scafold", "rigger", "brick", "gypsum", "ceiling", "glazier", "joiner", "house builder", "house-builder", "builder", "stone"]),
    ("Security", ["security", "guard", "watchm"]),
    ("Technician & mechanic", ["technician", "mechanic", "hvac", "air condition", "aircondition"]),
    ("Cleaner & laundry", ["clean", "housekeep", "house keeping", "janitor", "sweeper", "laundry", "dry clean", "room attendant", "hk attendant", "presser", "iron man"]),
    ("Food & hospitality", ["cook", "chef", "kitchen", "baker", "butcher", "food", "waiter", "waitress", "barista", "bartender", "barman", "restaurant", "hotel", "juice", "coffee", "tea maker", "sandwich", "pizza", "shawar", "griller", "commi", "dishwash", "dish wash", "steward", "confection", "pastry", "salad", "service crew", "catering", "hostess", "beverage"]),
    ("Warehouse & loading", ["warehouse", "loading", "unloading", "packing", "packaging", "packer", "porter", "stevedore", "freight", "cargo", "store keep", "storekeep", "store keepar", "baggage", "parcel", "stock"]),
    ("Tailor & garment", ["tailor", "sewing", "garment", "shoe maker", "upholster", "knitting"]),
    ("Factory & manufacturing", ["factory", "manufactur", "production", "machine operator", "machinist", "assembl"]),
    ("Agriculture & farm", ["agricultur", "farm", "poultry", "livestock", "gardener", "garden", "plantation", "fisherman", "forester", "landscap", "horticult", "florist"]),
    ("Care, health & domestic", ["domestic", "housemaid", "maid", "caregiver", "care giver", "care worker", "nanny", "nurse", "pharmac", "medical"]),
    ("Sales & office", ["sales", "cashier", "office", "clerk", "accountant", "receptionist", "secretar", "typist", "data entry", "document control", "admin", "filing", "customer service", "customer care", "telephone", "ticket", "representative", "purchas", "executive"]),
    ("General labour & helper", ["labour", "labor", "helper", "worker", "general", "assistant"]),
]


def categorize(title):
    t = (title or "").lower()
    for name, words in CATEGORIES:
        if any(w in t for w in words):
            return name
    return "Other"


def clean(s):
    s = s or ""
    for _ in range(3):  # some values are double-encoded (&amp;amp;)
        n = html.unescape(s)
        if n == s:
            break
        s = n
    return re.sub(r"\s+", " ", s).strip()


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
        v = float(str(s).replace(",", ""))
    except ValueError:
        return None
    return v if v >= 20 else None  # tiny values (e.g. 2.0) are data-entry errors


def main():
    today = datetime.now(timezone(timedelta(hours=5, minutes=45))).date()  # Nepal date
    jobs, failed, raw_rows = {}, [], 0
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
                    raw_rows += 1
                    key = (x["lotDetailID"], x.get("lotNo"))
                    if key in jobs:  # same job already seen for the other gender
                        if gender not in jobs[key]["genders"]:
                            jobs[key]["genders"].append(gender)
                        continue
                    title = clean(x.get("skillName"))
                    d = days_left(x.get("deadLine"))
                    cur = clean(x.get("currency"))
                    jobs[key] = {
                        "id": x["lotDetailID"],
                        "lot_no": x.get("lotNo"),
                        "title": title,
                        "category": categorize(title),
                        "employer": clean(x.get("companyName")),
                        "country": x.get("countryName") or country,
                        "salary": to_float(x.get("salaryStr")),
                        "currency": cur,
                        "currency_code": CURRENCY_CODES.get(cur.upper(), cur),
                        "deadline_text": x.get("deadLine"),
                        "deadline_days": d,
                        # The site's own deadLineDate looked wrong in testing,
                        # so the closing date is calculated from "days left".
                        "closes_on": (today + timedelta(days=d)).isoformat() if d is not None else None,
                        "genders": [gender],
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
    for j in job_list:
        j["genders"].sort()
    if not job_list:
        sys.exit("No jobs fetched; keeping existing data.")

    prev_raw = 0
    if JOBS_FILE.exists():
        prev_raw = json.loads(JOBS_FILE.read_text(encoding="utf-8")).get("raw_rows") or 0
    if failed and prev_raw and raw_rows < prev_raw * 0.5:
        sys.exit(f"Only {raw_rows} rows vs {prev_raw} before; failures: {failed}. Keeping old data.")

    by_country, by_category = {}, {}
    for j in job_list:
        by_country[j["country"]] = by_country.get(j["country"], 0) + 1
        by_category[j["category"]] = by_category.get(j["category"], 0) + 1
    empty = [c for c in COUNTRIES if c not in by_country]
    if empty:
        print("NOTE: no jobs found for:", empty, "(may be genuine, or a spelling issue)")
    print("Share in 'Other':", round(100 * by_category.get("Other", 0) / len(job_list)), "%")

    DATA.mkdir(exist_ok=True)
    out = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": SOURCE_PAGE,
        "count": len(job_list),
        "raw_rows": raw_rows,
        "by_country": by_country,
        "by_category": by_category,
        "jobs": job_list,
    }
    tmp = JOBS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    tmp.replace(JOBS_FILE)

    history = json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else []
    history = [h for h in history if h["date"] != today.isoformat()]
    history.append({"date": today.isoformat(), "total": len(job_list), "by_country": by_country})
    HISTORY_FILE.write_text(json.dumps(history[-365:], ensure_ascii=False), encoding="utf-8")
    print(f"Saved {len(job_list)} unique jobs ({raw_rows} raw rows). Failures: {failed or 'none'}")


if __name__ == "__main__":
    main()
