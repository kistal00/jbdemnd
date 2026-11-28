# Builds data/seasonal.json once a day (GitHub Actions 04:00 Nepal time)
# Sources: 1) DoFE jobs.json - COLLECT ALL, not only seasonal (MAIN)
#          2) Job Bank Canada - ENABLED, shows in Foreign section, at least 4 real jobs
#          3) EURES Europe
#          4) Manual entries
# App only reads seasonal.json.
# source_url is NOT included - app never shows original links.
# A collector that fails is logged and skipped.
import csv
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "seasonal.json"
MANUAL = ROOT / "data" / "manual_seasonal.json"
DOFE_JOBS = ROOT / "data" / "jobs.json"
ENABLE_CANADA = True
MAX_JOBS = 10000
MIN_CANADA_JOBS = 4
UA = {"User-Agent": "job-demand-dashboard (contact: " + os.environ.get("CONTACT_EMAIL", "kistal.gamer2020@gmail.com") + ")"}

SEASONAL_WORDS = re.compile(r"seasonal|harvest|picker|picking|farm|agricultur|greenhouse|orchard|horticultur|fruit|berry|vegetable|packing|livestock|dairy|poultry|vineyard|forestry", re.I)

# ---------- DoFE: COLLECT ALL ----------
DOFE_SEASONAL_CATEGORIES = {"Agriculture & farm", "Food & hospitality", "Cleaner & laundry", "Care, health & domestic", "Factory & manufacturing", "Construction trades"}
DOFE_SEASONAL_KEYWORDS = ["seasonal", "season", "harvest", "farm", "agricultur", "greenhouse", "hotel", "hospitality", "resort", "tourism", "restaurant", "kitchen", "housekeep", "cleaner", "fruit", "vegetable", "picking", "packhouse", "warehouse", "poultry"]
EUROPE_SEASONAL_COUNTRIES = {"Bulgaria", "Romania", "Poland", "Spain", "Croatia", "Serbia", "Slovenia", "Hungary", "Portugal", "Greece", "Italy", "Malta", "Cyprus", "Czechia", "Czech Republic", "Slovakia", "Lithuania", "Latvia", "Estonia", "Montenegro", "Bosnia", "Albania", "North Macedonia"}

def collect_dofe():
    if not DOFE_JOBS.exists():
        print("DoFE: data/jobs.json not found, skipping DoFE")
        return []
    data = json.loads(DOFE_JOBS.read_text(encoding="utf-8"))
    jobs = data.get("jobs", [])
    out = []
    canada_count = 0
    for j in jobs:
        title = (j.get("title") or "").lower()
        cat = j.get("category") or ""
        country = j.get("country") or ""
        details = j.get("details") or {}
        contract_raw = str(details.get("Contract period") or "").strip()
        try:
            months = float(contract_raw) if contract_raw else None
        except ValueError:
            months = None
        is_seasonal = False
        reason = "all DoFE"
        if months is not None and months <= 12:
            is_seasonal = True
            reason = "contract " + str(months) + " months"
        elif cat in DOFE_SEASONAL_CATEGORIES:
            is_seasonal = True
            reason = "category " + cat
        elif any(k in title for k in DOFE_SEASONAL_KEYWORDS):
            is_seasonal = True
            reason = "keyword in title"
        elif country in EUROPE_SEASONAL_COUNTRIES:
            is_seasonal = True
            reason = "Europe seasonal " + country
        # COLLECT ALL: even if not seasonal, we still keep it. kind tells app where to show it.
        kind = "Seasonal" if is_seasonal else "Foreign"
        if country in EUROPE_SEASONAL_COUNTRIES:
            region = "Europe"
        elif country in {"UAE", "Saudi Arabia", "Qatar", "Kuwait", "Oman", "Bahrain"}:
            region = "Gulf"
        else:
            region = "Foreign"
        if country == "Canada":
            region = "Foreign"
            canada_count += 1
        new_id = j.get("id") or ("dofe-" + str(j.get("lot") or j.get("title")))
        out.append({
            "id": new_id,
            "title": j.get("title") or "",
            "employer": j.get("employer") or "",
            "country": country,
            "location": j.get("location") or "",
            "category": cat or "Foreign",
            "source": "DoFE",
            "salary": j.get("salary") or "",
            "closes_on": j.get("closes_on") or "",
            "region": region,
            "kind": kind,
            "agency": j.get("ra_name") or details.get("Agency name") or "",
            "agency_phone": j.get("agency_phone") or "",
            "lot": j.get("lot") or "",
            "details": details,
            "seasonal_reason": reason,
        })
    print("DoFE: kept ALL " + str(len(out)) + " jobs from " + str(len(jobs)) + ", Canada in DoFE: " + str(canada_count))
    return out

# ---------- Helpers ----------
def norm(name):
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()

def find_col(headers, *keywords):
    for h in headers:
        n = norm(h)
        if all(k in n for k in keywords):
            return h
    return None

def decode_csv_bytes(raw):
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")

# ---------- Job Bank Canada - ENABLED, Foreign section, min 4 ----------
JOBBANK_PACKAGE = "https://open.canada.ca/data/api/3/action/package_show?id=ea639e28-c0fc-48bf-b5dd-b8899bd43072"

def collect_jobbank():
    pkg = requests.get(JOBBANK_PACKAGE, headers=UA, timeout=60).json()["result"]
    csvs = [r for r in pkg["resources"] if r.get("format", "").upper() == "CSV" and "job-postings-en" in r.get("url", "").lower()]
    if not csvs:
        raise RuntimeError("no English CSV found")
    csvs.sort(key=lambda r: r.get("last_modified") or r.get("created") or "", reverse=True)
    url = csvs[0]["url"]
    print("Job Bank CSV:", url)
    resp = requests.get(url, headers=UA, timeout=600)
    resp.raise_for_status()
    text = decode_csv_bytes(resp.content)
    first_line = text[:5000].splitlines()[0] if text else ""
    delim = "\t" if first_line.count("\t") > first_line.count(",") else ","
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delim)
    headers = [h.strip() for h in (reader.fieldnames or []) if h]
    reader.fieldnames = [h.strip() for h in (reader.fieldnames or [])]
    c_title = find_col(headers, "job", "title")
    c_id = find_col(headers, "snapshot", "id") or find_col(headers, "posting", "id")
    c_city = find_col(headers, "city")
    c_prov = find_col(headers, "province")
    c_term = find_col(headers, "employment", "term")
    c_noc = find_col(headers, "noc21", "name") or find_col(headers, "noc", "name")
    c_min = find_col(headers, "salary", "minimum")
    c_max = find_col(headers, "salary", "maximum")
    c_per = find_col(headers, "salary", "per")
    c_posted = find_col(headers, "first", "posting")
    c_edu = find_col(headers, "education")
    c_exp = find_col(headers, "experience")
    c_vac = find_col(headers, "vacancy")
    c_type = find_col(headers, "employment", "type")
    c_start = find_col(headers, "start", "date")
    c_hmin = find_col(headers, "hours", "minimum")
    c_hmax = find_col(headers, "hours", "maximum")
    c_hper = find_col(headers, "hours", "per")
    c_lang = find_col(headers, "official", "language")
    if not c_title or not c_id:
        raise RuntimeError("could not find title/id columns")
    def val(row, col):
        return (row.get(col) or "").strip() if col else ""
    def make_job(row):
        title = val(row, c_title)
        term = val(row, c_term)
        lo, hi, per = val(row, c_min), val(row, c_max), val(row, c_per)
        if lo and hi and lo != hi:
            pay = "CAD " + lo + "-" + hi + " " + per
        elif lo or hi:
            pay = "CAD " + (lo or hi) + " " + per
        else:
            pay = ""
        hmin, hmax, hper = val(row, c_hmin), val(row, c_hmax), val(row, c_hper)
        hours = ""
        if hmin or hmax:
            hours = ((hmin + "-" + hmax) if hmin and hmax and hmin != hmax else (hmin or hmax)) + " hours " + hper
        details = {k: v for k, v in {"Education required": val(row, c_edu), "Experience required": val(row, c_exp), "Language": val(row, c_lang), "Employment type": val(row, c_type), "Employment term": term, "Start date": val(row, c_start)[:10], "Hours": hours, "Vacancies": val(row, c_vac), "Posted on": val(row, c_posted)[:10]}.items() if v}
        place = ", ".join(x for x in (val(row, c_city), val(row, c_prov)) if x)
        return {"id": "jb-" + val(row, c_id), "title": title.title() if title.isupper() else title, "employer": "", "country": "Canada", "region": "Foreign", "kind": "Seasonal" if "seasonal" in term.lower() else "Foreign", "location": place, "category": "Seasonal" if "seasonal" in term.lower() else "Foreign", "source": "Job Bank Canada", "salary": pay.strip(), "closes_on": "", "details": details, "_posted": val(row, c_posted)}
    jobs = []
    all_rows = []
    rows = 0
    for row in reader:
        rows += 1
        all_rows.append(row)
        title = val(row, c_title)
        term = val(row, c_term)
        blob = title + " " + val(row, c_noc)
        if not (SEASONAL_WORDS.search(blob) or "seasonal" in term.lower()):
            continue
        jobs.append(make_job(row))
    print("Job Bank: read " + str(rows) + " rows, seasonal filtered: " + str(len(jobs)))
    # GUARANTEE at least 4 Canada jobs in Foreign section, using REAL rows from same CSV
    if len(jobs) < MIN_CANADA_JOBS:
        print("Job Bank: only " + str(len(jobs)) + " seasonal, adding fallback to reach " + str(MIN_CANADA_JOBS))
        seen_ids = {j["id"] for j in jobs}
        # Take most recent rows first, even if not seasonal, still real jobs
        all_rows_sorted = sorted(all_rows, key=lambda r: val(r, c_posted), reverse=True)
        for row in all_rows_sorted:
            if len(jobs) >= MIN_CANADA_JOBS:
                break
            j = make_job(row)
            if j["id"] in seen_ids:
                continue
            # Fallback jobs are Foreign, not Seasonal, so they show in Foreign section
            j["kind"] = "Foreign"
            j["category"] = "Foreign"
            j["seasonal_reason"] = "fallback to show Canada in Foreign"
            jobs.append(j)
            seen_ids.add(j["id"])
    jobs.sort(key=lambda j: j["_posted"], reverse=True)
    for j in jobs:
        j.pop("_posted", None)
    # Keep up to 300, but at least 4 are guaranteed above
    jobs = jobs[:300]
    print("Job Bank: final " + str(len(jobs)) + " Canada jobs, all region=Foreign")
    return jobs

# ---------- EURES ----------
EURES_SEARCH = "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search"
EURES_DETAIL = "https://europa.eu/eures/api/jv-searchengine/public/jv/id/{id}?requestLang=en"
EURES_DELAY = 1.5
PER_QUERY = 30
MAX_PAGES = 4
PAGE_SIZE = 20
MAX_PER_COUNTRY_KIND = 30
FETCH_DETAILS = True
MAX_DETAIL_CALLS = 400
CACHE = ROOT / "data" / "eures_cache.json"
DEEPL_KEY = os.environ.get("DEEPL_API_KEY", "").strip()
MONTHLY_CHAR_LIMIT = 450000
TRANSLATE_DESC_CHARS = 250
EURES_COUNTRIES = {"at": "Austria", "be": "Belgium", "bg": "Bulgaria", "hr": "Croatia", "cy": "Cyprus", "cz": "Czechia", "dk": "Denmark", "ee": "Estonia", "fi": "Finland", "fr": "France", "de": "Germany", "el": "Greece", "hu": "Hungary", "is": "Iceland", "ie": "Ireland", "it": "Italy", "lv": "Latvia", "li": "Liechtenstein", "lt": "Lithuania", "lu": "Luxembourg", "mt": "Malta", "nl": "Netherlands", "no": "Norway", "pl": "Poland", "pt": "Portugal", "ro": "Romania", "sk": "Slovakia", "si": "Slovenia", "es": "Spain", "se": "Sweden", "ch": "Switzerland"}
SEASONAL_TERMS = {"default": ["seasonal", "harvest"], "ro": ["sezonier", "seasonal", "culegator", "recoltare"], "hr": ["sezonski", "seasonal", "berba", "branje"], "pt": ["sazonal", "seasonal", "colheita", "apanha"], "pl": ["sezonowa", "seasonal", "zbior owocow", "zbiory"], "bg": ["seasonal"], "hu": ["szezonalis", "seasonal"], "cz": ["sezonni", "seasonal"], "de": ["Saisonarbeit", "Erntehelfer", "seasonal"], "at": ["Saisonarbeit", "Erntehelfer", "seasonal"], "ch": ["Saisonarbeit", "Erntehelfer", "seasonal"], "li": ["Saisonarbeit", "seasonal"], "it": ["stagionale", "seasonal", "raccolta", "bracciante"], "fi": ["kausityo", "seasonal", "poiminta"], "lu": ["saisonnier", "seasonal"], "fr": ["saisonnier", "vendanges", "cueillette", "seasonal"], "be": ["saisonnier", "seizoenswerk", "seasonal"], "nl": ["seizoenswerk", "oogst", "seasonal"], "dk": ["saesonarbejde", "seasonal"], "no": ["sesongarbeid", "seasonal"], "se": ["sasongsarbete", "seasonal"], "ee": ["hooajatoo", "seasonal"], "lv": ["sezonas darbs", "seasonal"], "lt": ["sezoninis darbas", "seasonal"], "sk": ["sezonna praca", "seasonal"], "si": ["sezonsko delo", "seasonal"], "es": ["temporada", "temporero", "recoleccion", "seasonal"], "el": ["seasonal"], "cy": ["seasonal", "harvest"]}
VISA_TERMS = ["visa sponsorship", "work permit", "relocation"]
VISA_RE = re.compile(r"visa|work permit|relocat|sponsor", re.I)
SCHEDULES = {"fulltime": "Full-time", "parttime": "Part-time", "flexitime": "Flexible hours"}
URI_RE = re.compile(r"^https?://", re.I)

def strip_html(t, limit=700):
    t = re.sub(r"<[^>]+>", " ", t or "")
    t = re.sub(r"&nbsp;|&#160;", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return (t[:limit].rsplit(" ", 1)[0] + "...") if len(t) > limit else t

def eures_post(body):
    last = None
    for attempt in range(2):
        try:
            r = requests.post(EURES_SEARCH, json=body, headers={"User-Agent": UA["User-Agent"], "Content-Type": "application/json"}, timeout=60)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(5)
    raise last

def eures_query(cc, keyword):
    global PAGE_SIZE
    items, data, page = [], {}, 1
    while len(items) < PER_QUERY and page <= MAX_PAGES:
        body = {"resultsPerPage": PAGE_SIZE, "page": page, "sortSearch": "MOST_RECENT", "keywords": [{"keyword": keyword, "specificSearchCode": "EVERYWHERE"}], "publicationPeriod": None, "occupationUris": [], "skillUris": [], "requiredExperienceCodes": [], "positionScheduleCodes": [], "sectorCodes": [], "educationAndQualificationLevelCodes": [], "positionOfferingCodes": [], "locationCodes": [cc], "euresFlagCodes": [], "otherBenefitsCodes": [], "requiredLanguages": [], "minNumberPost": None, "sessionId": "jobs-bot", "requestLanguage": "en"}
        try:
            data = eures_post(body)
        except Exception:
            if page == 1 and PAGE_SIZE > 10:
                PAGE_SIZE = 10
                continue
            raise
        got = None
        for key in ("jvs", "results", "data", "items", "vacancies"):
            if isinstance(data.get(key), list):
                got = data[key]
                break
        if not got:
            break
        items += got
        page += 1
        if len(items) < PER_QUERY:
            time.sleep(EURES_DELAY)
    return data, items[:PER_QUERY]

def walk_leaves(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk_leaves(v, (path + "." + k) if path else str(k))
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_leaves(v, path)
    elif obj is not None and not isinstance(obj, bool):
        yield path, str(obj)

def eures_details(job_id):
    r = requests.get(EURES_DETAIL.format(id=job_id), headers=UA, timeout=60)
    r.raise_for_status()
    data = r.json()
    det, desc = {}, ""
    for path, val in walk_leaves(data):
        low = path.lower()
        val = val.strip()
        if not val or URI_RE.match(val):
            continue
        val = SCHEDULES.get(val.lower(), val)
        if "description" in low and len(val) > len(desc):
            desc = val
        elif re.search(r"education|qualification", low):
            det.setdefault("Qualification", val)
        elif "experience" in low:
            det.setdefault("Experience", val)
        elif "language" in low and len(val) < 40:
            det["Language"] = (det["Language"] + ", " + val) if "Language" in det and val not in det["Language"] else det.get("Language", val)
        elif re.search(r"salary|wage|remuneration", low) and len(val) < 60:
            det.setdefault("Pay", val)
        elif re.search(r"schedule|offering|contract", low) and len(val) < 40:
            det.setdefault("Contract", val)
    if desc:
        det["Requirements and description"] = strip_html(desc, 900)
    return det

def load_cache():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_cache(cache):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

EN_STOP = {"the", "and", "with", "for", "you", "we", "to", "of", "in", "is", "are", "will", "your", "our", "a"}

def looks_english(text):
    words = re.findall(r"[a-z']+", (text or "").lower())
    if len(words) < 6:
        return False
    hits = sum(1 for w in words if w in EN_STOP)
    return hits >= max(3, len(words) * 0.12)

def short_text(t, n):
    t = (t or "").strip()
    return (t[:n].rsplit(" ", 1)[0] + "...") if len(t) > n else t

def deepl_translate(texts):
    url = "https://api-free.deepl.com/v2/translate" if DEEPL_KEY.endswith(":fx") else "https://api.deepl.com/v2/translate"
    r = requests.post(url, timeout=60, headers={"Authorization": "DeepL-Auth-Key " + DEEPL_KEY, "Content-Type": "application/json"}, json={"text": texts, "target_lang": "EN-GB"})
    r.raise_for_status()
    return [t["text"] for t in r.json()["translations"]]

def apply_translation(j, c):
    en = c.get("title_en")
    if en and en != j["title"]:
        j["details"]["Original title"] = j["title"]
        j["title"] = en
    if c.get("desc_en"):
        j["details"]["Requirements and description"] = c["desc_en"]

def translate_jobs(jobs, cache):
    pending = []
    for j in jobs:
        c = cache.setdefault(j["_eid"], {})
        desc = j["details"].get("Requirements and description", "")
        if "title_en" not in c and not looks_english(desc):
            c["orig_title"], c["orig_desc"] = j["title"], short_text(desc, TRANSLATE_DESC_CHARS)
            pending.append(j)
        elif "title_en" in c:
            apply_translation(j, c)
    if not pending:
        return
    if not DEEPL_KEY:
        print("Translation skipped: " + str(len(pending)) + " jobs stay in original language (no DEEPL_API_KEY)")
        return
    usage = cache.setdefault("_usage", {})
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    if usage.get("month") != month:
        usage.clear()
        usage.update({"month": month, "chars": 0})
    done = 0
    for i in range(0, len(pending), 20):
        batch = pending[i:i + 20]
        texts = []
        for j in batch:
            c = cache[j["_eid"]]
            texts += [c["orig_title"], c["orig_desc"] or "-"]
        if usage["chars"] + sum(len(t) for t in texts) > MONTHLY_CHAR_LIMIT:
            print("Translation stopped: monthly budget reached")
            break
        try:
            res = deepl_translate(texts)
        except Exception as e:
            print("Batch translation failed, trying job by job: " + str(e), file=sys.stderr)
            res = []
            for j in batch:
                c = cache[j["_eid"]]
                pair = [c["orig_title"], c["orig_desc"] or "-"]
                try:
                    pair = deepl_translate(pair)
                except Exception:
                    pass
                res += pair
        usage["chars"] += sum(len(t) for t in texts)
        for k, j in enumerate(batch):
            c = cache[j["_eid"]]
            c["title_en"] = res[2*k].strip() or c["orig_title"]
            c["desc_en"] = "" if c["orig_desc"] in ("", "-") else res[2*k+1].strip()
            apply_translation(j, c)
            done += 1
        time.sleep(0.5)
    print("Translated " + str(done) + " jobs; chars used: " + str(usage["chars"]))

def collect_eures():
    found = {}
    for cc, country in EURES_COUNTRIES.items():
        for kind, terms in (("Seasonal", SEASONAL_TERMS.get(cc, SEASONAL_TERMS["default"])), ("Visa sponsored", VISA_TERMS)):
            kept = 0
            for term in terms:
                if kept >= MAX_PER_COUNTRY_KIND:
                    break
                try:
                    data, items = eures_query(cc, term)
                except Exception as e:
                    print("  EURES " + cc + "/" + term + ": failed " + str(e), file=sys.stderr)
                    time.sleep(EURES_DELAY)
                    continue
                print("  " + cc + "/" + kind + "/" + term + ": " + str(len(items)) + " results")
                time.sleep(EURES_DELAY)
                for it in items:
                    jid = str(it.get("id") or "")
                    title = (it.get("title") or "").strip()
                    if not jid or not title:
                        continue
                    emp = it.get("employer")
                    employer = (emp.get("name") if isinstance(emp, dict) else emp) or it.get("employerName") or ""
                    loc = ""
                    lm = it.get("locationMap")
                    if isinstance(lm, dict):
                        for v in lm.values():
                            if isinstance(v, list) and v:
                                loc = ", ".join(str(x) for x in v[:2] if not URI_RE.match(str(x)))
                                break
                    desc = strip_html(it.get("description") or "")
                    if kind == "Visa sponsored" and desc and not VISA_RE.search(desc + " " + title):
                        continue
                    key = "eu-" + jid
                    if key in found and found[key]["kind"] == "Visa sponsored":
                        continue
                    det = {}
                    if desc:
                        det["Requirements and description"] = desc
                    posted = str(it.get("creationDate") or it.get("lastModificationDate") or "")[:10]
                    if posted:
                        det["Posted on"] = posted
                    found[key] = {"id": key, "title": title, "employer": str(employer), "country": country, "location": loc, "region": "Europe", "kind": kind, "category": "Seasonal" if kind == "Seasonal" else "Visa sponsored", "source": "EURES", "salary": "", "closes_on": "", "details": det, "_eid": jid}
                    kept += 1
                    if kept >= MAX_PER_COUNTRY_KIND:
                        break
    jobs = list(found.values())
    print("EURES: " + str(len(jobs)) + " jobs before details")
    cache = load_cache()
    calls = 0
    for j in jobs:
        c = cache.setdefault(j["_eid"], {})
        if "details" not in c and FETCH_DETAILS and calls < MAX_DETAIL_CALLS:
            calls += 1
            try:
                c["details"] = eures_details(j["_eid"])
            except Exception as e:
                print("  detail " + j["_eid"] + ": " + str(e), file=sys.stderr)
            time.sleep(EURES_DELAY)
        j["details"].update({k: v for k, v in c.get("details", {}).items() if v})
    print("EURES: fetched details for " + str(calls) + " new jobs")
    translate_jobs(jobs, cache)
    current = {j["_eid"] for j in jobs}
    cache = {k: v for k, v in cache.items() if k in current or k == "_usage"}
    save_cache(cache)
    out = []
    for j in jobs:
        j.pop("_eid", None)
        if j["kind"] == "Visa sponsored":
            blob = " ".join(str(v) for v in j["details"].values()) + " " + j["title"]
            if not VISA_RE.search(blob):
                continue
        out.append(j)
    return out

# ---------- Manual ----------
def collect_manual():
    if not MANUAL.exists():
        return []
    data = json.loads(MANUAL.read_text(encoding="utf-8"))
    out = []
    for j in data.get("jobs", []):
        if str(j.get("id", "")).startswith("example-"):
            continue
        j = dict(j)
        j.setdefault("region", "Europe")
        j.setdefault("kind", "Seasonal")
        j.setdefault("category", "Seasonal")
        j.pop("source", None)
        j.pop("source_url", None)
        out.append(j)
    return out

COLLECTORS = ([collect_jobbank] if ENABLE_CANADA else []) + [collect_dofe, collect_eures, collect_manual]

def main():
    all_jobs, ok = [], 0
    for fn in COLLECTORS:
        try:
            got = fn()
            print(fn.__name__ + ": " + str(len(got)) + " jobs")
            all_jobs += got
            ok += 1
        except Exception as e:
            print(fn.__name__ + ": FAILED " + str(e), file=sys.stderr)
    seen, unique = set(), []
    for j in all_jobs:
        key = j.get("id") or (j.get("title"), j.get("employer"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(j)
    unique.sort(key=lambda x: (x.get("closes_on") or "9999", x.get("country") or ""))
    unique = unique[:MAX_JOBS]
    if ok == 0 or not unique:
        print("Nothing collected - keeping existing seasonal.json")
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    from collections import Counter
    counts = Counter((j.get("country", "?"), j.get("kind", "?")) for j in unique)
    print("Jobs by country / kind:")
    for (country, kind), n in sorted(counts.items()):
        print("  " + country + " " + kind + " " + str(n))
    canada = [j for j in unique if j.get("country") == "Canada"]
    print("Canada total in seasonal.json: " + str(len(canada)) + " (need at least 4, all region=Foreign)")
    payload = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "count": len(unique), "canada_count": len(canada), "jobs": unique}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("Wrote " + str(len(unique)) + " jobs to " + str(OUT))

if __name__ == "__main__":
    main()
