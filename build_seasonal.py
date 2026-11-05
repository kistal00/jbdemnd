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
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "seasonal.json"
MANUAL = ROOT / "data" / "manual_seasonal.json"
MAX_JOBS = 600                     # keeps the file small for the app
UA = {"User-Agent": f"job-demand-dashboard (contact: {os.environ.get('CONTACT_EMAIL', 'not-provided')})"}

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
# 2) Job Market Finland (Tyomarkkinatori)
#    The OFFICIAL retrieval API needs credentials from KEHA Centre (terms of use).
#    Fill in once you have them, then remove the NotImplementedError.
# ---------------------------------------------------------------------
def collect_finland():
    raise NotImplementedError(
        "Needs official Job Market Finland API credentials (apply to KEHA Centre) - see README"
    )


# ---------------------------------------------------------------------
# 3) EURES - add once you've confirmed the access terms / endpoint you will use.
# ---------------------------------------------------------------------
def collect_eures():
    raise NotImplementedError("EURES collector not configured yet - see README")


# ---------------------------------------------------------------------
# 4) Manual entries - anything you copy from SEEK, Harvest Trail, etc.
#    Edit data/manual_seasonal.json directly on GitHub; it is merged in daily.
# ---------------------------------------------------------------------
def collect_manual():
    if not MANUAL.exists():
        return []
    data = json.loads(MANUAL.read_text(encoding="utf-8"))
    return [j for j in data.get("jobs", []) if not str(j.get("id", "")).startswith("manual-001")]


COLLECTORS = [collect_jobbank, collect_finland, collect_eures, collect_manual]


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
    print(f"Wrote {len(unique)} jobs to {OUT}")


if __name__ == "__main__":
    main()
