"""
Builds data/seasonal.json once a day (run by GitHub Actions at 04:00 Nepal time).
The app only reads that file, so everyone sees the same data all day.

Each "collector" returns a list of dicts with these keys:
  id, title, employer, country, location, category, source, salary, closes_on
(source_url is intentionally NOT included - the app never shows original links.)

A collector that fails is logged and skipped, so one broken site never wipes the file.
"""
import csv
import hashlib
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


def collect_jobbank():
    pkg = requests.get(JOBBANK_PACKAGE, headers=UA, timeout=60).json()["result"]
    csvs = [
        r for r in pkg["resources"]
        if r.get("format", "").upper() == "CSV" and "-en-" in r.get("url", "").lower()
        or (r.get("format", "").upper() == "CSV" and "job-postings-en" in r.get("url", "").lower())
    ]
    if not csvs:
        raise RuntimeError("no English CSV found in Job Bank dataset")
    # newest first (the dataset lists newest first; fall back to last_modified)
    csvs.sort(key=lambda r: r.get("last_modified") or r.get("created") or "", reverse=True)
    url = csvs[0]["url"]
    print("Job Bank CSV:", url)

    jobs = []
    rows = no_id = no_text = 0
    with requests.get(url, headers=UA, timeout=300, stream=True) as resp:
        resp.raise_for_status()
        lines = (ln.decode("utf-8-sig", errors="replace") for ln in resp.iter_lines())
        reader = csv.DictReader(lines)
        headers = [h for h in (reader.fieldnames or []) if h]
        print("Job Bank columns:", headers)

        c_title = find_col(headers, "title") or find_col(headers, "job")
        c_id = find_col(headers, "id") or find_col(headers, "number") or find_col(headers, "reference")
        c_emp = find_col(headers, "employer")
        c_city = find_col(headers, "city") or find_col(headers, "location")
        c_wage = find_col(headers, "wage") or find_col(headers, "salary")
        c_exp = find_col(headers, "expir") or find_col(headers, "clos") or find_col(headers, "end")
        c_noc = find_col(headers, "noc") or find_col(headers, "occupation")
        print("Using columns:", dict(title=c_title, id=c_id, employer=c_emp, city=c_city,
                                      wage=c_wage, expiry=c_exp, noc=c_noc))

        for row in reader:
            rows += 1
            if rows == 1:
                print("First row sample:", {k: (v or "")[:60] for k, v in row.items() if k})
            title = (row.get(c_title) or "").strip() if c_title else ""
            blob = title + " " + ((row.get(c_noc) or "") if c_noc else "")
            if not SEASONAL_WORDS.search(blob):
                no_text += 1
                continue
            ref = (row.get(c_id) or "").strip() if c_id else ""
            if not ref:  # no id column: build a stable one from the row
                ref = hashlib.md5(f"{title}|{row.get(c_emp)}|{row.get(c_city)}".encode()).hexdigest()[:10]
                no_id += 1
            wage = (row.get(c_wage) or "").strip() if c_wage else ""
            jobs.append({
                "id": f"jb-{ref}",
                "title": title,
                "employer": (row.get(c_emp) or "").strip() if c_emp else "",
                "country": "Canada",
                "location": (row.get(c_city) or "").strip() if c_city else "",
                "category": "Agriculture / Seasonal",
                "source": "Job Bank Canada",
                "salary": wage,
                "closes_on": ((row.get(c_exp) or "").strip()[:10]) if c_exp else "",
            })
            if len(jobs) >= 300:
                break
    print(f"Job Bank: read {rows} rows, {len(jobs)} matched, {no_text} not seasonal, {no_id} without id")
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
