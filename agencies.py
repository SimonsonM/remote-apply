"""Staffing-agency job sources: Robert Half, Michael Page and Adecco.

Discovery is plain HTTP (server-rendered search pages for Robert Half and Michael Page,
Adecco's public search API). Agency jobs skip the per-company caps: instead only the top
`agency_daily_cap` matches per agency per day (default 2, by fit) are worked. Each agency's
apply flow is account/email gated, so those picks are queued for the user with a prepared
cover letter rather than submitted blind.

boards.json:
  "agencies": {"robert_half": {"enabled": true}, "michael_page": {"enabled": true},
               "adecco": {"enabled": true}, "keywords": [...], "pages": 2}
"""
import datetime as dt
import html
import json
import re
import urllib.parse
import urllib.request

SOURCES = {"roberthalf": "Robert Half", "michaelpage": "Michael Page", "adecco": "Adecco"}
CFG_KEYS = {"robert_half": "roberthalf", "michael_page": "michaelpage", "adecco": "adecco"}
KEYWORDS = ("program manager", "project manager", "technical program manager", "implementation manager",
            "operations manager", "AI operations", "IT project manager", "marketing manager")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"}
LEAD_PREFIX = "AGENCY LEAD"


def _get(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _text(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def _slug(keyword):
    return re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")


def _location(place, remote=False, hybrid=False):
    place = place or ""
    if remote:
        return f"Remote - United States ({place})" if place else "Remote - United States"
    return f"Hybrid - {place}" if hybrid else place


def _job(source, title, location, url, description="", salary="", posted_at="", job_id=""):
    return dict(company=SOURCES[source], title=title.strip(), location=location, description=description,
                url=url, source=source, token="", job_id=job_id, apply_url=url, salary=salary, posted_at=posted_at)


def _money(value):
    value = float(value)
    return f"{value:,.2f}" if value < 1000 else f"{value:,.0f}"


def _pay(low, high, period):
    if not low and not high:
        return ""
    unit = "per hour" if re.search(r"hour", period or "", re.I) else "per year"
    low, high = low or high, high or low
    return f"${_money(low)} - ${_money(high)} {unit}"


# ---------------------------------------------------------------- Robert Half
RH_CARD = re.compile(r"<rhcl-job-card[^>]*job-id=\"([^\"]+)\".*?</rhcl-job-card>", re.S)


def parse_roberthalf(page):
    for match in RH_CARD.finditer(page):
        card = match.group(0)
        link = re.search(r'<a href="([^"]+)"[^>]*slot="headline"[^>]*>(.*?)</a>', card, re.S)
        if not link:
            continue
        slot = lambda name: _text((re.search(rf'data-subslot="{name}">(.*?)</(?:li|span)>', card, re.S) or [None, ""])[1])
        site = slot("worksite").lower()
        period = slot("salary-period")
        try:
            pay = _pay(float(slot("salary-min") or 0), float(slot("salary-max") or 0), period)
        except ValueError:
            pay = ""
        desc = " ".join(x for x in (slot("type"), slot("copy")) if x)
        yield _job("roberthalf", _text(link.group(2)), _location(slot("location"), "remote" in site, "hybrid" in site),
                   link.group(1), desc, pay, job_id=match.group(1))


def fetch_roberthalf(cfg, keywords):
    for kw in keywords:
        for page in range(1, cfg.get("pages", 2) + 1):
            url = f"https://www.roberthalf.com/us/en/jobs/all/{_slug(kw)}" + (f"?pagenumber={page}" if page > 1 else "")
            try:
                jobs = list(parse_roberthalf(_get(url)))
            except Exception:
                break
            yield from jobs
            if len(jobs) < 25:
                break


# ---------------------------------------------------------------- Michael Page
MP_TILE = re.compile(r'<div about="(/job-detail/[^"]+)" class="job-tile.*?(?=<li class="views-row"|\Z)', re.S)


def parse_michaelpage(page):
    for match in MP_TILE.finditer(page):
        tile = match.group(0)
        get = lambda cls: _text((re.search(rf'class="{cls}[^"]*">(.*?)</div>', tile, re.S) or [None, ""])[1])
        title = _text((re.search(r"<h3>(.*?)</h3>", tile, re.S) or [None, ""])[1])
        if not title:
            continue
        nature = get("job-nature").lower()
        salary = get("job-salary")
        salary = re.sub(r"USD\s*", "$", salary)
        desc = " ".join(x for x in (get("job-contract-type"), _text((re.search(r'job-summary">(.*?)<div class="job-links"', tile[:8000], re.S) or [None, ""])[1])) if x)
        ref = re.search(r"/ref/([\w-]+)", match.group(1))
        yield _job("michaelpage", title, _location(get("job-location"), "remote" in nature, "hybrid" in nature),
                   "https://www.michaelpage.com" + match.group(1), desc, salary, job_id=ref.group(1) if ref else "")


def fetch_michaelpage(cfg, keywords):
    for kw in keywords:
        for page in range(cfg.get("pages", 2)):
            url = f"https://www.michaelpage.com/jobs/{_slug(kw)}" + (f"?page={page}" if page else "")
            try:
                jobs = list(parse_michaelpage(_get(url)))
            except Exception:
                break   # no results page for this keyword (404): try the next one
            yield from jobs
            if len(jobs) < 20:
                break


# ---------------------------------------------------------------- Adecco
ADECCO_SEARCH = "https://www.adecco.com/api/data/jobs/summarized"
ADECCO_DETAIL = "https://www.adecco.com/api/data/jobs/job-description-details/{}/adecco/US/en-US/job-details"


def adecco_job(item, description=""):
    title = item.get("jobTitle") or ""
    url = f"https://www.adecco.com/en-us/job-search/{_slug(title + ' ' + (item.get('jobLocation') or ''))}/{(item.get('jobId') or '').lower()}"
    pay = _pay(item.get("minsalary"), item.get("maxsalary"), item.get("salaryTimeScale") or "")
    desc = " ".join(x for x in (item.get("contractTypeTitle") or "", description) if x)
    return _job("adecco", title, _location(item.get("jobLocation"), bool(item.get("isRemote"))), url, desc, pay,
                (item.get("postedDate") or "")[:10], item.get("jobId") or "")


def _adecco_description(job_id):
    try:
        data = json.loads(_get(ADECCO_DETAIL.format(urllib.parse.quote(job_id))))
    except Exception:
        return ""
    texts = [v for v in data.values() if isinstance(v, str) and len(v) > 200]
    return _text(max(texts, key=len)) if texts else ""


def fetch_adecco(cfg, keywords, wanted=None):
    for kw in keywords:
        for page in range(cfg.get("pages", 2)):
            body = {"queryString": f"&q={kw}&sort=score desc", "filtersToDisplay": "", "range": page * 10,
                    "siteName": "adecco", "brand": "adecco", "countryCode": "US", "languageCode": "en-US"}
            data = json.loads(_get(ADECCO_SEARCH, json.dumps(body).encode(), {"Content-Type": "text/plain;charset=UTF-8"}))
            items = data.get("jobs") or []
            for item in items:
                desc = _adecco_description(item["jobId"]) if wanted and wanted(item.get("jobTitle") or "") else ""
                yield adecco_job(item, desc)
            if len(items) < 10:
                break


FETCHERS = {"roberthalf": fetch_roberthalf, "michaelpage": fetch_michaelpage, "adecco": fetch_adecco}


def fetch(boards, log=print, wanted=None):
    """Yield agency jobs for every enabled agency in boards['agencies'] (de-duplicated by URL)."""
    cfg = boards.get("agencies") or {}
    keywords = cfg.get("keywords") or KEYWORDS
    seen = set()
    for key, source in CFG_KEYS.items():
        sub = cfg.get(key) or {}
        if not sub.get("enabled"):
            continue
        sub = {"pages": cfg.get("pages", 2), **sub}
        try:
            args = (sub, keywords, wanted) if source == "adecco" else (sub, keywords)
            for job in FETCHERS[source](*args):
                if job["url"] not in seen:
                    seen.add(job["url"])
                    yield job
        except Exception as exc:
            log(f"fetch failed {SOURCES[source]}: {str(exc)[:120]}")


# ---------------------------------------------------------------- daily cap
def lead_note(day=None):
    return f"{LEAD_PREFIX} {(day or dt.date.today()).isoformat()}"


def picked_today(conn, source, day=None):
    """Agency jobs already worked today (queued as a lead or submitted)."""
    day = (day or dt.date.today()).isoformat()
    return conn.execute("SELECT count(*) FROM jobs WHERE source=? AND (notes LIKE ? OR "
                        "(status IN ('applied','unconfirmed') AND applied_at=?))",
                        (source, f"{LEAD_PREFIX} {day}%", day)).fetchone()[0]


def daily_picks(candidates, conn, cap=2, day=None):
    """Top `cap` candidates per agency for the day, best fit first, net of what was already worked today."""
    left = {s: max(0, cap - picked_today(conn, s, day)) for s in SOURCES}
    picks = []
    for job in sorted(candidates, key=lambda j: (j.get("fit") or 0, j.get("score") or 0), reverse=True):
        source = job.get("source")
        if (job.get("notes") or "").startswith(LEAD_PREFIX):
            continue  # already handed over on an earlier day
        if source in left and left[source] > 0:
            picks.append(job)
            left[source] -= 1
    return picks
