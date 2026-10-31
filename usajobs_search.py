#!/usr/bin/env python3
"""USAJOBS search -> usajobs_matches.csv. Needs USAJOBS_API_KEY and USAJOBS_EMAIL in env or .env."""
import csv, json, os, sys, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).parent
for line in (HERE / ".env").read_text().splitlines() if (HERE / ".env").exists() else []:
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip().strip('"'))
KEY, EMAIL = os.environ.get("USAJOBS_API_KEY"), os.environ.get("USAJOBS_EMAIL")
if not (KEY and EMAIL):
    sys.exit("Set USAJOBS_API_KEY and USAJOBS_EMAIL in .env")

SERIES = "2210;0343;0340;1102;0301"  # IT, mgmt/program analyst, program mgmt, contracting, misc admin/program
SEARCHES = [
    {"JobCategoryCode": SERIES, "RemoteIndicator": "True"},
    {"JobCategoryCode": SERIES, "LocationName": "Anywhere in the U.S. (remote job)"},
]
try:  # local searches: profile.json "usajobs": {"locations": ["City, State", ...]}
    _locs = json.loads((Path(__file__).with_name("profile.json")).read_text()).get("usajobs", {}).get("locations", [])
except Exception:
    _locs = []
SEARCHES += [{"JobCategoryCode": SERIES, "LocationName": loc, "Radius": "40"} for loc in _locs]
COMMON = {"PayGradeLow": "07", "PayGradeHigh": "13", "HiringPath": "public",
          "DatePosted": "30", "ResultsPerPage": "500", "SortField": "OpenDate", "SortDirection": "Desc"}

def fetch(params):
    url = "https://data.usajobs.gov/api/search?" + urllib.parse.urlencode(params, safe=";")
    req = urllib.request.Request(url, headers={"Host": "data.usajobs.gov", "User-Agent": EMAIL, "Authorization-Key": KEY})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

rows, seen = [], set()
for s in SEARCHES:
    page = 1
    while True:
        data = fetch({**COMMON, **s, "Page": str(page)})["SearchResult"]
        for it in data.get("SearchResultItems", []):
            d = it["MatchedObjectDescriptor"]
            jid = it.get("MatchedObjectId")
            if jid in seen: continue
            seen.add(jid)
            plan = (d.get("JobGrade") or [{}])[0].get("Code", "")
            if plan not in ("GS", "GG", "GL") or "billet" in d.get("PositionTitle", "").lower(): continue
            pay = (d.get("PositionRemuneration") or [{}])[0]
            det = d.get("UserArea", {}).get("Details", {})
            rows.append({
                "id": jid, "title": d.get("PositionTitle"), "agency": d.get("OrganizationName"),
                "dept": d.get("DepartmentName"), "location": d.get("PositionLocationDisplay"),
                "remote": det.get("RemoteIndicator"), "grade": f"{det.get('LowGrade','')}-{det.get('HighGrade','')}",
                "series": ";".join(c.get("Code", "") for c in d.get("JobCategory", [])),
                "pay": f"{pay.get('MinimumRange','')}-{pay.get('MaximumRange','')} {pay.get('RateIntervalCode','')}",
                "opens": d.get("PublicationStartDate", "")[:10], "closes": d.get("ApplicationCloseDate", "")[:10],
                "search": s.get("LocationName", "Remote"), "url": d.get("PositionURI"),
            })
        if page >= int(data.get("UserArea", {}).get("NumberOfPages", 1) or 1): break
        page += 1

out = HERE / "usajobs_matches.csv"
with out.open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["id"]); w.writeheader(); w.writerows(rows)
print(f"{len(rows)} jobs -> {out}")
