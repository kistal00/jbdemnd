"""Fetch open foreign-job lots from DoFE's job search and save them as JSON.

Writes data/jobs.json (current jobs) and data/history.json (daily counts).
Run:  CONTACT_EMAIL=you@example.com python fetch_jobs.py
"""
import html, json, os, re, sys, time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

START = time.time()  # when this run started (used for the time budget below)
RUN_BUDGET_MIN = int(os.environ.get("RUN_BUDGET_MIN", "300"))  # stop fetching details after this many minutes

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

# --- Agency / job details (one extra request per job, cached) ---------------
DETAIL_URL = "https://foreignjob.dofe.gov.np/api/LotSearch/GetLotDetailByID/{}"
CACHE_FILE = DATA / "details_cache.json"
MAX_DETAIL_FETCH = int(os.environ.get("MAX_DETAIL_FETCH", "700"))  # detail requests per run
DETAIL_PAUSE = 1.0        # seconds between detail requests (be gentle with a government site)
REFRESH_DAYS = 3          # re-fetch a cached job's details after this many days (positions left change)
SHOW_PROPRIETOR_MOBILES = True  # False = hide the proprietors' personal mobile numbers

contact = os.environ.get("CONTACT_EMAIL", "not-provided")
session = requests.Session()
session.headers.update({"User-Agent": f"job-demand-dashboard (contact: {contact})"})
# Retries politely when the site answers "too many requests" (429) or has a short outage (5xx).
session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=None, respect_retry_after_header=True)))

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


KNOWN_KEYS = {"lotDetailID", "lotNo", "skillName", "companyName", "countryName", "salaryStr",
              "currency", "deadLine", "deadLineDate", "rowTotal"}


def extra_fields(x):
    """Every other readable field DoFE sends (requirements, qualifications, etc.) for the details page."""
    out = {}
    for k, v in x.items():
        if k in KNOWN_KEYS or v is None or isinstance(v, (dict, list)):
            continue
        kl = k.lower()
        if kl.endswith("id") or kl in ("rownum", "rownumber", "sn", "sno", "totalrows"):
            continue
        t = clean(str(v))
        if t and t.lower() not in ("null", "none", "0", "0.0", "false"):
            out[k] = t
    return out


class AuthError(Exception):
    pass


def num(s):
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def fmt_num(s):
    v = num(s)
    if v is None:
        return ""
    return str(int(v)) if v == int(v) else str(round(v, 2))


def to_int(s):
    v = num(s)
    return int(v) if v is not None else None


def parse_dt(s, fmt="%m/%d/%Y %I:%M:%S %p"):
    try:
        return datetime.strptime(s, fmt)
    except (TypeError, ValueError):
        return None


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def fetch_detail(lot_id):
    """Returns a list of rows, [] if none, or None after repeated network trouble."""
    for attempt in range(3):
        try:
            r = session.get(DETAIL_URL.format(lot_id), timeout=30)
            if r.status_code in (401, 403):
                raise AuthError(r.status_code)
            r.raise_for_status()
            return r.json().get("result") or []
        except AuthError:
            raise
        except (requests.RequestException, ValueError) as e:
            print(f"  detail retry {attempt + 1} for {lot_id}: {e}")
            time.sleep(3 * (attempt + 1))
    return None


def slim(row):
    """Keep only the fields the app needs (keeps the cache small)."""
    interviews = []
    for iv in row.get("interviewList") or []:
        start, end = parse_dt(iv.get("interviewDate")), parse_dt(iv.get("interviewEndDate"))
        t = parse_dt(iv.get("interviewTime"))
        interviews.append({
            "from": start.date().isoformat() if start else None,
            "to": end.date().isoformat() if end else None,
            "time": t.strftime("%I:%M %p").lstrip("0") if t else None,
            "venue": clean(iv.get("interviewVenue")),
        })
    return {
        "lot_no": str(row.get("lotNo") or ""),
        "ra_id": str(row.get("raID") or ""),
        "ra_name": clean(row.get("raName")),
        "ra_address": clean(row.get("raAddress")),
        "phone": clean(row.get("phoneNo")),
        "email": clean(row.get("email")),
        "website": clean(row.get("website")),
        "licence": clean(row.get("permissionNo")),
        "proprietors": [
            {"name": clean(p.get("proprietorName")), "mobile": clean(p.get("mobileNo")), "tel": clean(p.get("officeTelephone"))}
            for p in (row.get("proprietors") or [])
        ],
        "employer_address": clean(row.get("companyAddress")),
        "skill_type": clean(row.get("skillType")),
        "qualification": clean(row.get("qualification")),
        "contract": fmt_num(row.get("contractPeriod")),
        "daily_hours": fmt_num(row.get("dailyWorkHour")),
        "weekly_days": fmt_num(row.get("weeklyWorkDay")),
        "food": clean(row.get("foodFacility")),
        "accommodation": clean(row.get("accomodation")),
        "men_approved": to_int(row.get("approvedNoOfMen")),
        "women_approved": to_int(row.get("approvedNoOfWomen")),
        "men_left": to_int(row.get("remainingNoOfMen")),
        "women_left": to_int(row.get("remainingNoOfWomen")),
        "interviews": interviews,
    }


def build_details(d, today_iso):
    """Flat label -> text map. The app shows these on the job's details page.
    Labels containing agency/phone/contact/address are shown in the contact section with tap-to-call."""
    out = {}

    def put(k, v):
        if v not in (None, "", "0"):
            out[k] = v

    put("Qualification", d["qualification"])
    put("Skill level", d["skill_type"])
    if d["men_approved"] is not None or d["women_approved"] is not None:
        out["Positions approved"] = f"{d['men_approved'] or 0} men, {d['women_approved'] or 0} women"
        out["Positions remaining"] = f"{d['men_left'] or 0} men, {d['women_left'] or 0} women"
    put("Contract period", d["contract"])
    put("Daily work hours", d["daily_hours"])
    put("Weekly work days", d["weekly_days"])
    put("Food", d["food"])
    put("Accommodation", d["accommodation"])
    put("Employer location", d["employer_address"])

    upcoming = [iv for iv in d["interviews"] if (iv["to"] or iv["from"] or "") >= today_iso]
    if upcoming:
        dates = []
        for iv in upcoming:
            span = iv["from"] if iv["from"] == iv["to"] else f"{iv['from']} to {iv['to']}"
            dates.append(span + (f", {iv['time']}" if iv["time"] else ""))
        out["Interview date"] = "\n".join(dict.fromkeys(dates))
        venues = [iv["venue"] for iv in upcoming if iv["venue"]]
        if venues:
            out["Interview address"] = "\n".join(dict.fromkeys(venues))

    put("Agency name", d["ra_name"])
    put("Agency licence", d["licence"])
    put("Agency address", d["ra_address"])
    put("Agency phone", d["phone"])
    put("Agency email", d["email"])
    put("Agency website", d["website"])
    if SHOW_PROPRIETOR_MOBILES:
        for i, p in enumerate(d["proprietors"], 1):
            nums = [x for x in (p["mobile"], p["tel"]) if x and x != d["phone"]]
            if p["name"] or nums:
                out[f"Proprietor {i} contact"] = p["name"] + (": " + ", ".join(nums) if nums else "")
    return out


def enrich(job_list, today):
    """Adds agency contact + job terms to each job, fetching only what is new or stale."""
    today_iso = today.isoformat()
    cache = load_json(CACHE_FILE, {})
    live = {str(j["id"]) for j in job_list}
    cache = {k: v for k, v in cache.items() if k in live}

    def age(entry):
        try:
            return (today - date.fromisoformat(entry["fetched"])).days
        except (KeyError, ValueError):
            return 999

    first_by_id = {}
    for j in job_list:
        first_by_id.setdefault(str(j["id"]), j)
    queue = [j for k, j in first_by_id.items() if k not in cache or age(cache[k]) >= REFRESH_DAYS]
    # new jobs first, then stale ones; soonest-closing first within each group
    queue.sort(key=lambda j: (str(j["id"]) in cache, j["deadline_days"] if j["deadline_days"] is not None else 999))

    ok = bad = 0
    stopped = None
    for j in queue[:MAX_DETAIL_FETCH]:
        if time.time() - START > RUN_BUDGET_MIN * 60:
            print(f"Time budget of {RUN_BUDGET_MIN} minutes reached; the rest is fetched next run.")
            break
        try:
            rows = fetch_detail(j["id"])
        except AuthError as e:
            stopped = f"DoFE answered {e.args[0]} for the detail request (login needed?). Skipping details."
            break
        if rows is None:
            continue  # network trouble; try again next run
        row = next((r for r in rows if str(r.get("lotNo")) == str(j["lot_no"])), None)
        if row is None:
            bad += 1
            cache[str(j["id"])] = {"fetched": today_iso, "none": True}
        else:
            ok += 1
            cache[str(j["id"])] = {"fetched": today_iso, "d": slim(row)}
        if ok + bad >= 20 and bad > 0.1 * (ok + bad):
            stopped = f"{bad} of {ok + bad} detail lookups returned a different lot number. The ID mapping may be wrong. Stopping."
            break
        time.sleep(DETAIL_PAUSE)

    attached = 0
    for j in job_list:
        e = cache.get(str(j["id"]))
        d = e.get("d") if e else None
        if d and d["lot_no"] == str(j["lot_no"]):
            j["ra_id"] = d["ra_id"]
            j["open_men"], j["open_women"] = d["men_left"], d["women_left"]
            j["details"] = build_details(d, today_iso)
            attached += 1
    # Write to a temporary file first, so a crash can never leave a half-written cache.
    tmp_cache = CACHE_FILE.with_suffix(".tmp")
    tmp_cache.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp_cache.replace(CACHE_FILE)
    left = max(0, len(queue) - MAX_DETAIL_FETCH)
    print(f"Details: {attached}/{len(job_list)} jobs have agency info. Fetched {ok + bad} this run "
          f"({bad} did not match). Still queued for later runs: {left}.")
    if stopped:
        print("WARNING:", stopped)
    return attached


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
                        "deadline_days": d,
                        # The site's own deadLineDate looked wrong in testing,
                        # so the closing date is calculated from "days left".
                        "closes_on": (today + timedelta(days=d)).isoformat() if d is not None else None,
                        "genders": [gender],
                        "details": extra_fields(x),
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
    with_details = enrich(job_list, today)
    out = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": SOURCE_PAGE,
        "count": len(job_list),
        "with_details": with_details,
        "raw_rows": raw_rows,
        "by_country": by_country,
        "by_category": by_category,
        "jobs": job_list,
    }
    tmp = JOBS_FILE.with_suffix(".tmp")
    # compact separators: a smaller file for every user to download
    tmp.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(JOBS_FILE)

    history = json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else []
    history = [h for h in history if h["date"] != today.isoformat()]
    history.append({"date": today.isoformat(), "total": len(job_list), "by_country": by_country})
    HISTORY_FILE.write_text(json.dumps(history[-365:], ensure_ascii=False), encoding="utf-8")
    print(f"Saved {len(job_list)} unique jobs ({raw_rows} raw rows). Failures: {failed or 'none'}")


if __name__ == "__main__":
    main()
