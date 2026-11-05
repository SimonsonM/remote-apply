"""Indeed job source (discovery only) via python-jobspy.

Local searches around home and a remote search per keyword. When Indeed lists the employer's own posting on a
supported application system (Greenhouse, Lever, Workday, ...) the job goes into that system's queue; otherwise it
is an Indeed lead you apply to yourself. Volume is kept modest (Indeed rate-limits and its terms discourage
scraping): a few searches per night, recent postings only.

boards.json:
  "indeed": {"enabled": true, "keywords": [...], "location": "Janesville, WI", "distance": 40,
             "remote": true, "hours_old": 72, "results": 30}
"""
import math

KEYWORDS = ("project manager", "program manager", "operations manager", "implementation manager",
            "IT project manager", "marketing manager")


def _num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def salary_text(row):
    lo, hi = _num(row.get("min_amount")), _num(row.get("max_amount"))
    if lo is None and hi is None:
        return ""
    lo, hi = lo or hi, hi or lo
    unit = "per hour" if str(row.get("interval") or "").lower() == "hourly" else "per year"
    fmt = lambda n: f"{n:,.2f}" if n < 1000 else f"{n:,.0f}"
    return f"${fmt(lo)} - ${fmt(hi)} {unit}"


def to_job(row, remote_search=False):
    import apply
    direct = str(row.get("job_url_direct") or "")
    direct = direct if direct.startswith("http") else ""
    url = str(row.get("job_url") or direct)
    loc = str(row.get("location") or "").replace(", USA", "")
    if row.get("is_remote") is True or remote_search:
        loc = f"Remote - United States ({loc})" if loc else "Remote - United States"
    job = dict(company=str(row.get("company") or ""), title=str(row.get("title") or ""), location=loc,
               description=str(row.get("description") or ""), url=url, apply_url=direct or url, source="indeed",
               token="", job_id=str(row.get("id") or ""), salary=salary_text(row),
               posted_at=str(row.get("date_posted") or "")[:10])
    if direct and apply.ats_source(direct):
        job = apply.promote_remote_board(job)   # employer's own Greenhouse/Lever/Workday posting: use that queue
    return job


def fetch(cfg, log=print):
    from jobspy import scrape_jobs
    searches = []
    for kw in cfg.get("keywords") or KEYWORDS:
        searches.append((kw, dict(location=cfg.get("location", "Janesville, WI"), distance=cfg.get("distance", 40)), False))
        if cfg.get("remote", True):
            searches.append((kw, dict(is_remote=True), True))
    seen = set()
    for kw, where, remote in searches:
        try:
            df = scrape_jobs(site_name=["indeed"], search_term=kw, hours_old=cfg.get("hours_old", 72),
                             results_wanted=cfg.get("results", 30), country_indeed="USA", description_format="markdown", **where)
        except Exception as exc:
            log(f"fetch failed indeed '{kw}': {str(exc)[:120]}")
            continue
        for _, row in df.iterrows():
            job = to_job(row.to_dict(), remote)
            if job["url"] and job["url"] not in seen and job["title"]:
                seen.add(job["url"])
                yield job
