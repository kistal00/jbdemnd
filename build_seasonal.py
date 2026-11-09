"""
Builds data/seasonal.json once a day (run by GitHub Actions at 04:00 Nepal time).
The app only reads that file, so everyone sees the same data all day.

Each "collector" returns a list of dicts with these keys:
  id, title, employer, country, location, category, source, salary, closes_on
(source_url is intentionally NOT included - the app never shows original links.)

A collector that fails is logged and skipped, so one broken site never wipes the file.
"""
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
ENABLE_CANADA = False   # Canadian jobs are not shown in the app yet; set True to collect them
MAX_JOBS = 2000                     # keeps the file small for the app
UA = {"User-Agent": f"job-demand-dashboard (contact: {os.environ.get('CONTACT_EMAIL', 'kistal.gamer2020@gmail.com')})"}

SEASONAL_WORDS = re.compile(
    r"seasonal|harvest|picker|picking|farm|agricultur|greenhouse|orchard|horticultur|"
    r"fruit|berry|vegetable|packing|livestock|dairy|poultry|vineyard|forestry",
    re.I,
)


def norm(name):
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def pick(row, *names):
    """First non-empty column among several possible names (ignores case and punctuation)."""
    lowered = {norm(k): v for k, v in row.items() if k}
    for n in names:
        v = lowered.get(norm(n))
        if v and str(v).strip():
            return str(v).strip()
    return ""


def find_col(headers, *keywords):
    """First header containing ALL keywords (after normalising)."""
    for h in headers:
        n = norm(h)
        if all(k in n for k in keywords):
            return h
    return None


# ---------------------------------------------------------------------
# 1) Job Bank Canada - official open data (Open Government Licence - Canada)
#    Monthly CSV of postings published on open.canada.ca.
# ---------------------------------------------------------------------
JOBBANK_PACKAGE = "https://open.canada.ca/data/api/3/action/package_show?id=ea639e28-c0fc-48bf-b5dd-b8899bd43072"


def decode_csv_bytes(raw):
    """Job Bank files are UTF-16 with tab separators; handle normal UTF-8 CSVs too."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def collect_jobbank():
    pkg = requests.get(JOBBANK_PACKAGE, headers=UA, timeout=60).json()["result"]
    csvs = [
        r for r in pkg["resources"]
        if r.get("format", "").upper() == "CSV" and "job-postings-en" in r.get("url", "").lower()
    ]
    if not csvs:
        raise RuntimeError("no English CSV found in Job Bank dataset")
    csvs.sort(key=lambda r: r.get("last_modified") or r.get("created") or "", reverse=True)
    url = csvs[0]["url"]
    print("Job Bank CSV:", url)

    resp = requests.get(url, headers=UA, timeout=600)
    resp.raise_for_status()
    text = decode_csv_bytes(resp.content)
    del resp
    first_line = text[:5000].splitlines()[0] if text else ""
    delim = "\t" if first_line.count("\t") > first_line.count(",") else ","
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delim)
    headers = [h.strip() for h in (reader.fieldnames or []) if h]
    reader.fieldnames = [h.strip() for h in (reader.fieldnames or [])]
    print("Job Bank columns:", headers)

    c_title = find_col(headers, "job", "title")
    c_id = find_col(headers, "snapshot", "id") or find_col(headers, "posting", "id")
    c_city = find_col(headers, "city")
    c_prov = find_col(headers, "province")
    c_term = find_col(headers, "employment", "term") if find_col(headers, "employment", "term") not in (None,) else None
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
    print("Using columns:", dict(title=c_title, id=c_id, city=c_city, province=c_prov, term=c_term,
                                  noc=c_noc, sal_min=c_min, sal_max=c_max, sal_per=c_per))
    if not c_title or not c_id:
        raise RuntimeError("could not find title/id columns in Job Bank file")

    def val(row, col):
        return (row.get(col) or "").strip() if col else ""

    jobs, rows, not_seasonal = [], 0, 0
    for row in reader:
        rows += 1
        title = val(row, c_title)
        term = val(row, c_term)
        blob = f"{title} {val(row, c_noc)}"
        if not (SEASONAL_WORDS.search(blob) or "seasonal" in term.lower()):
            not_seasonal += 1
            continue
        lo, hi, per = val(row, c_min), val(row, c_max), val(row, c_per)
        if lo and hi and lo != hi:
            pay = f"CAD {lo}-{hi} {per}".strip()
        elif lo or hi:
            pay = f"CAD {lo or hi} {per}".strip()
        else:
            pay = ""
        hmin, hmax, hper = val(row, c_hmin), val(row, c_hmax), val(row, c_hper)
        hours = ""
        if hmin or hmax:
            hours = (f"{hmin}-{hmax}" if hmin and hmax and hmin != hmax else (hmin or hmax)) + f" hours {hper}".rstrip()
        details = {
            "Education required": val(row, c_edu),
            "Experience required": val(row, c_exp),
            "Language": val(row, c_lang),
            "Employment type": val(row, c_type),
            "Employment term": term,
            "Start date": val(row, c_start)[:10],
            "Hours": hours,
            "Vacancies": val(row, c_vac),
            "Posted on": val(row, c_posted)[:10],
        }
        details = {k: v for k, v in details.items() if v}
        place = ", ".join(x for x in (val(row, c_city), val(row, c_prov)) if x)
        jobs.append({
            "id": f"jb-{val(row, c_id)}",
            "title": title.title() if title.isupper() else title,
            "employer": "",  # the open data file has no employer names
            "country": "Canada",
            "region": "Canada",
            "kind": "Seasonal",
            "location": place,
            "category": "Seasonal" if "seasonal" in term.lower() else "Agriculture / Farm",
            "source": "Job Bank Canada",
            "salary": pay,
            "closes_on": "",
            "details": details,
            "_posted": val(row, c_posted),
        })
    # newest first, seasonal-term jobs first, then cap
    jobs.sort(key=lambda j: (j["category"] != "Seasonal", j["_posted"]), reverse=False)
    jobs.sort(key=lambda j: j["_posted"], reverse=True)
    jobs.sort(key=lambda j: j["category"] != "Seasonal")
    for j in jobs:
        j.pop("_posted", None)
    jobs = jobs[:300]
    print(f"Job Bank: read {rows} rows, kept {len(jobs)}, {not_seasonal} not seasonal")
    return jobs


# ---------------------------------------------------------------------
# 2) EURES - the EU's official job mobility portal (public employment services of
#    EU countries). One collector covers most of your country list.
#    NOT covered by EURES: Serbia, Albania, United Kingdom.
#    NOTE: this uses EURES' public search service (community-documented, not an official
#    partner API). Keep volumes low, and check the EURES terms of use before you publish.
# ---------------------------------------------------------------------
EURES_SEARCH = "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search"
EURES_DETAIL = "https://europa.eu/eures/api/jv-searchengine/public/jv/id/{id}?requestLang=en"
EURES_DELAY = 1.5            # seconds between requests (be polite)
# ---- LIMITS (Europe jobs). To REMOVE the 30-job limit raise ALL THREE numbers below, e.g. 1000 / 1000 / 50.
# More jobs = bigger seasonal.json (about 1 KB per job), more detail requests, and more translation characters.
PER_QUERY = 30               # jobs kept per country per search word
MAX_PAGES = 4                # result pages read per search word (20 jobs per page)
PAGE_SIZE = 20               # results asked per request (falls back to 10 if EURES refuses)
MAX_PER_COUNTRY_KIND = 30   # jobs kept per country per type (Seasonal / Visa sponsored)
FETCH_DETAILS = True
MAX_DETAIL_CALLS = 400      # new jobs only; details are remembered in data/eures_cache.json
CACHE = ROOT / "data" / "eures_cache.json"
DEEPL_KEY = os.environ.get("DEEPL_API_KEY", "").strip()
MONTHLY_CHAR_LIMIT = 450_000  # DeepL Free allows 500,000 characters a month
TRANSLATE_DESC_CHARS = 250

# EURES country code -> name shown in the app
EURES_COUNTRIES = {   # every country EURES covers (EU + Iceland, Liechtenstein, Norway, Switzerland)
    "at": "Austria", "be": "Belgium", "bg": "Bulgaria", "hr": "Croatia", "cy": "Cyprus",
    "cz": "Czechia", "dk": "Denmark", "ee": "Estonia", "fi": "Finland", "fr": "France",
    "de": "Germany", "el": "Greece", "hu": "Hungary", "is": "Iceland", "ie": "Ireland",
    "it": "Italy", "lv": "Latvia", "li": "Liechtenstein", "lt": "Lithuania", "lu": "Luxembourg",
    "mt": "Malta", "nl": "Netherlands", "no": "Norway", "pl": "Poland", "pt": "Portugal",
    "ro": "Romania", "sk": "Slovakia", "si": "Slovenia", "es": "Spain", "se": "Sweden",
    "ch": "Switzerland",
}
SEASONAL_TERMS = {        # search words per country: "seasonal" plus typical harvest/picking words in the local language
    "default": ["seasonal", "harvest"],
    "ro": ["sezonier", "seasonal", "culegător", "recoltare"],
    "hr": ["sezonski", "seasonal", "berba", "branje"],
    "pt": ["sazonal", "seasonal", "colheita", "apanha"],
    "pl": ["sezonowa", "seasonal", "zbiór owoców", "zbiory"],
    "bg": ["сезонна", "seasonal", "беритба"],
    "hu": ["szezonális", "seasonal", "szüret"],
    "cz": ["sezónní", "seasonal", "sběr ovoce", "sklizeň"],
    "de": ["Saisonarbeit", "Erntehelfer", "seasonal"],
    "at": ["Saisonarbeit", "Erntehelfer", "seasonal"],
    "ch": ["Saisonarbeit", "Erntehelfer", "seasonal"],
    "li": ["Saisonarbeit", "seasonal"],
    "it": ["stagionale", "seasonal", "raccolta", "bracciante"],
    "fi": ["kausityö", "seasonal", "poiminta"],
    "lu": ["saisonnier", "seasonal"],
    "fr": ["saisonnier", "vendanges", "cueillette", "seasonal"],
    "be": ["saisonnier", "seizoenswerk", "seasonal"],
    "nl": ["seizoenswerk", "oogst", "seasonal"],
    "dk": ["sæsonarbejde", "frugtplukker", "seasonal"],
    "no": ["sesongarbeid", "bærplukker", "seasonal"],
    "se": ["säsongsarbete", "plockare", "seasonal"],
    "ee": ["hooajatöö", "seasonal"],
    "lv": ["sezonas darbs", "seasonal"],
    "lt": ["sezoninis darbas", "seasonal"],
    "sk": ["sezónna práca", "zber ovocia", "seasonal"],
    "si": ["sezonsko delo", "trgatev", "seasonal"],
    "es": ["temporada", "temporero", "recolección", "seasonal"],
    "el": ["εποχιακή", "συγκομιδή", "seasonal"],
    "cy": ["εποχιακή", "seasonal", "harvest"],
}
VISA_TERMS = ["visa sponsorship", "work permit", "relocation"]
VISA_RE = re.compile(r"visa|work permit|relocat|sponsor", re.I)
SCHEDULES = {"fulltime": "Full-time", "parttime": "Part-time", "flexitime": "Flexible hours"}
URI_RE = re.compile(r"^https?://", re.I)


def strip_html(t, limit=700):
    t = re.sub(r"<[^>]+>", " ", t or "")
    t = re.sub(r"&nbsp;|&#160;", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return (t[:limit].rsplit(" ", 1)[0] + "…") if len(t) > limit else t


def eures_post(body):
    last = None
    for attempt in range(2):
        try:
            r = requests.post(EURES_SEARCH, json=body, headers={**UA, "Content-Type": "application/json"}, timeout=60)
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
        body = {
            "resultsPerPage": PAGE_SIZE, "page": page, "sortSearch": "MOST_RECENT",
            "keywords": [{"keyword": keyword, "specificSearchCode": "EVERYWHERE"}],
            "publicationPeriod": None, "occupationUris": [], "skillUris": [],
            "requiredExperienceCodes": [], "positionScheduleCodes": [], "sectorCodes": [],
            "educationAndQualificationLevelCodes": [], "positionOfferingCodes": [],
            "locationCodes": [cc], "euresFlagCodes": [], "otherBenefitsCodes": [],
            "requiredLanguages": [], "minNumberPost": None,
            "sessionId": "jobs-bot", "requestLanguage": "en",
        }
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
    """Yield (key path, string value) for every simple value inside a JSON object."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk_leaves(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_leaves(v, path)
    elif obj is not None and not isinstance(obj, bool):
        yield path, str(obj)


def eures_details(job_id):
    """Qualification / requirement style fields from the vacancy detail (shape is guessed; see log)."""
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
    return (t[:n].rsplit(" ", 1)[0] + "…") if len(t) > n else t


def deepl_translate(texts):
    url = "https://api-free.deepl.com/v2/translate" if DEEPL_KEY.endswith(":fx") else "https://api.deepl.com/v2/translate"
    r = requests.post(
        url, timeout=60,
        headers={"Authorization": f"DeepL-Auth-Key {DEEPL_KEY}", "Content-Type": "application/json"},
        json={"text": texts, "target_lang": "EN-GB"},
    )
    r.raise_for_status()
    return [t["text"] for t in r.json()["translations"]]


def translate_jobs(jobs, cache):
    """Translate titles and short descriptions to English. Each job is translated once and remembered."""
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
        print(f"Translation skipped: {len(pending)} jobs stay in their original language (no DEEPL_API_KEY set)")
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
            print("Translation stopped: monthly character budget reached")
            break
        try:
            res = deepl_translate(texts)
        except Exception as e:
            print(f"Batch translation failed ({type(e).__name__}: {e}); trying job by job", file=sys.stderr)
            res = []
            for j in batch:
                c = cache[j["_eid"]]
                pair = [c["orig_title"], c["orig_desc"] or "-"]
                try:
                    pair = deepl_translate(pair)
                except Exception:
                    pass          # language not supported: keep the original text
                res += pair
        usage["chars"] += sum(len(t) for t in texts)
        for k, j in enumerate(batch):
            c = cache[j["_eid"]]
            c["title_en"] = res[2 * k].strip() or c["orig_title"]
            c["desc_en"] = "" if c["orig_desc"] in ("", "-") else res[2 * k + 1].strip()
            apply_translation(j, c)
            done += 1
        time.sleep(0.5)
    print(f"Translated {done} jobs; characters used this month: {usage['chars']}")


def apply_translation(j, c):
    en = c.get("title_en")
    if en and en != j["title"]:
        j["details"]["Original title"] = j["title"]
        j["title"] = en
    if c.get("desc_en"):
        j["details"]["Requirements and description"] = c["desc_en"]


def collect_eures():
    found = {}          # eures id -> job dict (visa wins over seasonal)
    printed = False
    for cc, country in EURES_COUNTRIES.items():
        for kind, terms in (("Seasonal", SEASONAL_TERMS.get(cc, SEASONAL_TERMS["default"])), ("Visa sponsored", VISA_TERMS)):
            kept = 0
            for term in terms:
                if kept >= MAX_PER_COUNTRY_KIND:
                    break
                try:
                    data, items = eures_query(cc, term)
                except Exception as e:
                    print(f"  EURES {cc}/{term}: failed {type(e).__name__}: {e}", file=sys.stderr)
                    time.sleep(EURES_DELAY)
                    continue
                print(f"  {cc}/{kind}/'{term}': {len(items)} results")
                if not printed:
                    printed = True
                    print("EURES response keys:", list(data.keys())[:12])
                    if items:
                        print("EURES first item keys:", list(items[0].keys())[:25])
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
                    key = f"eu-{jid}"
                    if key in found and found[key]["kind"] == "Visa sponsored":
                        continue
                    det = {}
                    if desc:
                        det["Requirements and description"] = desc
                    posted = str(it.get("creationDate") or it.get("lastModificationDate") or "")[:10]
                    if posted:
                        det["Posted on"] = posted
                    found[key] = {
                        "id": key, "title": title, "employer": str(employer), "country": country,
                        "location": loc, "region": "Europe", "kind": kind, "salary": "",
                        "closes_on": "", "details": det, "_eid": jid,
                    }
                    kept += 1
                    if kept >= MAX_PER_COUNTRY_KIND:
                        break
    jobs = list(found.values())
    print(f"EURES: {len(jobs)} jobs found before details")

    cache = load_cache()
    calls = 0
    for j in jobs:
        c = cache.setdefault(j["_eid"], {})
        if "details" not in c and FETCH_DETAILS and calls < MAX_DETAIL_CALLS:
            calls += 1
            try:
                c["details"] = eures_details(j["_eid"])
                if calls == 1:
                    print("EURES detail fields kept:", list(c["details"].keys()))
            except Exception as e:
                print(f"  detail {j['_eid']}: {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(EURES_DELAY)
        j["details"].update({k: v for k, v in c.get("details", {}).items() if v})
    print(f"EURES: fetched details for {calls} new jobs")

    translate_jobs(jobs, cache)

    # keep the cache small: forget jobs that are no longer listed
    current = {j["_eid"] for j in jobs}
    cache = {k: v for k, v in cache.items() if k in current or k == "_usage"}
    save_cache(cache)

    out = []
    for j in jobs:
        j.pop("_eid", None)
        if j["kind"] == "Visa sponsored":
            blob = " ".join(str(v) for v in j["details"].values()) + " " + j["title"]
            if not VISA_RE.search(blob):
                continue          # a "visa" hit that is not confirmed by the text
        out.append(j)
    return out


# ---------------------------------------------------------------------
# 4) Manual entries - anything you copy from SEEK, Harvest Trail, etc.
#    Edit data/manual_seasonal.json directly on GitHub; it is merged in daily.
# ---------------------------------------------------------------------
def collect_manual():
    """Hand-added listings. Defaults: region = Europe, kind = Seasonal. Ids starting with 'example-' are ignored."""
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
        j.pop("source", None)       # sources are never shown in the app
        j.pop("source_url", None)
        out.append(j)
    return out


COLLECTORS = ([collect_jobbank] if ENABLE_CANADA else []) + [collect_eures, collect_manual]


def main():
    all_jobs, ok = [], 0
    for fn in COLLECTORS:
        try:
            got = fn()
            print(f"{fn.__name__}: {len(got)} jobs")
            all_jobs += got
            ok += 1
        except NotImplementedError as e:
            print(f"{fn.__name__}: skipped ({e})")
        except Exception as e:  # never let one site break the run
            print(f"{fn.__name__}: FAILED {type(e).__name__}: {e}", file=sys.stderr)

    # de-duplicate by id
    seen, unique = set(), []
    for j in all_jobs:
        key = j.get("id") or (j.get("title"), j.get("employer"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(j)
    unique = unique[:MAX_JOBS]

    if ok == 0 or not unique:
        print("Nothing collected - keeping the existing seasonal.json")
        return

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "jobs": unique}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    from collections import Counter
    counts = Counter((j.get("country", "?"), j.get("kind", "?")) for j in unique)
    print("Jobs by country / kind:")
    for (country, kind), n in sorted(counts.items()):
        print(f"  {country:12} {kind:15} {n}")
    print(f"Wrote {len(unique)} jobs to {OUT}")


if __name__ == "__main__":
    main()
