"""Google Jobs source via SerpApi (Google has no public jobs API and blocks scraping).

Google Jobs aggregates postings from employer career sites, LinkedIn, Indeed, ZipRecruiter and local boards, so it
mainly adds local employers that only post on their own sites. SerpApi's free plan is 250 searches a month, so this
runs a small fixed number of searches per night and counts them against a monthly budget.

keys.env: SERPAPI_KEY=...
boards.json: "google_jobs": {"enabled": true, "queries": ["project manager", ...], "location": "Janesville, Wisconsin",
             "remote": true, "per_night": 8, "monthly_budget": 240}
"""
import datetime as dt
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://serpapi.com/search.json"


USAGE = Path(__file__).with_name(".serpapi_usage.json")   # a file, not the DB: the scan holds the DB write lock


def _usage(add=0):
    month = dt.date.today().strftime("%Y-%m")
    try:
        data = json.loads(USAGE.read_text())
    except (OSError, ValueError):
        data = {}
    data[month] = data.get(month, 0) + add
    if add:
        USAGE.write_text(json.dumps(data))
    return data[month]


def best_link(options):
    """Prefer the employer's own application system, then any non-aggregator site, then the first link."""
    import apply
    links = [o.get("link") or "" for o in options or [] if o.get("link")]
    for link in links:
        if apply.ats_source(link):
            return link
    for link in links:
        if not re.search(r"linkedin|indeed|ziprecruiter|glassdoor|monster|simplyhired|talent\.com|jooble|adzuna", link, re.I):
            return link
    return links[0] if links else ""


def to_job(item, remote=False):
    import apply
    ext = item.get("detected_extensions") or {}
    link = best_link(item.get("apply_options"))
    loc = (item.get("location") or "").strip()
    if remote or ext.get("work_from_home"):
        loc = f"Remote - United States ({loc})" if loc and "anywhere" not in loc.lower() else "Remote - United States"
    job = dict(company=item.get("company_name") or "", title=item.get("title") or "", location=loc,
               description=item.get("description") or "", url=link or item.get("share_link") or "", apply_url=link,
               source="google", token="", job_id=str(item.get("job_id") or "")[:120], salary=ext.get("salary") or "",
               posted_at=_posted(ext.get("posted_at")))
    if link and apply.ats_source(link):
        job = apply.promote_remote_board(job)
    return job


def _posted(text):
    m = re.match(r"(\d+)\s+(hour|day|week|month)", text or "")
    if not m:
        return ""
    n, unit = int(m.group(1)), m.group(2)
    days = {"hour": 0, "day": n, "week": 7 * n, "month": 30 * n}[unit]
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()


def fetch(cfg, keys, log=print):
    key = keys.get("SERPAPI_KEY")
    if not key:
        return
    budget, per_night = cfg.get("monthly_budget", 240), cfg.get("per_night", 8)
    searches = []
    for q in cfg.get("queries") or ["project manager", "program manager", "operations manager", "implementation manager"]:
        searches.append((q, cfg.get("location", "Janesville, Wisconsin"), False))
        if cfg.get("remote", True):
            searches.append((f"{q} remote", "United States", True))
    start = dt.date.today().toordinal() % max(1, len(searches))   # rotate so every query gets its turn across nights
    searches = searches[start:] + searches[:start]
    searches = sorted(searches, key=lambda s: not s[2])[:per_night]   # remote searches first: remote is the priority
    for q, where, remote in searches:
        if _usage() >= budget:
            log(f"google jobs: monthly budget of {budget} searches used")
            return
        params = {"engine": "google_jobs", "q": q, "location": where, "hl": "en", "gl": "us", "api_key": key}
        if remote:
            params["ltype"] = "1"
        try:
            with urllib.request.urlopen(API + "?" + urllib.parse.urlencode(params), timeout=40) as r:
                data = json.load(r)
        except Exception as exc:
            log(f"fetch failed google jobs '{q}': {str(exc)[:120]}")
            continue
        finally:
            _usage(add=1)
        for item in data.get("jobs_results") or []:
            job = to_job(item, remote)
            if job["url"] and job["title"]:
                yield job
