"""Stale-job sweeper: mark postings closed when the job board says they are gone.

Only a definitive answer from the board's own API closes a job (404, or absent
from the org's full Ashby board). Timeouts, 5xx and unknown sources are left
alone, so a network blip never purges live jobs. Adzuna has no lookup API, so
Adzuna jobs older than ADZUNA_MAX_DAYS are expired by age instead.

Usage: python stale.py            # dry run, prints what would close
       python stale.py --apply    # write status='closed' and rebuild needs_you.html
"""
import json, os, re, sqlite3, sys, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get("RA_DB", ROOT / "applications.sqlite3"))
OPEN = ("new", "needs_you", "captcha", "staged", "manual")
ADZUNA_MAX_DAYS = 30
UA = {"User-Agent": "remote-apply-stale/1.0"}
LIVE, GONE, UNKNOWN = "live", "gone", "unknown"


def http(url):
    """Return (status, body) or (None, None) on network failure."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        return None, None


def by_status(url):
    code, _ = http(url)
    return GONE if code == 404 else LIVE if code == 200 else UNKNOWN


def check_greenhouse(token, job_id, url):
    if not token or not job_id:
        return UNKNOWN
    return by_status(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}")


def check_lever(token, job_id, url):
    m = re.search(r"jobs\.(?:eu\.)?lever\.co/([^/]+)/([0-9a-f-]{36})", url or "")
    if not m:
        return UNKNOWN
    us = by_status(f"https://api.lever.co/v0/postings/{m[1]}/{m[2]}")
    if us != GONE:
        return us
    return by_status(f"https://api.eu.lever.co/v0/postings/{m[1]}/{m[2]}")


_ashby = {}


def check_ashby(token, job_id, url):
    m = re.search(r"jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", url or "")
    if not m:
        return UNKNOWN
    org, jid = m[1], m[2]
    if org not in _ashby:
        code, body = http(f"https://api.ashbyhq.com/posting-api/job-board/{org}")
        try:
            _ashby[org] = {j["id"] for j in json.loads(body)["jobs"]} if code == 200 else None
        except Exception:
            _ashby[org] = None
    ids = _ashby[org]
    if ids is None:
        return UNKNOWN
    return LIVE if jid in ids else GONE


def check_workday(token, job_id, url):
    p = urlparse(url or "")
    parts = [x for x in p.path.split("/") if x]
    if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0] if parts else ""):
        parts = parts[1:]
    if len(parts) < 3 or parts[1] != "job" or ".myworkdayjobs.com" not in p.netloc:
        return UNKNOWN
    tenant = p.netloc.split(".")[0]
    return by_status(f"https://{p.netloc}/wday/cxs/{tenant}/{parts[0]}/{'/'.join(parts[1:])}")


def check_smartrecruiters(token, job_id, url):
    m = re.search(r"smartrecruiters\.com/([^/]+)/(\d+)", url or "")
    if not m:
        return UNKNOWN
    return by_status(f"https://api.smartrecruiters.com/v1/companies/{m[1]}/postings/{m[2]}")


CHECKS = {"greenhouse": check_greenhouse, "lever": check_lever, "ashby": check_ashby,
          "workday": check_workday, "smartrecruiters": check_smartrecruiters}


def adzuna_expired(found_at, today=None):
    try:
        found = datetime.fromisoformat(str(found_at)[:10]).date()
    except ValueError:
        return False
    return (today or date.today()) - found > timedelta(days=ADZUNA_MAX_DAYS)


def sweep(apply=False, conn=None):
    conn = conn or sqlite3.connect(DB, timeout=60)
    marks = ",".join("?" * len(OPEN))
    rows = conn.execute(f"SELECT rowid,source,token,job_id,url,found_at,company,title FROM jobs "
                        f"WHERE status IN ({marks})", OPEN).fetchall()
    gone, unknown = [], 0
    for r in rows:
        if r[1] == "adzuna" and adzuna_expired(r[5]):
            gone.append((r, f"stale: Adzuna listing over {ADZUNA_MAX_DAYS} days old"))
    todo = [r for r in rows if r[1] in CHECKS]
    with ThreadPoolExecutor(8) as ex:
        for r, verdict in zip(todo, ex.map(lambda r: CHECKS[r[1]](r[2], r[3], r[4]), todo)):
            if verdict == GONE:
                gone.append((r, f"stale: posting removed from {r[1]}"))
            elif verdict == UNKNOWN:
                unknown += 1
    stamp = date.today().isoformat()
    for r, why in gone:
        print(f"{'CLOSE' if apply else 'would close'}  {r[6]} | {r[7]}  ({why})")
        if apply:
            conn.execute("UPDATE jobs SET status='closed', notes=coalesce(notes,'')||? WHERE rowid=?",
                         (f" | {why} ({stamp})", r[0]))
    if apply:
        conn.commit()
    print(f"checked {len(todo)} via board APIs, {len(rows) - len(todo)} other; "
          f"{len(gone)} {'closed' if apply else 'stale'}, {unknown} could not be verified (left open)")
    return gone


if __name__ == "__main__":
    do_apply = "--apply" in sys.argv
    sweep(apply=do_apply)
    if do_apply:
        sys.path.insert(0, str(ROOT / "build"))
        import needs_you_page
        # The nightly worktree symlinks the DB; write the page beside the real DB, not the worktree.
        home = DB.resolve().parent
        needs_you_page.ROOT, needs_you_page.OUT = home, home / "needs_you.html"
        needs_you_page.main()
