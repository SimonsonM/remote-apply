#!/usr/bin/env python3
"""Remote Apply - unattended remote-job finder and applicant.

  python apply.py run [--dry]   scan boards, queue/submit applications, write applications.csv
  python apply.py scan          refresh listings only
  python apply.py export        rewrite applications.csv
  python apply.py report        write jobs.html shortlist
  python apply.py mark URL STATUS
  python apply.py apply URL     open one application prefilled in a visible browser (manual finish)
  python apply.py recruiter-email URL  write a grounded draft; never send it

Rules: one application per company ever (except names in profile.never_twice_exceptions);
never invents answers - any required question not covered by profile.json/answers.json goes to
'Needs you'; never marks 'Applied' without seeing a confirmation page.
"""
import argparse, csv, datetime as dt, hashlib, html, json, os, random, re, sqlite3, time, urllib.error, urllib.request
from urllib.parse import urlparse, urljoin
from html.parser import HTMLParser
import state, policy, answer_store, answer_wizard, followup
import income
from pathlib import Path

ROOT = Path(__file__).resolve().parent
def _boards_path():
    """Your boards.json (here, or beside the real DB when running from a worktree), else the shipped example."""
    db = Path(os.environ.get("RA_DB", ROOT / "applications.sqlite3"))
    for p in (ROOT / "boards.json", db.resolve().parent / "boards.json"):
        if p.exists():
            return p
    return ROOT / "boards.example.json"


DB, CONFIG, BOARDS, ANSWERS = Path(os.environ.get("RA_DB", ROOT / "applications.sqlite3")), ROOT / "profile.json", _boards_path(), ROOT / "answers.json"


def _profile_file():
    try:
        return json.loads(CONFIG.read_text())
    except Exception:
        return {}


_PROFILE = _profile_file()
# Home area for on-site/hybrid roles, from profile.json "home_area" (see profile.example.json). Without it only remote roles qualify.
_LOC = _PROFILE.get("home_area") or {}
HOME = tuple(_LOC["home"]) if _LOC.get("home") else None
HOME_STATES = [s.lower() for s in _LOC.get("states", [])]
LOCAL_PLACES = {k.lower(): tuple(v) for k, v in (_LOC.get("places") or {}).items()}
_METRO = _LOC.get("metro") or {}
METRO_CENTER = tuple(_METRO["center"]) if _METRO.get("center") else None  # optional second commute zone (a nearby metro)
METRO_NAME = (_METRO.get("name") or "").lower()
METRO_STATES = [s.lower() for s in _METRO.get("states", [])]
METRO_PLACES = {k.lower(): tuple(v) for k, v in (_METRO.get("places") or {}).items()}
CSV_OUT, PROOF, LOG = (DB.parent if os.environ.get("RA_DB") else ROOT) / "applications.csv", ROOT / "proof", ROOT / "run.log"
APPLIED_LOG = CSV_OUT.parent / "applied_log.csv"
APPLIED_COLS = ["Date", "Time (CT)", "Company", "Role", "Status", "Location", "HQ", "Salary (posted)", "Fit Score",
                "Ghost Risk", "Source", "Follow Up By", "Job URL", "Apply URL", "Notes"]


def out_dir():
    """CSV outputs live next to the active DB, so tests (which repoint DB) never touch the real files."""
    return ROOT if Path(DB).resolve() == (ROOT / "applications.sqlite3").resolve() else Path(DB).parent


def log_application(job, status, note=""):
    """Append one row per submitted application; never rewritten."""
    now = dt.datetime.now()
    APPLIED_LOG = out_dir() / "applied_log.csv"
    new = not APPLIED_LOG.exists()
    with APPLIED_LOG.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=APPLIED_COLS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({"Date": now.date().isoformat(), "Time (CT)": now.strftime("%H:%M:%S"), "Company": job.get("company", ""),
                    "Role": job.get("title", ""), "Status": status, "Location": job.get("location", ""), "HQ": job.get("hq", ""),
                    "Salary (posted)": job.get("salary", ""), "Fit Score": job.get("fit") or job.get("score", ""),
                    "Ghost Risk": job.get("ghost") or "", "Source": job.get("source", ""),
                    "Follow Up By": (now.date() + dt.timedelta(days=10)).isoformat(), "Job URL": job.get("url", ""),
                    "Apply URL": job.get("apply_url", ""), "Notes": note})
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"}
CURRENT_JOB = {}
CLIENT_VALIDATION = re.compile(r"Form rejected:.*(select an?\b|is required|required field|this field|please (select|enter|fill|choose))", re.I)
CLAIMED = ("staged", "ready", "needs_you", "captcha", "applied", "unconfirmed", "interview", "rejected", "offer")
SUCCESS_PATTERNS = [r'thank you for applying', r'thank you for your interest in (?:working (?:at|with|for)|joining)', r'thanks for applying', r'delighted that you would consider joining', r'(?:team|we) (?:is|are) reviewing your application', r'(?:your )?application (?:was |has been )?(?:successfully )?(?:submitted|received)', r"we(?:'ve| have) received your application"]
STATUS_LABEL = {"staged": "Open in Chrome - review & submit", "applied": "Applied", "unconfirmed": "Verify - submit not confirmed", "needs_you": "Needs you", "manual": "Manual apply",
                "ready": "Ready (auto-submit off)", "interview": "Interview", "rejected": "Rejected", "offer": "Offer"}
STATUS_ORDER = ["staged", "needs_you", "unconfirmed", "applied", "ready", "interview", "offer", "rejected", "manual"]


def log(msg):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M} {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def attachment_details(value):
    path = Path(value).expanduser().resolve()
    return f'{path} ({path.stat().st_size} bytes, file={path.is_file()})'


def get_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
        return json.load(r)


def post_json(url, payload):
    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={**UA, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def clean(v):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(v or ""))).strip()


def ckey(company):
    return re.sub(r"[^a-z0-9]", "", company.lower())


def db():
    if not DB.exists():
        try:
            fd = os.open(DB, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
    conn = sqlite3.connect(DB)
    conn.create_function('salary_max', 1, posted_salary_max)
    state.migrate(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT, location TEXT, description TEXT, "
                 "score INTEGER, status TEXT DEFAULT 'new', updated TEXT DEFAULT CURRENT_TIMESTAMP, fit REAL DEFAULT 0, "
                 "ghost REAL DEFAULT 0, value REAL DEFAULT 0, learn_label INTEGER, posted_at TEXT DEFAULT '', "
                 "ghost_why TEXT DEFAULT '', source TEXT DEFAULT '', hq TEXT DEFAULT '', salary TEXT DEFAULT '', "
                 "token TEXT DEFAULT '', job_id TEXT DEFAULT '', apply_url TEXT DEFAULT '', applied_at TEXT DEFAULT '', "
                 "notes TEXT DEFAULT '', found_at TEXT DEFAULT '')")
    return conn


def followup_days():
    try:
        days = json.loads(CONFIG.read_text()).get('followup_days', 10)
    except FileNotFoundError:
        days = 10
    if not isinstance(days, int) or isinstance(days, bool) or days < 0:
        raise ValueError('followup_days must be a nonnegative integer')
    return days


# ---------------------------------------------------------------- discovery
SAL_RE = re.compile(r"(?:US)?\$\s?\d{2,3}(?:,\d{3}|[kK])(?:\.\d+)?\s?(?:-|–|—|to)\s?(?:US)?\$?\s?\d{2,3}(?:,\d{3}|[kK])(?:\.\d+)?")

PAY_AMOUNT = re.compile(r'(?:USD\s*|\$\s*)(\d+(?:\.\d+)?\s*[kK]|\d{1,3}(?:,\d{3})*(?:\.\d+)?)', re.I)


def posted_salary_max(salary):
    """Highest explicitly posted annual USD pay; unknown pay is unrestricted."""
    text = str(salary or '')
    if not text or text.lower().startswith('est.'):
        return None
    amounts = []
    for match in PAY_AMOUNT.finditer(text):
        token = match.group(1).replace(',', '').replace(' ', '')
        value = float(token[:-1]) * 1000 if token.lower().endswith('k') else float(token)
        tail = text[match.end():match.end() + 30].lower()
        if re.search(r'(?:/\s*(?:hr|hour)|per[ -]?hour|hourly)', tail):
            value *= 2080
        elif value < 1000 and re.search(r'(?:/\s*(?:hr|hour)|per[ -]?hour|hourly)', text, re.I):
            value *= 2080
        if 10000 <= value <= 1000000:
            amounts.append(int(value))
    if re.search(r'\bUSD\b', text, re.I):
        for match in re.finditer(r'[-–—]\s*(\d{2,3}(?:,\d{3})+(?:\.\d+)?)', text):
            amounts.append(int(float(match.group(1).replace(',', ''))))
    return max(amounts) if amounts else None


def salary_from(text):
    m = SAL_RE.search(text or "")
    return m.group(0) if m else ""


def _wd_post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=dict(UA, **{"Content-Type": "application/json", "Accept": "application/json"}))
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch_workday(name, cfg, max_pages=40):
    """Workday career sites (public cxs JSON)."""
    host, tenant, site = cfg["host"], cfg["tenant"], cfg["site"]
    base = f"https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    seen = set()
    for text in cfg.get("search") or [""]:
        for page in range(max_pages):
            d = _wd_post(base + "/jobs", {"limit": 20, "offset": page * 20, "searchText": text, "appliedFacets": {}})
            posts = d.get("jobPostings") or []
            for p in posts:
                path, title = p.get("externalPath", ""), p.get("title", "")
                if not path or path in seen:
                    continue
                seen.add(path)
                tl = title.lower()
                if any(t in tl for t in BAD_TITLE) or not role_ok(title):
                    continue
                pre = f"{p.get('remoteType') or ''} - {p.get('locationsText') or ''}"
                if not (local_ok(pre, 999) or REMOTE.search(pre) or re.search(r"\d+ locations", pre, re.I)):
                    continue
                try:
                    info = get_json_h(base + path).get("jobPostingInfo") or {}
                except Exception:
                    continue
                locs = [info.get("location", "")] + list(info.get("additionalLocations") or [])
                loc = f"{info.get('remoteType') or p.get('remoteType') or ''} - " + " / ".join(filter(None, locs)) + \
                      f" / {(info.get('country') or {}).get('descriptor', '')}"
                desc = clean(info.get("jobDescription", ""))
                url = info.get("externalUrl") or f"https://{host}.myworkdayjobs.com/{site}{path}"
                yield dict(company=name, title=title, location=loc, description=desc, url=url, source="workday", token=tenant,
                           job_id=info.get("jobReqId") or path.rsplit("_", 1)[-1], apply_url=url.rstrip("/") + "/apply",
                           salary=salary_from(desc), posted_at=info.get("startDate", "")[:10])
            if len(posts) < 20:
                break


def get_json_h(url):
    req = urllib.request.Request(url, headers=dict(UA, Accept="application/json"))
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


AGENCY_SOURCES = ("roberthalf", "michaelpage", "adecco")  # staffing agencies: top N per agency per day, see agencies.py
LEAD_SOURCES = ("workday", "icims", "adzuna", "remotive", "himalayas", "usajobs", "indeed", "google")  # Account sources are routed separately when enabled
KEYS_FILE = Path.home() / ".config" / "remote_apply" / "keys.env"
ADZUNA_CALLS = {"n": 0}


def load_keys():
    keys = {}
    try:
        for line in KEYS_FILE.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                keys[k.strip()] = v.strip()
    except OSError:
        pass
    return keys


def _dist(a, b):
    import math
    return 3959 * 2 * math.asin(math.sqrt(math.sin(math.radians(a[0] - b[0]) / 2) ** 2 + math.cos(math.radians(b[0])) *
                                           math.cos(math.radians(a[0])) * math.sin(math.radians(a[1] - b[1]) / 2) ** 2))


def _miles(la, lo):
    import math
    if not HOME:
        return float("inf")
    return 3959 * 2 * math.asin(math.sqrt(math.sin(math.radians(la - HOME[0]) / 2) ** 2 + math.cos(math.radians(HOME[0])) *
                                           math.cos(math.radians(la)) * math.sin(math.radians(lo - HOME[1]) / 2) ** 2))


ATS_SOURCES = ('greenhouse', 'lever', 'ashby', 'workday', 'workable', 'smartrecruiters', 'icims', 'eightfold', 'microsoft')


def ats_source(url):
    host = (urlparse(url).hostname or '').lower()
    if host in ('boards.greenhouse.io', 'job-boards.greenhouse.io', 'boards-api.greenhouse.io'):
        return 'greenhouse'
    if host == 'jobs.lever.co':
        return 'lever'
    if host == 'jobs.ashbyhq.com':
        return 'ashby'
    if host.endswith('.myworkdayjobs.com'):
        return 'workday'
    if host == 'apply.workable.com':
        return 'workable'
    if host in ('jobs.smartrecruiters.com', 'careers.smartrecruiters.com'):
        return 'smartrecruiters'
    if host == 'icims.com' or host.endswith('.icims.com'):
        return 'icims'
    return None


class _ApplyLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and attrs.get('href'):
            self.links.append(attrs['href'])
        if tag == 'meta' and attrs.get('http-equiv', '').lower() == 'refresh':
            match = re.search(r'url\s*=\s*[\'\"]?([^\'\";]+)', attrs.get('content', ''), re.I)
            if match:
                self.links.append(match.group(1))


def resolve_adzuna_url(redirect_url, details_url):
    """Follow the aggregator redirect and inspect its landing page for a supported ATS link."""
    if ats_source(redirect_url):
        return state.normalize_url(redirect_url), ats_source(redirect_url)
    for start in dict.fromkeys((redirect_url, details_url)):
        if not start or urlparse(start).scheme not in ('http', 'https'):
            continue
        try:
            req = urllib.request.Request(start, headers=UA)
            with urllib.request.urlopen(req, timeout=12) as response:
                final = response.geturl()
                if ats_source(final):
                    return state.normalize_url(final), ats_source(final)
                parser = _ApplyLinks()
                parser.feed(response.read(250_000).decode('utf-8', errors='replace'))
                for link in parser.links:
                    target = urljoin(final, html.unescape(link))
                    if ats_source(target):
                        return state.normalize_url(target), ats_source(target)
        except (OSError, ValueError, UnicodeError) as exc:
            log(f"adzuna redirect failed {start}: {str(exc)[:100]}")
    return None, None


def promote_adzuna(job):
    url, source = resolve_adzuna_url(job.get('apply_url'), job.get('url'))
    if source:
        job.update(url=url, apply_url=url, source=source, token='', job_id='')
    return job


AGGREGATOR = re.compile(r"(indeed|linkedin|ziprecruiter|glassdoor|monster|simplyhired|talent\.com|jooble|adzuna|careerbuilder|"
                        r"dice\.com|snagajob|google\.com|lensa|jobright|builtin)\.", re.I)
SHORTLINK = re.compile(r"^https?://(grnh\.se|lnkd\.in|bit\.ly|t\.co|tinyurl\.com|jobs\.lever\.co/r)/", re.I)


def resolve_short(url):
    """Follow a short link (grnh.se is Greenhouse's) to the real posting."""
    if not SHORTLINK.match(url or ""):
        return url
    try:
        req = urllib.request.Request(url, method="HEAD", headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.geturl()
    except Exception:
        return url


def promote_employer_site(job):
    """A lead whose link is the employer's own application page: ATS queue if we know the system,
    else source 'employer' so the generic form filler applies to it instead of leaving it to the user."""
    url = resolve_short(job.get('apply_url') or '')
    if not url.startswith('http') or AGGREGATOR.search(urlparse(url).hostname or ''):
        return job
    if ats_source(url):
        job['apply_url'] = url
        return promote_remote_board(job)
    job.update(apply_url=url, source='employer')
    return job


def promote_remote_board(job):
    """A direct supported ATS link can use the existing ATS queue and dedupe."""
    url = job.get('apply_url') or ''
    source = ats_source(url)
    if source:
        url = state.normalize_url(url)
        job.update(url=url, apply_url=url, source=source, token='', job_id='')
    return job


def existing_ats_url(conn, url, exclude=None):
    if not url:
        return None
    canonical = state.normalize_url(url)
    for old_url, apply_url in conn.execute("SELECT url,apply_url FROM jobs WHERE source IN ('greenhouse','lever','ashby','workday','workable','smartrecruiters','icims','eightfold','microsoft')"):
        if old_url != exclude and canonical in (state.normalize_url(old_url), state.normalize_url(apply_url)):
            return old_url
    return None


def refresh_adzuna_jobs(conn):
    """Repair old Adzuna queue entries before either run mode selects candidates."""
    for url, apply_url in conn.execute("SELECT url,apply_url FROM jobs WHERE source='adzuna' AND status IN ('new','ready','needs_you','staged')").fetchall():
        target, source = resolve_adzuna_url(apply_url, url)
        if not source:
            conn.execute("UPDATE jobs SET status='manual', notes='Adzuna lead: apply yourself', updated=CURRENT_TIMESTAMP WHERE url=?", (url,))
        elif existing_ats_url(conn, target, exclude=url):
            conn.execute("UPDATE jobs SET status='duplicate', notes='Already listed at supported ATS', updated=CURRENT_TIMESTAMP WHERE url=?", (url,))
        else:
            conn.execute("UPDATE jobs SET source=?, apply_url=?, status='new', notes='', updated=CURRENT_TIMESTAMP WHERE url=?", (source, target, url))
    conn.commit()


def fetch_adzuna(cfg, keys, radius_miles=40, max_calls=20):
    """Adzuna US search: local (radius from home zip) and remote queries. Paid plan: 500/day."""
    from urllib.parse import urlencode
    base = "https://api.adzuna.com/v1/api/jobs/us/search/"
    seen = set()
    home_zip = cfg.get("zip") or _LOC.get("zip")
    centers = ([(home_zip, radius_miles, HOME)] if HOME and home_zip else []) + [(c["zip"], c["radius_miles"], tuple(c["latlon"])) for c in cfg.get("extra_centers", [])]
    queries = [dict(what=w, where=z, distance=round(r * 1.609), _center=ctr, _radius=r) for z, r, ctr in centers for w in cfg.get("local", [])] + \
              [dict(what=w + " remote") for w in cfg.get("remote", [])]
    for q in queries:
        for page in range(1, cfg.get("pages", 2) + 1):
            if ADZUNA_CALLS["n"] >= max_calls:
                log(f"adzuna: call budget {max_calls} reached")
                return
            params = dict(app_id=keys["ADZUNA_APP_ID"], app_key=keys["ADZUNA_APP_KEY"], results_per_page=50,
                          max_days_old=cfg.get("max_days_old", 14), sort_by="date",
                          **{k: v for k, v in q.items() if not k.startswith("_")})
            endpoint = base + f"{page}?" + urlencode(params) + "&content-type=application/json"
            for attempt in range(3):
                if ADZUNA_CALLS["n"] >= max_calls:
                    log(f"adzuna: call budget {max_calls} reached")
                    return
                ADZUNA_CALLS["n"] += 1
                try:
                    d = get_json(endpoint)
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code != 503:
                        raise
                    if attempt == 2:
                        log(f"adzuna: HTTP 503 after 3 attempts; continuing")
                    else:
                        time.sleep(2 ** attempt)
            else:
                break
            res = d.get("results") or []
            for j in res:
                jid = str(j.get("id", ""))
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                title = clean(j.get("title", ""))
                desc = clean(j.get("description", ""))
                loc_name = (j.get("location") or {}).get("display_name", "")
                la, lo = j.get("latitude"), j.get("longitude")
                remote = bool(re.search(r"\bremote\b|work from home", title + " " + desc, re.I))
                ctr, rad = q.get("_center", HOME), q.get("_radius", radius_miles)
                local = la is not None and lo is not None and _dist((float(la), float(lo)), ctr) <= rad
                if "where" in q and not local:
                    continue
                if "where" not in q and not remote:
                    continue
                lo_s, hi_s = j.get("salary_min"), j.get("salary_max")
                sal = "" if not (lo_s and hi_s) else (f"USD {int(lo_s):,}-{int(hi_s):,}" if int(lo_s) != int(hi_s) or j.get("salary_is_predicted") == "0"
                                                      else f"est. USD {int(lo_s):,} (Adzuna estimate)")
                url = f"https://www.adzuna.com/details/{jid}"
                yield dict(company=(j.get("company") or {}).get("display_name", ""), title=title,
                           location=("Remote - United States / " if remote else "Onsite - ") + loc_name, description=desc,
                           url=url, source="adzuna", token="adzuna", job_id=jid, apply_url=j.get("redirect_url") or url, salary=sal or salary_from(desc),
                           posted_at=(j.get("created") or "")[:10], local=local)
            if len(res) < 50:
                break


REMOTE_API_DELAY_MIN, REMOTE_API_DELAY_MAX = 1.0, 2.5
HIMALAYAS_PAGE_SIZE, HIMALAYAS_MAX_PAGES = 20, 15
US_LOCATION_HINTS = ('usa', 'u.s.', 'us,', 'united states', 'north america', 'northern america',
                     'worldwide', 'anywhere', 'global', 'remote - worldwide', 'americas')


# profile.json "remote_anywhere": true accepts remote roles open to any country, not just ones open to US residents.
REMOTE_ANYWHERE = bool(_PROFILE.get("remote_anywhere", False))


def _allows_us_freeform(location):
    if REMOTE_ANYWHERE:
        return True
    if not location or not location.strip():
        return True
    return any(re.search(r'(?<![a-z])' + re.escape(hint) + r'(?![a-z])', location.lower())
               for hint in US_LOCATION_HINTS)


# profile.json "timezone": which timezones you can work. "any" (default) skips the check; otherwise remote jobs
# restricted to timezones that exclude yours are dropped. "home_utc_offsets" (list) overrides it for anything unusual.
TIMEZONES = {"eastern": (-5, -4), "central": (-6, -5), "mountain": (-7, -6), "arizona": (-7,),
             "pacific": (-8, -7), "alaska": (-9, -8), "hawaii": (-10,)}
_TZ_ALIASES = {"est": "eastern", "edt": "eastern", "et": "eastern", "cst": "central", "cdt": "central", "ct": "central",
               "mst": "mountain", "mdt": "mountain", "mt": "mountain", "pst": "pacific", "pdt": "pacific", "pt": "pacific",
               "akst": "alaska", "akdt": "alaska", "hst": "hawaii"}


def tz_offsets(profile):
    """UTC offsets the user can work in, from profile 'home_utc_offsets' or 'timezone'; () means any timezone."""
    if profile.get("home_utc_offsets"):
        return tuple(profile["home_utc_offsets"])
    name = str(profile.get("timezone") or "any").strip().lower()
    return TIMEZONES.get(_TZ_ALIASES.get(name, name), ())


def himalayas_allows_us(job, home_utc_offsets=()):
    countries = job.get('locationRestrictions') or []
    if countries and not REMOTE_ANYWHERE and not any(str(c).strip().lower() in ('united states', 'usa', 'u.s.', 'united states of america') for c in countries):
        return False
    offsets = job.get('timezoneRestrictions') or []
    if offsets and home_utc_offsets:
        try:
            if not {float(o) for o in offsets}.intersection(float(o) for o in home_utc_offsets):
                return False
        except (TypeError, ValueError):
            pass
    return True


def _remote_salary(low, high, period):
    if low is None and high is None:
        return ''
    try:
        low = float(low if low is not None else high)
        high = float(high if high is not None else low)
    except (ValueError, TypeError):
        return ''
    if period in ('month', 'monthly'):
        low, high = low * 12, high * 12
    unit = 'hourly' if period in ('hour', 'hourly') else 'yearly'
    fmt = lambda n: f'{n:g}' if n < 1000 else f'{n:,.0f}'
    return f'{fmt(low)}-{fmt(high)} {unit}' if low != high else f'{fmt(low)} {unit}'


def _remotive_salary(value):
    if not value:
        return ''
    amounts = []
    for raw, suffix in re.findall(r'(\d[\d,]*(?:\.\d+)?)\s*([km])?(?![a-z])', value.lower()):
        amounts.append(float(raw.replace(',', '')) * {'k': 1000, 'm': 1000000}.get(suffix, 1))
    if not amounts:
        return ''
    period = 'hourly' if re.search(r'\b(hour(ly)?|hrs?|hrly)\b|/\s*h\b', value, re.I) else \
             'monthly' if re.search(r'\bmonth(ly)?\b|/\s*mo\b', value, re.I) else 'yearly'
    return _remote_salary(min(amounts), max(amounts), period)


def _remote_posted(value):
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).date().isoformat()
    return str(value or '')[:10]


def fetch_remotive(cfg):
    """Remotive ignores search and paging parameters, so fetch its fixed set once."""
    seen = set()
    for j in (get_json('https://remotive.com/api/remote-jobs').get('jobs') or []):
        url, title = (j.get('url') or '').strip(), (j.get('title') or '').strip()
        if not url or not title or url in seen or not _allows_us_freeform(j.get('candidate_required_location')):
            continue
        seen.add(url)
        loc = j.get('candidate_required_location') or 'Worldwide'
        desc = clean(j.get('description') or '')
        yield dict(company=j.get('company_name') or '', title=title, location=f'Remote - {loc}' if REMOTE_ANYWHERE else f'Remote - United States / {loc}',
                   description=desc, url=url, apply_url=url, source='remotive', salary=_remotive_salary(j.get('salary')) or salary_from(desc),
                   token='remotive', job_id=str(j.get('id') or ''), posted_at=_remote_posted(j.get('publication_date')))


def fetch_himalayas(cfg):
    """Himalayas ignores search parameters and caps pages at 20 listings."""
    seen = set()
    page_size = min(20, max(1, int(cfg.get('page_size', HIMALAYAS_PAGE_SIZE))))
    for page in range(max(0, int(cfg.get('max_pages', HIMALAYAS_MAX_PAGES)))):
        if page:
            time.sleep(random.uniform(REMOTE_API_DELAY_MIN, REMOTE_API_DELAY_MAX))
        from urllib.parse import urlencode
        data = get_json('https://himalayas.app/jobs/api?' + urlencode({'limit': page_size, 'offset': page * page_size}))
        jobs = data.get('jobs') or []
        for j in jobs:
            url, title = (j.get('applicationLink') or '').strip(), (j.get('title') or '').strip()
            if not url or not title or url in seen or not himalayas_allows_us(j, tz_offsets({**_PROFILE, **{k: cfg[k] for k in ('timezone', 'home_utc_offsets') if k in cfg}})):
                continue
            seen.add(url)
            desc = clean(j.get('description') or '')
            countries = ', '.join(j.get('locationRestrictions') or []) or 'Worldwide'
            currency = (j.get('currency') or '').upper()
            sal = _remote_salary(j.get('minSalary'), j.get('maxSalary'), (j.get('salaryPeriod') or '').lower()) if currency in ('', 'USD') else ''
            yield dict(company=j.get('companyName') or '', title=title, location=f'Remote - {countries}' if REMOTE_ANYWHERE else f'Remote - United States / {countries}',
                       description=desc, url=url, apply_url=url, source='himalayas', salary=sal or salary_from(desc),
                       token='himalayas', job_id=str(j.get('id') or j.get('guid') or ''), posted_at=_remote_posted(j.get('pubDate')))
        if len(jobs) < page_size:
            break


def fetch_usajobs(keys, cfg):
    """Federal postings (USAJOBS API): leads only - federal applications need Login.gov and agency questionnaires."""
    import urllib.parse, urllib.request
    hdr = {"Host": "data.usajobs.gov", "User-Agent": keys["USAJOBS_EMAIL"], "Authorization-Key": keys["USAJOBS_API_KEY"]}
    common = {"JobCategoryCode": cfg.get("series", "2210;0343;0340;1102;0301"), "PayGradeLow": cfg.get("grade_low", "07"),
              "PayGradeHigh": cfg.get("grade_high", "13"), "HiringPath": "public", "DatePosted": "30", "ResultsPerPage": "500"}
    searches = [{"RemoteIndicator": "True"}, {"LocationName": "Anywhere in the U.S. (remote job)"}] + \
               [{"LocationName": loc, "Radius": "40"} for loc in cfg.get("locations", [])]
    seen = set()
    for extra in searches:
        url = "https://data.usajobs.gov/api/search?" + urllib.parse.urlencode({**common, **extra}, safe=";")
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=30) as r:
            items = json.load(r)["SearchResult"].get("SearchResultItems", [])
        for it in items:
            d = it["MatchedObjectDescriptor"]
            if it.get("MatchedObjectId") in seen or (d.get("JobGrade") or [{}])[0].get("Code") not in ("GS", "GG", "GL") \
                    or "billet" in d.get("PositionTitle", "").lower():
                continue
            seen.add(it.get("MatchedObjectId"))
            det = d.get("UserArea", {}).get("Details", {})
            pay = (d.get("PositionRemuneration") or [{}])[0]
            remote = "Remote - " if det.get("RemoteIndicator") or "remote job" in d.get("PositionLocationDisplay", "").lower() else ""
            places = [p.get("LocationName", "") for p in d.get("PositionLocation") or []]
            # Leads only, so the user reviews them: keep negotiable duty stations and multi-site postings with a site near home.
            near = "negotiable" in d.get("PositionLocationDisplay", "").lower() or any(
                local_ok(p, cfg.get("radius_miles", 40)) or metro_ok(p, cfg.get("radius_miles", 40)) for p in places)
            yield dict(local=bool(near and not remote), company=f'{d.get("OrganizationName", "")} (federal)', title=d.get("PositionTitle", ""),
                       location=remote + d.get("PositionLocationDisplay", "") + " / United States",
                       description=clean(" ".join(filter(None, [det.get("JobSummary", ""), d.get("QualificationSummary", "")]))),
                       url=d.get("PositionURI", ""), source="usajobs", token="", job_id=str(it.get("MatchedObjectId", "")),
                       apply_url=(d.get("ApplyURI") or [d.get("PositionURI", "")])[0],
                       salary=f'${pay.get("MinimumRange", "")}-${pay.get("MaximumRange", "")} {pay.get("RateIntervalCode", "")}',
                       posted_at=(d.get("PublicationStartDate") or "")[:10])


def fetch_jobs(boards):
    keys = load_keys()
    microsoft_cfg = boards.get('microsoft')
    if microsoft_cfg and microsoft_cfg.get('enabled', True):
        import eightfold
        try:
            yield from eightfold.fetch_eightfold('Microsoft', microsoft_cfg, source='microsoft')
        except Exception as e:
            log(f'fetch failed Microsoft Eightfold: {str(e)[:120]}')
    for name, cfg in boards.get('eightfold', {}).items():
        import eightfold
        try:
            yield from eightfold.fetch_eightfold(name, cfg)
        except Exception as e:
            log(f'fetch failed {name} Eightfold: {str(e)[:120]}')
    if boards.get('remotive', {}).get('enabled'):
        try:
            yield from fetch_remotive(boards['remotive'])
        except Exception as e:
            log(f"fetch failed remotive: {str(e)[:120]}")
    if boards.get('himalayas', {}).get('enabled'):
        try:
            yield from fetch_himalayas(boards['himalayas'])
        except Exception as e:
            log(f"fetch failed himalayas: {str(e)[:120]}")
    if keys.get("USAJOBS_API_KEY") and keys.get("USAJOBS_EMAIL") and boards.get("usajobs", {}).get("enabled", True):
        try:
            yield from fetch_usajobs(keys, {**boards.get("usajobs", {}), **(_PROFILE.get("usajobs") or {})})
        except Exception as e:
            log(f"fetch failed usajobs: {str(e)[:120]}")
    if (boards.get("google_jobs") or {}).get("enabled") and keys.get("SERPAPI_KEY"):
        import google_jobs
        yield from google_jobs.fetch(boards["google_jobs"], keys, log)
    if (boards.get("indeed") or {}).get("enabled"):
        import indeed
        yield from indeed.fetch(boards["indeed"], log)
    if boards.get("agencies"):
        import agencies
        yield from agencies.fetch(boards, log, wanted=lambda t: bool(role_ok(t) and TRACK_TITLE.search(t)))
    if boards.get("adzuna") and keys.get("ADZUNA_APP_ID") and keys.get("ADZUNA_APP_KEY"):
        try:
            yield from fetch_adzuna(boards["adzuna"], keys, boards["adzuna"].get("radius_miles", 40), boards["adzuna"].get("max_calls", 20))
        except Exception as e:
            log(f"fetch failed adzuna: {str(e)[:120]}")
    for name, cfg in boards.get("workday", {}).items():
        try:
            yield from fetch_workday(name, cfg)
        except Exception as e:
            log(f"fetch failed {name}: {e}")
    if boards.get("icims"):
        import icims
        for name, cfg in boards["icims"].items():
            try:
                yield from icims.fetch_icims(name, cfg)
            except Exception as e:
                log(f"fetch failed {name}: {e}")
    for name, token in boards.get("greenhouse", {}).items():
        try:
            for j in get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true").get("jobs", []):
                desc = clean(j.get("content", ""))
                yield dict(company=name, title=j.get("title", ""), location=(j.get("location") or {}).get("name", ""),
                           description=desc, url=j.get("absolute_url", ""), source="greenhouse", token=token, job_id=str(j["id"]),
                           apply_url=f"https://job-boards.greenhouse.io/embed/job_app?for={token}&token={j['id']}", salary=salary_from(desc),
                           posted_at=(j.get("first_published") or j.get("updated_at") or "")[:10])
        except Exception as e:
            log(f"fetch failed {name}: {e}")
    for name, token in boards.get("lever", {}).items():
        try:
            for j in get_json(f"https://api.lever.co/v0/postings/{token}?mode=json"):
                c = j.get("categories", {})
                wp = (j.get("workplaceType") or "").lower()
                pre = {"remote": "Remote - ", "hybrid": "Hybrid - ", "onsite": "Onsite - "}.get(wp, "")
                loc = pre + " / ".join(filter(None, (c.get("allLocations") or [c.get("location", "")]) + [j.get("country", "")]))
                sr = j.get("salaryRange") or {}
                sal = f"{sr.get('currency','')} {sr.get('min',''):,}-{sr.get('max',''):,} {sr.get('interval','')}".strip() if sr.get("min") else ""
                desc = clean(j.get("descriptionPlain") or j.get("description", "")) + " " + " ".join(clean(l.get("content", "")) for l in j.get("lists", []))
                yield dict(company=name, title=j.get("text", ""), location=loc, description=desc, url=j.get("hostedUrl", ""),
                           source="lever", token=token, job_id=j.get("id", ""), apply_url=j.get("applyUrl") or j.get("hostedUrl", "") + "/apply",
                           salary=sal or salary_from(desc),
                           posted_at=dt.datetime.fromtimestamp(j["createdAt"] / 1000).date().isoformat() if j.get("createdAt") else "")
        except Exception as e:
            log(f"fetch failed {name}: {e}")
    for name, token in boards.get("ashby", {}).items():
        try:
            for j in get_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true").get("jobs", []):
                if not j.get("isListed", True):
                    continue
                wp = j.get("workplaceType") or ""
                pre = {"Remote": "Remote - ", "Hybrid": "Hybrid - ", "OnSite": "Onsite - "}.get(wp, "Remote - " if j.get("isRemote") else "")
                locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
                country = (((j.get("address") or {}).get("postalAddress")) or {}).get("addressCountry", "")
                comp = j.get("compensation") or {}
                desc = j.get("descriptionPlain", "")
                yield dict(company=name, title=j.get("title", ""), location=pre + " / ".join(filter(None, locs + [country])),
                           description=desc, url=j.get("jobUrl", ""), source="ashby", token=token, job_id=j.get("id", ""),
                           apply_url=j.get("applyUrl", ""), salary=comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary") or salary_from(desc),
                           posted_at=(j.get("publishedAt") or "")[:10])
        except Exception as e:
            log(f"fetch failed {name}: {e}")
    for name, token in boards.get('workable', {}).items():
        try:
            listing = post_json(f'https://apply.workable.com/api/v3/accounts/{token}/jobs',
                                {'query': '', 'location': [], 'department': [], 'worktype': [], 'remote': []})
            try:
                details = get_json(f'https://apply.workable.com/api/v1/widget/accounts/{token}?details=true')
            except Exception as e:
                log(f'fetch Workable descriptions failed {name}: {e}')
                details = {}
            by_code = {j['shortcode']: j for j in details.get('jobs', []) if j.get('shortcode')}
            # v3 exposes only its first batch; the public widget supplies the full board.
            jobs_by_code = {j['shortcode']: j for j in listing.get('results', []) if j.get('shortcode')}
            for code, detail in by_code.items():
                jobs_by_code.setdefault(code, detail)
            for j in jobs_by_code.values():
                code = j.get('shortcode') or ''
                if not code or j.get('state', 'published') != 'published':
                    continue
                detail = by_code.get(code, {})
                place = j.get('location') or detail.get('location') or detail
                if not isinstance(place, dict):
                    place = detail
                loc = ' / '.join(filter(None, (place.get('city'), place.get('region'), place.get('country'))))
                remote = j.get('remote') or j.get('telecommuting') or j.get('workplace') == 'remote'
                loc = ('Remote - ' if remote else '') + loc
                desc = clean(detail.get('description', ''))
                url = f'https://apply.workable.com/{token}/j/{code}/'
                yield dict(company=name, title=j.get('title', ''), location=loc, description=desc,
                           url=url, source='workable', token=token, job_id=code,
                           apply_url=f'{url}apply', salary=salary_from(desc),
                           posted_at=(j.get('published') or detail.get('published_on') or '')[:10])
        except Exception as e:
            log(f'fetch failed {name}: {e}')
    for name, token in boards.get('smartrecruiters', {}).items():
        try:
            base = f'https://api.smartrecruiters.com/v1/companies/{token}/postings'
            offset = 0
            while True:
                data = get_json(f'{base}?limit=100&offset={offset}')
                items = data.get('content', [])
                for j in items:
                    job_id = str(j.get('id') or '')
                    if not job_id:
                        continue
                    # Cheap checks on the listing before the per-posting detail call (big boards: AbbVie has ~1,900).
                    title_l = (j.get('name') or '').lower()
                    if any(t in title_l for t in BAD_TITLE) or not role_ok(j.get('name') or '') or not TRACK_TITLE.search(title_l):
                        continue
                    lj = j.get('location') or {}
                    lstr = ', '.join(filter(None, (lj.get('city'), lj.get('region'), lj.get('country'))))
                    if lj and ((lj.get('country') or 'us').lower() != 'us' or not (lj.get('remote') or local_ok(lstr) or metro_ok(lstr))):
                        continue  # US remote or near home only
                    try:
                        detail = get_json(f'{base}/{job_id}')
                    except Exception as e:
                        log(f'fetch detail failed {name} {job_id}: {e}')
                        detail = j
                    loc_data = detail.get('location') or j.get('location') or {}
                    loc = loc_data.get('fullLocation') or ' / '.join(filter(None, (loc_data.get('city'), loc_data.get('region'), loc_data.get('country'))))
                    loc = ('Remote - ' if loc_data.get('remote') else '') + loc
                    sections = (detail.get('jobAd') or {}).get('sections') or {}
                    desc = clean(' '.join((v.get('text') or '') for v in sections.values() if isinstance(v, dict)))
                    url = detail.get('postingUrl') or f'https://jobs.smartrecruiters.com/{token}/{job_id}'
                    yield dict(company=name, title=detail.get('name') or j.get('name', ''), location=loc,
                               description=desc, url=url, source='smartrecruiters', token=token,
                               job_id=job_id, apply_url=detail.get('applyUrl') or url + '?oga=true',
                               salary=salary_from(desc), posted_at=(detail.get('releasedDate') or j.get('releasedDate') or '')[:10])
                offset += len(items)
                if not items or offset >= data.get('totalFound', offset):
                    break
        except Exception as e:
            log(f'fetch failed {name}: {e}')


# Titles in one of the resume tracks; keeps huge SmartRecruiters boards from costing a detail call per posting.
TRACK_TITLE = re.compile(r"program|project|operations|\bops\b|implementation|delivery|pmo|portfolio|strategy|chief of staff|"
                         r"\bai\b|artificial intelligence|machine learning|automation|data|analytics|"
                         r"marketing|brand|content|communications|growth|"
                         r"security|cyber|identity|\biam\b|\bit\b|information technology|infrastructure|systems|compliance|governance|risk", re.I)
OVERLEVEL_TITLE = re.compile(r"\b(director|staff|principal|head of|vp|vice president|svp|evp|chief|tech(nical)? lead manager|engineering manager|manager of managers)\b", re.I)


LEAD_LEVEL = re.compile(r"^\s*lead\b|,\s*lead\b|\blead (technical|program|product|project|solutions?|software|data|engineer|manager)\b", re.I)


def overlevel_title(title):
    """Senior-IC and executive titles screened out at 2 years of program management on paper
    (Staff / Principal / Lead roles drew the fastest rejections). 'Team Lead' and 'Chief of Staff' stay in."""
    t = title or ""
    if re.search(r"\bchief of staff\b", t, re.I):
        return False
    return bool(OVERLEVEL_TITLE.search(t) or (LEAD_LEVEL.search(t) and not re.search(r"\bteam lead\b", t, re.I)))


def mark_overlevel_jobs(conn):
    for url, title in conn.execute("SELECT url, title FROM jobs WHERE status IN ('new','ready','staged','needs_you','low_fit')").fetchall():
        if overlevel_title(title):
            conn.execute("UPDATE jobs SET status='low_fit', notes='overlevel' WHERE url=?", (url,))

REMOTE = re.compile(r"remote|work from anywhere|work from home|distributed", re.I)
BAD_LOC = re.compile(r"hybrid|on-?site|in-office|in office", re.I)
NON_ENGLISH_TITLE = re.compile(r'\b(?:bilingual|multilingual|german|japanese|spanish|french|mandarin|chinese|korean|arabic|portuguese|italian|hindi|russian|dutch|polish)\b', re.I)


def remote_listing(location):
    return bool(REMOTE.search(location or '') and not BAD_LOC.search(location or ''))


def fit_floor(location, profile):
    return profile.get('remote_min_fit', profile.get('min_fit', 60)) if remote_listing(location) else profile.get('min_fit', 60)


def eligible_job(job, profile):
    salary_max = posted_salary_max(job['salary'])
    title = job['title'] or ''
    return ((job['fit'] or 0) >= fit_floor(job['location'], profile)
            and (salary_max is None or salary_max >= pay_floor(job.get('location', ''), profile))
            and not NON_ENGLISH_TITLE.search(title)
            and not any(term in title.lower() for term in BAD_TITLE)
            and role_ok(title) and not overlevel_title(title))
US_OK = re.compile(r"united states|\busa\b|\bu\.s\.|\bus\b|americas|north america|anywhere|worldwide" + "".join(rf"|\b{re.escape(s)}\b" for s in HOME_STATES if len(s) > 2), re.I)
BAD_TITLE = ("machine learning engineer", "ml engineer", "data engineer", "security engineer", "fullstack", "full stack", "full-stack",
             "ios ", "android", "firmware", "scientist", "engineering manager", "hr business partner", "people business partner",
             "financial analyst", "legal", "business development rep", "executive business partner", "creative", "product designer",
             "distinguished engineer", "staff engineer", "software architecture", "solutions architecture", "ai engineer", "deep learning", "benefits", "campaign", "design program", "data center", "network", "revenue", "gtm", "payroll", "accounting",
             "people operations", "talent", "legal operations", "finance", "software engineer", "account executive", "sales", "physician", "nurse", "intern",
             "recruiter", "counsel", "attorney", "designer", "frontend", "backend", "machinist", "business development", "seller", "driver", "technician", "nurse practitioner", "pharmacist", "nursing", "certified nursing", "caregiver", "phlebotom", "recruiting", "store manager", "security officer", "residence life", "culinary", "fellows program", "fellowship", "research scientist", "member of technical staff", "financial systems",
             # customer-facing roles are out, remote or not; CSM and partner manager allowed back in same day
             "medical communications", "insurance agent", "insurance producer", "insurance advisor", "licensed agent", "life insurance", "final expense", "benefits advisor",
             "customer service", "customer support", "customer care", "customer experience", "client services", "client service", "account manager", "support specialist", "support representative", "support agent", "call center", "contact center", "help desk", "helpdesk", "engagement manager", "customer onboarding", "client onboarding", "onboarding specialist", "solutions consultant", "pre-sales", "presales", "concierge", "client relations", "customer relations", "member services", "patient services", "guest services", "receptionist")


# Defaults are tuned for program/project/operations/AI roles. Override in profile.json:
#   "title_filter": regex of titles worth a detail call, "exclude_titles": [substrings that disqualify a title]
if _PROFILE.get("title_filter"):
    TRACK_TITLE = re.compile(_PROFILE["title_filter"], re.I)
if _PROFILE.get("exclude_titles") is not None:
    BAD_TITLE = tuple(t.lower() for t in _PROFILE["exclude_titles"])


PRODUCT_ROLE = re.compile(r"\bproduct (manager|management|lead|owner|director)\b|\bhead of product\b|\b(vp|vice president),? (of )?product\b|\bgroup product\b", re.I)
PROGRAM_OR_OPS = re.compile(r"\b(program|programs|operations|ops|implementation|delivery operations)\b", re.I)


# Individual-contributor engineering/developer titles are out unless the role is program/project management.
IC_TECH = re.compile(r"\b(engineer|developer|devops|sre|site reliability|architect|mlops|machine learning|\.net|c#|programmer)\b", re.I)
MGMT_OK = re.compile(r"\b(program|project|implementation|delivery|operations) (manager|lead|director)\b|\bmanager,? (technical )?program", re.I)

def miles_from_home(loc):
    import math
    best = None
    if not HOME:
        return None
    states = "|".join(map(re.escape, HOME_STATES + METRO_STATES)) or "$^"
    for town, (la, lo) in LOCAL_PLACES.items():
        if re.search(rf"\b{re.escape(town)}\b", loc or "", re.I) and re.search(rf"\b({states})\b", loc or "", re.I):
            d = 3959 * 2 * math.asin(math.sqrt(math.sin(math.radians(la - HOME[0]) / 2) ** 2 + math.cos(math.radians(HOME[0])) *
                                               math.cos(math.radians(la)) * math.sin(math.radians(lo - HOME[1]) / 2) ** 2))
            best = d if best is None else min(best, d)
    return best


def pay_floor(location, profile):
    """Lowest posted pay worth applying to. A short commute earns a lower floor
    (profile 'local_min_salary' within 'short_commute_miles', default 40) than remote or long-commute roles."""
    floor = profile.get("min_salary", 0)
    if profile.get("local_min_salary") is not None and not REMOTE.search(location or "") \
            and local_ok(location, profile.get("short_commute_miles", 40)):
        return min(floor, profile["local_min_salary"])
    return floor


def local_ok(loc, radius=40):
    d = miles_from_home(loc)
    return d is not None and d <= radius


def metro_ok(loc, radius=30):
    """Listing in the optional second commute zone (profile location.metro): named town + metro state, or the metro name alone."""
    import math
    loc = loc or ""
    if not METRO_CENTER:
        return False
    states = "|".join(map(re.escape, METRO_STATES)) or "$^"
    for town, (la, lo) in METRO_PLACES.items():
        if re.search(rf"\b{re.escape(town)}\b", loc, re.I) and (town == METRO_NAME or re.search(rf"\b({states})\b", loc, re.I)):
            d = 3959 * 2 * math.asin(math.sqrt(math.sin(math.radians(la - METRO_CENTER[0]) / 2) ** 2 + math.cos(math.radians(METRO_CENTER[0])) *
                                               math.cos(math.radians(la)) * math.sin(math.radians(lo - METRO_CENTER[1]) / 2) ** 2))
            if d <= radius:
                return True
    return False


AI_ROLE = re.compile(r"\b(AI|A\.I\.|artificial intelligence|gen ?ai|generative ai|llm|ml ?ops|machine learning)\b", re.I)
AI_ROLE_KIND = re.compile(r"\b(program|project|operations|ops|governance|responsible|risk|implementation|enablement|transformation|"
                          r"strategy|adoption|solutions?|delivery|deployment|consultant|integration|policy|compliance)\b", re.I)


def role_ok(title):
    """Product-management titles only qualify when the role is clearly program/operations work."""
    title = title or ""
    if IC_TECH.search(title) and not MGMT_OK.search(title):
        return False
    return not (PRODUCT_ROLE.search(title) and not PROGRAM_OR_OPS.search(title))


CONTRACT = re.compile(r"\b(contract|contractor|1099|c2h|contract[- ]to[- ]hire|temp[- ]to[- ]hire|temporary|freelance|fixed[- ]term|consultant \(contract\)|\d+[- ]month (contract|assignment))\b", re.I)


EU_COUNTRIES = {"austria": "at", "belgium": "be", "bulgaria": "bg", "croatia": "hr", "cyprus": "cy", "czech republic": "cz",
                "czechia": "cz", "denmark": "dk", "estonia": "ee", "finland": "fi", "france": "fr", "germany": "de", "greece": "gr",
                "hungary": "hu", "ireland": "ie", "italy": "it", "latvia": "lv", "lithuania": "lt", "luxembourg": "lu", "malta": "mt",
                "netherlands": "nl", "poland": "pl", "portugal": "pt", "romania": "ro", "slovakia": "sk", "slovenia": "si",
                "spain": "es", "sweden": "se"}
US_STATE_CODES = ("al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms mo mt ne nv nh nj nm ny nc nd oh ok "
                  "or pa ri sc sd tn tx ut vt va wa wv wi wy dc").split()
US_PLACES = r"\b(sf|nyc|san francisco|new york|seattle|chicago|boston|austin|denver|atlanta|los angeles|la|dallas|miami|" \
            r"washington|portland|phoenix|san diego|san jose|philadelphia|minneapolis|madison|milwaukee)\b"
REGION_WORDS = {"us": (r"united states|\busa\b|\bu\.s\.|\bus\b|/ us\b", US_PLACES,
                       r"(?:^|[-;,(]\s*|remote\s*-\s*)(" + "|".join(US_STATE_CODES) + r")\b(?!\s*/)"),
                "ca": (r"canada|/ ca\b|\b(ontario|quebec|british columbia|alberta|toronto|vancouver|montreal)\b",),
                "uk": (r"united kingdom|\buk\b|\bu\.k\.|great britain|england|scotland|wales|northern ireland|/ gb\b|"
                       r"\b(london|manchester|edinburgh|glasgow|cardiff|belfast|bristol|birmingham|leeds)\b",),
                "eu": (r"european union|\beu\b|\bemea\b|\beurope\b",) + tuple(rf"\b{c}\b|/ {code}\b" for c, code in EU_COUNTRIES.items())}
OTHER_COUNTRY = re.compile(r"\b(united kingdom|uk|england|scotland|wales|costa rica|mexico|colombia|brazil|argentina|chile|peru|"
                           r"uruguay|india|pakistan|philippines|vietnam|indonesia|malaysia|singapore|thailand|japan|korea|china|"
                           r"australia|new zealand|south africa|nigeria|kenya|egypt|israel|turkey|uae|dubai|switzerland|norway|"
                           r"serbia|ukraine|georgia|armenia|latam|apac)\b", re.I)
OPEN_WORLD = re.compile(r"anywhere|worldwide|global|americas|north america|^\s*remote\s*$", re.I)


def remote_region_ok(location, profile):
    """profile.json "remote_regions" (e.g. ["us", "ca", "eu"]) limits remote roles to those regions; listings open
    worldwide or across the Americas still count. No setting: remote_anywhere decides, else US only."""
    regions = [r.lower() for r in (profile.get("remote_regions") or [])]
    loc = location or ""
    if not regions:
        return bool(US_OK.search(loc) or profile.get("remote_anywhere", REMOTE_ANYWHERE))
    if OPEN_WORLD.search(re.sub(r"(?i)^remote\s*[-,/]\s*", "", loc)) or OPEN_WORLD.search(loc.strip()):
        return True
    explicit = [p for r in regions for p in REGION_WORDS.get(r, ()) if p not in (US_PLACES, REGION_WORDS["us"][-1])]
    if any(re.search(p, loc, re.I) for p in explicit):
        return True
    if OTHER_COUNTRY.search(loc):
        return False   # 'San Jose, Costa Rica' is not San Jose, CA
    return any(re.search(p, loc, re.I) for r in regions for p in REGION_WORDS.get(r, ()))


_REQ_HERE = re.compile(r"\b(must|required to|need to|needs to|should|will need to|expected to|are required to)\s+(currently\s+)?"
                       r"(be\s+)?(based|located|living|residing|reside|live|relocate|resident)\b"
                       r"|\b(relocat\w+)\s+(to\s+\S+(\s+\S+){0,3}\s+)?(is\s+)?(required|mandatory|necessary)"
                       r"|\b(based|located|residing|resident)\s+in\s+[^.;]{1,40}?\s+(is\s+)?(required|only|mandatory)"
                       r"|\bopen\s+(only\s+)?to\s+(candidates|applicants|residents)\s+(who\s+(are|live)\s+)?(based\s+|located\s+|residing\s+)?in\b"
                       r"|\bwilling(ness)?\s+to\s+relocate\s+to\b", re.I)
_PAID_RELO = re.compile(r"relocation\s+(assistance|package|support|bonus|stipend|benefits?|costs?|expenses?|allowance)"
                        r"|\b(pay|pays|cover|covers|provide|provides|offer|offers|reimburse\w*)\s+(for\s+)?(your\s+|full\s+)?relocation"
                        r"|\b(paid|sponsored|funded)\s+relocation", re.I)
_NO_RELO = re.compile(r"\bno\s+relocation|relocation\s+(assistance\s+|support\s+|package\s+)?(is\s+|will\s+)?not\s+"
                      r"(be\s+)?(provided|offered|available|covered)|without\s+relocation|unable\s+to\s+(offer|provide)\s+relocation", re.I)


def requires_relocation_abroad(job):
    """A (remote) posting that requires living in, or moving to, a country other than the US,
    with no paid relocation. These are dropped: moving abroad only happens if the employer pays."""
    text = f"{job.get('title', '')}. {job.get('description', '')}"
    hit = next((sent for sent in re.split(r"(?<=[.!?\n])\s+", text)
                if _REQ_HERE.search(sent) and FOREIGN_COUNTRY.search(sent)
                and not re.search(r"\b(united states|u\.s\.|usa|us-based|north america|americas|anywhere|worldwide|globally)\b", sent, re.I)), None)
    if not hit:
        return False
    return not (_PAID_RELO.search(text) and not _NO_RELO.search(text))


# Remote listings tied to known scam-centre regions (job-offer lures into Southeast Asian scam compounds).
# profile.json "scam_regions" replaces this list.
SCAM_REGIONS = ("cambodia", "phnom penh", "sihanoukville", "myanmar", "burma", "yangon", "myawaddy", "shwe kokko",
                "laos", "vientiane", "golden triangle", "bokeo")
UNVERIFIED_SOURCES = ("adzuna", "himalayas", "remotive", "indeed", "google", "employer")      # aggregators: no employer-run application system behind them
ANON_COMPANY = re.compile(r"^\s*(confidential|private|stealth|anonymous|undisclosed|hiring company|company|n/?a|unknown|)\s*$", re.I)


def scam_listing(job, profile=None):
    """Why a remote listing looks scam-prone ('' if it doesn't): a scam-centre region, or an aggregator lead
    with recruitment-scam language (chat-app interviews, checks, payment apps, equipment purchases) or no named employer."""
    if not REMOTE.search(job.get("location") or ""):
        return ""
    text = f"{job.get('location', '')} {job.get('title', '')} {job.get('description', '')}".lower()
    regions = (profile or {}).get("scam_regions") or SCAM_REGIONS
    hit = next((r for r in regions if re.search(r"\b" + re.escape(r.lower()) + r"\b", text)), None)
    if hit:
        return f"scam-prone region: {hit}"
    if (job.get("source") or "") in UNVERIFIED_SOURCES:
        if ANON_COMPANY.match(job.get("company") or ""):
            return "remote lead with no named employer"
        import threat_scan
        flags = [why for pattern, points, why in threat_scan.RED_FLAGS if points >= 15 and re.search(pattern, text, re.I)]
        if len(flags) >= 2:   # one stray mention (a fintech posting naming Venmo) is normal; scams stack them
            return "scam language: " + "; ".join(flags[:2])
    return ""


def score(job, profile, eu):
    title, loc, body = job["title"].lower(), job["location"], job["description"].lower()
    remote_ok = not BAD_LOC.search(loc) and REMOTE.search(loc) and remote_region_ok(loc, profile)
    metro = (profile.get("metro") or profile.get("chicago", False)) and \
        metro_ok(loc, profile.get("metro_radius_miles", profile.get("chicago_radius_miles", 30)))
    if not remote_ok and not job.get("local") and not metro and not local_ok(loc, profile.get("local_radius_miles", 40)):
        return None
    if any(t in title for t in BAD_TITLE) or not role_ok(job["title"]):
        return None
    if requires_relocation_abroad(job):
        return None
    if scam_listing(job, profile):
        return None  # remote listing in a scam-centre region, or an aggregator lead that reads like a recruitment scam  # must live in / move to another country, and the employer doesn't pay to relocate
    if ckey(job.get("company", "")) in {ckey(c) for c in profile.get("blocked_companies", [])}:
        return None  # e.g. Coinbase: arbitration agreement on every application
    pts = sum(5 for t in ("project manager", "program manager", "implementation", "operations", "process") if t in title)
    if CONTRACT.search(title):
        pts += 10  # contract roles hire faster
    elif CONTRACT.search(body[:3000]):
        pts += 6
    if AI_ROLE.search(job["title"]) and AI_ROLE_KIND.search(job["title"]):
        pts += 12  # AI program / ops / governance / implementation roles are a priority
    pts += sum(2 for t in profile["focus_terms"] if t.lower() in title)
    pts += sum(1 for t in profile["focus_terms"] if t.lower() in body)
    pts -= sum(points for term, points in (profile.get('disqualifiers') or {}).items()
               if re.search(r'\b' + re.escape(term) + r'\b', body, re.I))
    return pts


_DISCOVERIES = {"n": 0}


def _discovery_budget(conn, company, profile):
    """Cached companies are free; at most workday_discovery_per_run new tenant probes per run."""
    try:
        if conn.execute("SELECT 1 FROM workday_tenants WHERE company=?", (company,)).fetchone():
            return True
    except sqlite3.OperationalError:
        pass
    if _DISCOVERIES["n"] >= profile.get("workday_discovery_per_run", 25):
        return False
    _DISCOVERIES["n"] += 1
    return True


def scan():
    profile, boards = json.loads(CONFIG.read_text()), json.loads(BOARDS.read_text())
    boards.setdefault('microsoft', {'base_url': 'https://apply.careers.microsoft.com',
                                     'domain': 'microsoft.com'})
    eu = {ckey(n) for n in boards.get("europe_hq", [])}
    ca = {ckey(n) for n in boards.get("canada_hq", [])}
    n = 0
    with db() as conn:
        for job in fetch_jobs(boards):
            if job.get('source') == 'adzuna' and score(job, profile, False) is not None:
                promote_adzuna(job)
            if job.get('source') in ('remotive', 'himalayas') and score(job, profile, False) is not None:
                promote_remote_board(job)
            if job.get('source') == 'adzuna' and profile.get('workday_auto', False) \
                    and score(job, profile, False) is not None and _discovery_budget(conn, job['company'], profile):
                try:
                    import workday
                    from difflib import SequenceMatcher
                    cfg = workday.discover_tenant(job['company'], conn)
                    if cfg:
                        match = next((wd for wd in fetch_workday(job['company'], cfg, max_pages=5)
                                      if SequenceMatcher(None, state.normalize_title(job['title']),
                                                         state.normalize_title(wd['title'])).ratio() >= .85), None)
                        if match:
                            job = match
                except Exception as e:
                    log(f"workday discovery failed {job['company']}: {str(e)[:100]}")
            is_eu = ckey(job["company"]) in eu
            hq = "Europe" if is_eu else "Canada" if ckey(job["company"]) in ca else "US"
            pts = score(job, profile, hq != "US")
            if pts is None or not job["url"]:
                continue
            if job['source'] in ATS_SOURCES and existing_ats_url(conn, job.get('apply_url') or job['url'], exclude=job['url']):
                continue
            status = 'manual' if job['source'] in ('adzuna', 'remotive', 'himalayas', 'indeed', 'google') else 'new'
            conn.execute("INSERT INTO jobs (url,company,title,location,description,score,source,hq,salary,token,job_id,apply_url,found_at,posted_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,date('now','localtime'),?) ON CONFLICT(url) DO UPDATE SET description=excluded.description, "
                         "posted_at=CASE WHEN jobs.posted_at='' THEN excluded.posted_at ELSE jobs.posted_at END, "
                         "score=excluded.score, salary=excluded.salary, location=excluded.location, "
                         "apply_url=CASE WHEN jobs.notes LIKE 'employer link found via %' THEN jobs.apply_url ELSE excluded.apply_url END, "
                         "updated=CURRENT_TIMESTAMP",
                         (job["url"], job["company"], job["title"], job["location"], job["description"], pts, job["source"],
                          hq, job["salary"], job["token"], job["job_id"], job["apply_url"], job.get("posted_at", "")))
            if job['source'] in ATS_SOURCES:
                conn.execute("UPDATE jobs SET source=?, apply_url=?, status='new', notes='' WHERE url=? AND source IN ('adzuna','remotive','himalayas','indeed','google') AND status IN ('manual','needs_you','new')",
                             (job['source'], job['apply_url'], job['url']))
            if status == 'manual':
                conn.execute("UPDATE jobs SET status='manual', notes=? WHERE url=? AND source IN ('adzuna','remotive','himalayas','indeed','google') AND status IN ('new','needs_you')",
                             (f"{job['source'].title()} lead: apply yourself", job['url']))
            n += 1
        refresh_adzuna_jobs(conn)
        import smart
        smart.score_jobs(conn, profile, log)
        bonus, floor = profile.get("europe_bonus", 3), profile.get("min_fit", 60)
        conn.execute("UPDATE jobs SET score = round(fit + CASE WHEN hq IN ('Europe','Canada') THEN ? ELSE 0 END)", (bonus,))
        conn.execute("UPDATE jobs SET score = score + 8 WHERE lower(title) GLOB '*contract*' OR lower(title) GLOB '*temporary*' "
                     "OR lower(title) GLOB '*freelance*' OR lower(title) GLOB '*1099*' OR lower(substr(description,1,3000)) GLOB '*contract-to-hire*' "
                     "OR lower(substr(description,1,3000)) GLOB '*contract to hire*' OR lower(substr(description,1,3000)) GLOB '*-month contract*'")  # contract roles first
        remote_floor = profile.get('remote_min_fit', floor)
        min_salary = profile.get('min_salary', 0)
        conn.create_function('is_remote_listing', 1, lambda location: int(remote_listing(location)))
        conn.create_function('pay_floor', 1, lambda location: pay_floor(location, profile))
        floor_sql = "CASE WHEN is_remote_listing(location) THEN ? ELSE ? END"
        conn.execute("UPDATE jobs SET status='low_fit' WHERE status='new' AND fit < " + floor_sql, (remote_floor, floor))
        # Leads (Adzuna, Remotive, Himalayas) skip the browser, but still have to clear the fit floor to reach the manual list.
        conn.execute("UPDATE jobs SET status='low_fit' WHERE status='manual' AND source IN ('adzuna','remotive','himalayas','indeed','google') AND fit < " + floor_sql, (remote_floor, floor))
        conn.execute("UPDATE jobs SET status='new', notes='' WHERE status='low_fit' AND notes != 'overlevel' AND fit >= " + floor_sql + " AND (salary_max(salary) IS NULL OR salary_max(salary) >= pay_floor(location))", (remote_floor, floor))
        ai_floor = profile.get("ai_min_fit", floor - 6)
        for url, title in conn.execute("SELECT url, title FROM jobs WHERE status='low_fit' AND fit >= ? AND notes != 'pay below floor' AND (salary_max(salary) IS NULL OR salary_max(salary) >= pay_floor(location))", (ai_floor,)).fetchall():
            if AI_ROLE.search(title or "") and AI_ROLE_KIND.search(title or "") and role_ok(title) \
                    and not any(t in (title or "").lower() for t in BAD_TITLE):
                conn.execute("UPDATE jobs SET status='new' WHERE url=?", (url,))
        conn.execute("UPDATE jobs SET status='low_fit', notes='pay below floor' WHERE status IN ('new','ready','manual') AND salary_max(salary) < pay_floor(location)")
        for url, title in conn.execute("SELECT url,title FROM jobs WHERE status IN ('new','ready')").fetchall():
            if NON_ENGLISH_TITLE.search(title or ''):
                conn.execute("UPDATE jobs SET status='low_fit', notes='language requirement' WHERE url=?", (url,))
        mark_overlevel_jobs(conn)
        for url, company, title, loc, desc, source in conn.execute("SELECT url, company, title, location, description, source FROM jobs "
                                                                  "WHERE status IN ('new','ready','manual','needs_you','captcha','staged')").fetchall():
            why = scam_listing(dict(company=company, title=title, location=loc, description=desc, source=source), profile)
            if not why and REMOTE.search(loc or "") and profile.get("remote_regions") and not remote_region_ok(loc, profile) \
                    and not local_ok(loc, profile.get("local_radius_miles", 40)):
                why = "remote outside allowed regions (" + ", ".join(profile["remote_regions"]).upper() + ")"
            if why:
                conn.execute("UPDATE jobs SET status='skip', notes=?, updated=CURRENT_TIMESTAMP WHERE url=?", (f"scam filter: {why}", url))
        for url, title, desc in conn.execute("SELECT url, title, description FROM jobs WHERE status IN ('new','ready','manual','needs_you')").fetchall():
            if requires_relocation_abroad({"title": title, "description": desc}):
                conn.execute("UPDATE jobs SET status='skip', notes='requires relocation abroad, no paid relocation', "
                             "updated=CURRENT_TIMESTAMP WHERE url=?", (url,))
        import employer_link
        link_result = employer_link.Finder(conn, boards, profile,
                                          budget=profile.get('employer_link_budget_seconds', 300)).run()
        log(f"employer links: {link_result['found']} found, {link_result['missed']} missed, "
            f"{link_result['unavailable']} unavailable, {link_result['duplicate']} duplicate")
        conn.execute("UPDATE jobs SET status='needs_you', notes='Microsoft account sign-in needed (one-time)' "
                     "WHERE source='microsoft' AND status='new'")
        conn.execute("UPDATE jobs SET status='needs_you', notes='Eightfold account sign-in needed' "
                     "WHERE source='eightfold' AND status='new'")
        good = conn.execute("SELECT count(*) FROM jobs WHERE fit >= ?", (floor,)).fetchone()[0]
        try:
            import signals
            signals.refresh(conn, log)
        except Exception as e:
            log(f"signals failed: {e}")
    log(f"scan: {n} remote US-eligible or local listings, {good} at fit >= {floor}")


# ---------------------------------------------------------------- answering
PROFILE_FIELDS = [(r"first name|given name", "first_name"), (r"last name|surname|family name", "last_name"),
                  (r"full (legal )?name|legal name|^name$|your name", "name"), (r"^e-?mail", "email"), (r"^(phone|mobile)", "phone"),
                  (r"linkedin", "linkedin"), (r"github|portfolio|^website|personal (site|url)", "website"),
                  (r"^(current )?location(?! preference)|^city|where are you (located|based)", "location"),
                  (r"(current|recent|previous) (company|employer)|^company$|^organi[sz]ation$|^most recent employer", "current_company"),
                  (r"(current|recent|previous) (job )?title|^title$", "current_title")]


def strip_label(label):
    return re.sub(r"[*✱]|\((required|optional)\)", "", label or "", flags=re.I).strip()


def clean_field_label(text):
    """Keep the first question line, excluding following option lines."""
    return next((line.strip()[:200] for line in (text or '').splitlines() if line.strip()), '')


def is_cover_file(field):
    return bool(re.search(r'cover.?letter', ' '.join(str(field.get(k, '')) for k in
                                           ('name', 'id', 'aria_label', 'label', 'heading')), re.I))


def resume_file_indices(fields):
    files = [f for f in fields if f.get('type') in ('file', 'input_file')]
    resume = lambda f: bool(re.search(r'resume|\bcv\b', ' '.join(str(f.get(k, '')) for k in ('name', 'id', 'aria_label', 'label', 'heading')), re.I))
    explicit = [f for f in files if resume(f) and not is_cover_file(f)]
    if explicit:
        return {f['i'] for f in explicit}, False
    required = [f for f in files if f.get('required') and not is_cover_file(f)]
    return ({required[0]['i']} if len(required) == 1 else set()), len(required) > 1


# Never agreed to automatically: these waive legal rights (to sue, to a jury, to join a
# class action, to take other work). They always go to "needs you" for the user to decide.
WAIVER = re.compile(r"arbitrat|waive|waiver|class[- ]action|jury trial|release (of )?(all )?claims|forum selection|"
                    r"\b(i agree|agree(ing)? to|accept(ing)?|consent(ing)? to|sign(ing)?|acknowledg(e|ing))\b.*(non-?compete|restrictive covenant|covenant not to|non-?solicit)|"
                    r"(non-?compete|restrictive covenant|covenant not to|non-?solicit).*\b(i agree|agree to|accept|consent to|sign)\b", re.I)


# Attestations about AI use or authorship: the bot drafts with AI, so a human must answer these.
AI_ATTEST = re.compile(r"\bAI\b.{0,80}\b(policy|responsible use|usage|guidelines?)\b|\b(artificial intelligence|generative ai|chatgpt|llm)\b.{0,80}\b(policy|use|used|assist)|"
                       r"\b(my|your) own (work|words)\b|written (entirely )?by (me|myself|you)\b|without (the )?(use|assistance|help) of (ai|artificial)", re.I)


# Employer discloses ITS OWN use of AI in screening; acknowledging that is not a claim about the user's work.
AI_NOTICE = re.compile(r"\b(we|our|[A-Z][\w&.-]+)\b.{0,40}\b(use|uses|may use|utilize|utilizes|leverage|leverages)\b.{0,60}"
                       r"(artificial intelligence|\bAI\b).{0,120}(review|screen|evaluat|assess|process)|"
                       r"\b(process|review|evaluate|compare)\b.{0,80}\busing (an? )?(artificial intelligence|\(?AI\)?)", re.I)
OWN_AI_CLAIM = re.compile(r"\bmy own (work|words)|reflect my own|without (the )?(use|assistance|help) of|did not use|will not use|"
                          r"do not use any ai|refrain from (using )?ai|not (to )?use (any )?ai|written (entirely )?by (me|myself)", re.I)


def ai_notice_only(text):
    return bool(AI_NOTICE.search(text) and not OWN_AI_CLAIM.search(text) and not WAIVER.search(text))


# Pledges NOT to use AI tools during interviews: approved to answer Yes.
AI_INTERVIEW_PLEDGE = re.compile(r"(do not|don't|not to|refrain from|without) (use )?(using )?(any )?(ai|artificial intelligence)( tools?)?.{0,60}\binterview|"
                                 r"^\s*ai (use )?policy for interview(ers|s)?\s*:?\s*$|\bai (use )?policy for interview", re.I)


def interview_pledge_only(text):
    return bool(AI_INTERVIEW_PLEDGE.search(text) and not re.search(r"my own work|reflect my own|application materials|resume|cover letter", text, re.I)
                and not WAIVER.search(text))


def restricted(text):
    """Waivers and claims about the user's own AI use always go to the human."""
    if interview_pledge_only(text):
        return False
    return bool(WAIVER.search(text) or (AI_ATTEST.search(text) and not ai_notice_only(text)))


WORK_AUTH_Q = re.compile(r"authori[sz]|sponsor|visa|eligib\w* to work|right to work|legally (able|permitted|entitled) to work|work permit|citizen", re.I)
FOREIGN_COUNTRY = re.compile(r"\b(uk|u\.k\.|united kingdom|great britain|britain|england|scotland|ireland|canada|mexico|brazil|argentina|colombia|chile|"
                             r"europe|european union|eu|eea|germany|france|spain|portugal|italy|netherlands|belgium|switzerland|austria|"
                             r"poland|sweden|denmark|norway|finland|czech\w*|romania|greece|israel|uae|united arab emirates|india|"
                             r"singapore|japan|korea|china|hong kong|taiwan|philippines|australia|new zealand|south africa|nigeria|kenya)\b", re.I)


def foreign_work_question(label, job=None):
    """A work-authorization question about a country other than the US (named, or implied by a non-US posting).
    Only a US work status is on file, so these always go to the user."""
    if not WORK_AUTH_Q.search(label):
        return False
    if FOREIGN_COUNTRY.search(label):
        return True
    loc = (job or {}).get("location") or ""
    names_us = re.search(r"\b(united states|u\.s\.|usa|us)\b", label, re.I)
    return bool(not names_us and loc and FOREIGN_COUNTRY.search(loc) and not US_OK.search(loc))


def foreign_work_answer(label, prof):
    """Truthful answer to a work-authorization question about another country. Only US status is on file:
    the user has no right to work there, would work remotely from the US, and moves only with paid relocation."""
    if not (prof or {}).get("us_citizen"):
        return ""   # nothing on file to answer from: leave it to the user
    remote = "I'm a US citizen and would work remotely from the United States (as a contractor or through an employer of record)"
    if re.search(r"sponsor|visa|work permit", label, re.I):
        return (f"Yes - {remote}, which needs no visa. Working in the country itself would require sponsorship "
                f"and a paid relocation package.|Yes")
    return f"No - {remote}; I don't hold work authorization there and would relocate only with a paid relocation package.|No"


def resolve(label, prof, bank, company):
    l = strip_label(label)
    if foreign_work_question(l, CURRENT_JOB):
        return foreign_work_answer(l, prof)
    if EMPLOYER_HISTORY_Q.search(l):
        if any(ckey(name) in ckey(l) or ckey(company) == ckey(name) for name in past_employers(prof)):
            return ''
        return 'No'
    if COMP_Q.search(l) and not COMP_ACCEPT.search(l):
        return comp_answer(CURRENT_JOB)
    if ai_notice_only(l):
        return "__agree__|Yes|Acknowledge|I acknowledge|I understand"
    if interview_pledge_only(l):
        return "Yes|__agree__|I agree|Acknowledge"
    if WAIVER.search(l) or AI_ATTEST.search(l):
        return ""
    if len(l) < 60:
        for pat, key in PROFILE_FIELDS:
            if re.search(pat, l, re.I) and prof.get(key):
                return prof[key]
    with db() as conn:
        if prof.get('us_citizen') and re.search(r'spon(?:sor|or)|work authori[sz]ation', l, re.I):
            approved = answer_store.resolve(conn, 'sponsor', company)[0]
            if approved:
                return 'No' if re.search(r'spon(?:sor|or)', l, re.I) else 'Yes'
        return answer_store.resolve(conn, l, company)[0]


def past_employers(prof):
    """Employers the user has worked for: profile.json past_employers, else the companies in resume_facts.json."""
    if (prof or {}).get('past_employers'):
        return tuple(prof['past_employers'])
    try:
        jobs = json.loads((ROOT / 'resume_facts.json').read_text()).get('jobs') or {}
        return tuple(v[1] for v in jobs.values() if isinstance(v, (list, tuple)) and len(v) > 1 and v[1])
    except Exception:
        return ()
EMPLOYER_HISTORY_Q = re.compile(r'\b(?:previously\s+(?:worked|been employed|consulted)|ever\s+(?:worked|been employed|consulted)|been employed\s+(?:at|by)|consulted\s+for\s+.*previously|former\s+.*employee|employee\s+of\s+.*(?:currently|previously)|worked\s+(?:at|for)\s+.*before|worked\s+(?:with|for)\s+us\s+(?:before|previously)|have\s+you\s+(?:ever\s+)?worked\s+(?:here|with\s+us))\b', re.I)



# ---------------------------------------------------------------- compensation
# Posted range -> bracket at/just above its midpoint, never below $100k.
# No posted range -> senior/tech titles $125-150k, everything else $100-125k.
CURRENT_JOB = {}
COMP_Q = re.compile(r"desired (compensation|salary|pay)|compensation (expectation|requirement)s?|salary (expectation|requirement|range)s?|"
                    r"expected (salary|compensation|pay)|target (salary|compensation)", re.I)
COMP_ACCEPT = re.compile(r"\b(accept|agree|comfortable|listed|posted|okay|ok with|aligned?|align)\b", re.I)
SENIOR_TITLE = re.compile(r"\b(technical program manager|tpm|director|senior|sr\.?|lead|principal|staff|head of|vp|vice president)\b", re.I)
_MONEY = r"\$?\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d)?\s?[kK])"


def _money(tok):
    tok = tok.replace(',', '').replace('$', '').strip()
    return int(float(tok[:-1]) * 1000) if tok[-1:] in 'kK' else int(tok)


def posted_range(job):
    """(lo, hi) annual USD from the posting's salary field or description; Adzuna estimates ignored."""
    for text in (str(job.get('salary') or ''), str(job.get('description') or '')[:20000]):
        if not text or text.startswith('est.'):
            continue
        for m in re.finditer(_MONEY + r"\s*(?:-|\u2013|\u2014|to)\s*" + _MONEY, text):
            lo, hi = _money(m.group(1)), _money(m.group(2))
            if 30000 <= lo < hi <= 600000:
                return lo, hi
    return None


ENTRY_TITLE = re.compile(r"\b(associate|coordinator|specialist|junior|jr\.?|entry|assistant|representative|analyst i\b|level i\b)", re.I)


def comp_target(job):
    """Posted range: its midpoint (floor $100k only when the range reaches $100k). No range: by seniority."""
    rng = posted_range(job or {})
    title = (job or {}).get('title', '')
    if rng:
        mid = (rng[0] + rng[1]) // 2
        return max(mid, 100000) if rng[1] >= 100000 else mid
    if SENIOR_TITLE.search(title):
        return 137500
    return 85000 if ENTRY_TITLE.search(title) else 112500


def comp_answer(job):
    """Token for bracket pickers plus a text form, e.g. '$125,000 - $150,000'."""
    t = comp_target(job)
    lo = (t // 25000) * 25000 if t < 100000 else max(100000, (t // 25000) * 25000)
    return f"${lo:,} - ${lo + 25000:,}|__salary__:{t}"


def _pick_salary_bracket(n, options):
    best = None
    for o in options:
        nums = [_money(x) for x in re.findall(_MONEY, o)]
        if len(nums) >= 2 and nums[0] <= n <= nums[1]:
            best = o  # boundary values fall in the higher bracket (later option wins)
        elif len(nums) == 1 and re.search(r"\+|or more|and above|over|greater", o, re.I) and n >= nums[0]:
            best = o
    return best

def _set_current_job(job):
    """apply.py may run as __main__ while workday.py imports 'apply': set both module globals."""
    import sys as _sys
    for _m in ('apply', '__main__'):
        mod = _sys.modules.get(_m)
        if mod is not None and hasattr(mod, 'comp_answer'):
            mod.CURRENT_JOB = dict(job or {})


def text_answer(val):
    if not val:
        return ""
    return next((a for a in val.split("|") if a and not a.startswith("__")), "")


def _unused():
    return ""


# Equivalent option spellings forms use for the same answer.
OPTION_ALIASES = (
    ("united states", "united states of america", "usa", "us", "u.s.", "u.s.a.", "america"),
    ("united kingdom", "uk", "u.k.", "great britain"),
)


OPTION_CLAIMS = re.compile(r"visa|citizen|green card|permanent resident|\bresident\b|spon(?:sor|or)|authori[sz]|clearance|"
                           r"degree|diploma|licen[sc]e|certif|disab|veteran|convict|criminal|felony|\d+\+? ?years?", re.I)


US_CITIZEN = False  # set from profile.json by prepare_profile; only a stated fact unlocks citizen options


def _yes_no_sentence(answer, options):
    """'Yes' / 'No' into options written as sentences ('Yes, I am authorized to work...'), when exactly one starts that way."""
    a = answer.strip().lower()
    if a not in ("yes", "no"):
        return None
    hits = [o for o in options if re.match(rf"\W*{a}\b", o.strip(), re.I)]
    if len(hits) != 1:
        return None
    o = hits[0]
    # never let a bare Yes/No adopt a sentence that claims more (a visa, a green card, a temporary status)
    plain_authorized = US_CITIZEN and re.search(r"authori[sz]", o, re.I) and not re.search(
        r"visa|temporar|green card|permanent resident|sponsor|\bead\b|\bopt\b|h-?1b|not\b", o, re.I)
    return o if (not OPTION_CLAIMS.search(o) or plain_authorized) else None


def pick_option(answer, options):
    if "|" not in answer and options:
        hit = _yes_no_sentence(answer, options)
        if hit and not any(o.strip().lower() == answer.strip().lower() for o in options):
            return hit
    if "|" in answer:
        for alt in answer.split("|"):
            o = pick_option(alt, options)
            if o:
                return o
        return None
    if answer.startswith("__salary__:"):
        return _pick_salary_bracket(int(answer.split(":", 1)[1]), options)
    a = answer.strip().lower()
    if a in ('master', "master's degree", 'mba'):
        graduate = [o for o in options if re.search(r"master(?:'s|’s)?(?: degree)?|\bmba\b|master of business administration", o, re.I)]
        if len(graduate) == 1:
            return graduate[0]
    pats = {"__decline__": ["decline", "i don't wish", "i do not wish", "i do not want to answer", "i don't want to answer", "prefer not", "choose not"],
            "__agree__": ["i agree", "agree", "yes", "i acknowledge", "acknowledge", "acknowledged", "i consent", "consent",
                          "confirmed", "i confirm", "confirm", "i accept", "accept", "accepted", "i understand", "understood",
                          "i have read", "i've read"]}.get(a, [a])
    for alias_set in OPTION_ALIASES:
        if a in alias_set:
            pats = [a] + [x for x in alias_set if x != a]
            break
    for p in pats:
        for o in options:
            if o.strip().lower() == p or _norm_opt(o).replace("careers", "career") == _norm_opt(p).replace("careers", "career"):
                return o
    sponsor = _sponsor_option(a, options)
    if sponsor:
        return sponsor
    for p in pats:
        hits = list(dict.fromkeys(o for o in options if re.match(rf"\s*{re.escape(p)}\b", o, re.I)))
        if len({_norm_opt(h) for h in hits}) > 1 and US_CITIZEN:
            mine = [h for h in hits if re.search(r"\bU\.?S\.?A?\.? citizen|united states citizen", h, re.I)
                    and not re.search(r"visa|sponsor|temporary", h, re.I)]
            if len(mine) == 1:
                return mine[0]
        if len({_norm_opt(h) for h in hits}) > 1:
            # e.g. "No - I am a citizen" vs "No - I hold a visa": the bare answer cannot choose
            # between distinct claims, so never guess; the human answers.
            return None
        if hits:
            rest = re.sub(rf"^\s*{re.escape(p)}\b", "", hits[0], flags=re.I)
            citizen_only = US_CITIZEN and re.search(r"\bU\.?S\.?A?\.? citizen|united states citizen", rest, re.I)
            if OPTION_CLAIMS.search(rest) and not citizen_only:
                # "No - I hold a visa ..." adds a status claim the bare answer never made: a human decides.
                return None
            return hits[0]
    return _semantic_option(a, options) or _fuzzy_option(a, options)


# Sentence-style options for short stored answers.
NO_SPONSOR = re.compile(r"\b(do not|don't|will not|won't|never|not)\s+(currently\s+)?(require|need)\b[^.]{0,60}sponsor|"
                        r"no restriction|without (the need for |requiring |needing )?(visa )?sponsorship|"
                        r"will not require sponsorship|not require (visa )?sponsorship", re.I)
NEED_SPONSOR = re.compile(r"\b(will|may|do|currently|would)\s+(require|need)\b[^.]{0,40}sponsor|\bneed sponsorship|"
                          r"temporary visa|hold a visa|visa status|not currently authori[sz]ed|"
                          r"\bno,? i (need|require)", re.I)
NEGATION = re.compile(r"^\W*(i\s+)?(have\s+not|haven't|haven\u2019t|do\s+not|don't|don\u2019t|am\s+not|was\s+not|never|not)\b", re.I)


def _sponsor_option(a, options):
    """Yes/No to an authorization or sponsorship question whose options are full sentences: a stated
    US citizen can truthfully pick the one option saying no sponsorship is needed, when the others say it is."""
    if a not in ("yes", "no") or not US_CITIZEN:
        return None
    ok = [o for o in options if (NO_SPONSOR.search(o) or re.search(r'\b(?:US|U\.S\.|United States) citizen\b|\bpermanent(?:ly)? authori[sz]ed\b', o, re.I))
          and not re.search(r'temporary visa|hold a visa|visa status|not currently authori[sz]ed', o, re.I)]
    if len(ok) == 1 and (len(options) == 2 or any(NEED_SPONSOR.search(o) for o in options if o != ok[0])):
        return ok[0]
    return None


def _semantic_option(a, options):
    """Short answers into sentence options, only where the option claims nothing beyond the answer
    (or, for sponsorship/authorization, only what the profile's stated US citizenship makes true)."""
    if a == "no" and not any(re.match(r"\W*(yes|no)\b", o, re.I) for o in options):
        neg = [o for o in options if NEGATION.search(o)]
        if len(neg) == 1 and not OPTION_CLAIMS.search(neg[0]):
            return neg[0]
    if len(a) >= 4 and not a.startswith("__"):
        hits = [o for o in options if re.search(rf"(?<![\w-]){re.escape(a)}(?![\w-])", _norm_opt(o)) and not OPTION_CLAIMS.search(o)]
        if len(hits) == 1:
            return hits[0]
    return None


def _norm_opt(text):
    return re.sub(r"\s+", " ", text.replace("\u2019", "'").replace("\u2018", "'").strip().lower())


def _fuzzy_option(a, options):
    """Numeric answers into range options; option text that prefixes the answer."""
    a = _norm_opt(a)
    num = re.fullmatch(r"(\d+(?:\.\d+)?)\+?(?: years?)?", a)
    if num:
        n = float(num.group(1))
        for o in options:
            t = _norm_opt(o)
            m = re.match(r"(\d+)\s*(?:-|to|–)\s*(\d+)", t)
            if m and float(m.group(1)) <= n <= float(m.group(2)):
                return o
            m = re.match(r"(\d+)\s*\+|(\d+) or more|more than (\d+)", t)
            if m:
                lo = float(next(g for g in m.groups() if g))
                if n >= lo + (1 if 'more than' in t else 0):
                    return o
            m = re.match(r"(?:less than|under|fewer than) (\d+)", t)
            if m and n < float(m.group(1)):
                return o
        return None
    def core(o):
        t = _norm_opt(o)
        return t[:-2] if t.endswith("'s") else t
    hits = [o for o in options if len(core(o)) >= 3 and re.match(rf"{re.escape(core(o))}(?:'s)?\b", a)]
    if hits:
        return max(hits, key=len)
    return _location_option(a, options)


US_ALIASES = ("united states", "united states of america", "usa", "us", "u.s.", "u.s.a.")


def _location_option(a, options):
    """'Columbus, OH, United States' into region options like 'US - Ohio' or 'United States'."""
    parts = [x.strip() for x in a.split(",") if x.strip()]
    if len(parts) < 2:
        return None
    state = next((US_STATES[x.upper()] for x in parts if x.upper() in US_STATES), None)
    if state:
        st = [o for o in options if re.search(rf"\b{re.escape(state)}\b", o, re.I) and "west " + state.lower() not in o.lower()]
        if st:
            return min(st, key=len)
    if any(x.lower() in US_ALIASES for x in parts):
        us = [o for o in options if _norm_opt(o) in US_ALIASES]
        if us:
            return us[0]
    return None


def prepare_profile(p):
    global US_CITIZEN
    US_CITIZEN = bool(p.get('us_citizen'))
    import sys as _sys
    for _m in ('apply', '__main__'):
        if _m in _sys.modules and hasattr(_sys.modules[_m], 'pick_option'):
            _sys.modules[_m].US_CITIZEN = US_CITIZEN
    parts = p["name"].split()
    return {**p, "first_name": parts[0], "last_name": parts[-1], "resume": str(Path(p["resume"]).expanduser().resolve())}


# ---------------------------------------------------------------- greenhouse
def gh_plan(token, job_id, prof, bank, company, mfields=None, resolver=None, questions=None):
    d = questions if questions is not None else get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}?questions=true")
    resolver = resolver or resolve
    actions, missing = [], []
    mfields = mfields if mfields is not None else []
    all_questions = (d.get("questions") or []) + (d.get("location_questions") or [])
    file_fields = [dict(i=n, type='input_file', name=f.get('name', ''), label=clean(q.get('label', '')),
                        required=q.get('required', False)) for n, q in enumerate(all_questions)
                   for f in (q.get('fields') or [])[:1] if f.get('type') == 'input_file']
    resume_ids, ambiguous = resume_file_indices(file_fields)
    for n, q in enumerate(all_questions):
        label, req, fields = clean_field_label(clean(q.get("label", ""))), q.get("required", False), q.get("fields") or []
        if not fields or re.fullmatch(r"latitude|longitude", label, re.I):
            continue
        f = fields[0]
        name, typ = f.get("name", ""), f.get("type", "")
        if typ == "input_file":
            if n in resume_ids:
                actions.append((name, "file", prof["resume"]))
            elif req:
                missing.append((label, 'ambiguous_file') if ambiguous and not is_cover_file({'name': name, 'label': label}) else label)
            continue
        val = resolver(label, prof, bank, company)
        if typ.startswith("multi_value"):
            opt = pick_option(val, [v.get("label", "") for v in f.get("values", [])]) if val else None
            if opt:
                actions.append((name, "select" if "single" in typ else "check", opt))
            elif req:
                missing.append(label)
        elif text_answer(val):
            actions.append((name, "text", text_answer(val)))
        else:
            kind = "textarea" if typ == "textarea" else "text"
            if req:
                missing.append(label)
            mfields.append((label, name, kind, req))
    edu = prof.get("education") or {}
    if d.get("education") in ("education_required", "education_optional") and edu.get("school"):
        actions.append(("school--0", "select", edu["school"]))
        if edu.get("degree"):
            actions.append(("degree--0", "select", edu["degree"]))
        if edu.get("discipline"):
            actions.append(("discipline--0", "select", edu["discipline"]))
    actions.extend(gh_employment_actions(d, prof))
    return actions, missing


MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")


def gh_employment_actions(d, prof, today=None):
    """Greenhouse employment section (company-name-0, title-0, start/end month+year, current-role-0_1)."""
    emp = prof.get("employment") or {}
    if d.get("employment") not in ("employment_required", "employment_optional") or not emp.get("company"):
        return []
    today = today or dt.date.today().isoformat()
    acts = [("company-name-0", "text", emp["company"])]
    if emp.get("title"):
        acts.append(("title-0", "text", emp["title"]))
    if emp.get("start_month"):
        acts.append(("start-date-month-0", "select", emp["start_month"]))
    if emp.get("start_year"):
        acts.append(("start-date-year-0", "text", str(emp["start_year"])))
    end = str(emp.get("end") or "")
    if not end or end >= today:
        acts.append(("current-role-0_1", "tick", "1"))
    else:
        y, m = end[:4], int(end[5:7])
        acts.append(("end-date-month-0", "select", MONTHS[m - 1]))
        acts.append(("end-date-year-0", "text", y))
    return acts


OPTIONAL_GH_FIELDS = ("discipline--0",)


def _read_back(box, kind, expected):
    if kind == "file":
        # Greenhouse/Lever widgets swap the <input> out after upload and show the
        # filename instead, so a detached input must not stall for 30s.
        try:
            if box.evaluate("e => e.files && e.files.length", timeout=3000):
                return True
        except Exception:
            pass
        name = Path(str(expected)).name
        try:
            box.page.wait_for_timeout(1500)
            return box.page.get_by_text(name, exact=False).count() > 0 or \
                box.page.get_by_text(Path(name).stem, exact=False).count() > 0
        except Exception:
            return False
    if kind in ("check", "click"):
        return box.is_checked()
    if kind == "select" and box.evaluate("e => e.tagName") == "SELECT":
        return box.locator("option:checked").first.inner_text().strip() == expected
    got, want = box.input_value().strip(), str(expected).strip()
    if got == want:
        return True
    norm = lambda v: re.sub(r"[^a-z0-9]", "", v.lower().replace("https://", "").replace("http://", "").replace("www.", ""))
    if norm(got) == norm(want):
        return True
    digits = lambda v: re.sub(r"\D", "", v)
    return len(digits(want)) >= 10 and digits(got).endswith(digits(want)[-10:])


LOCATION_IDS = ("candidate-location", "location")
US_STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
             "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
             "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
             "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
             "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
             "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
             "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
             "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
             "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
             "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"}


def _gh_box(page, name):
    ids = LOCATION_IDS if name in LOCATION_IDS else (name,)
    for ident in ids:
        box = page.locator(f"[id='{ident}']")
        if box.count():
            return box.first, ident
    return None, name


def _rs_control(box):
    return box.locator("xpath=ancestor::div[contains(@class,'select__control')][1]")


def _rs_shown(box):
    ctl = _rs_control(box)
    if not ctl.count():
        return []
    return [t.strip() for t in ctl.locator("[class*=single-value], [class*=singleValue], "
                                           "[class*=multi-value__label], [class*=multiValue__label]").all_inner_texts()]


def _rs_options(page, ident):
    return page.locator(f"[id^='react-select-{ident}-option']")


def _rs_pick(page, box, ident, val, typed=None):
    """Choose an option in a Greenhouse react-select combobox."""
    page.keyboard.press("Escape")
    box.scroll_into_view_if_needed()
    box.click(force=True)
    if typed:
        page.keyboard.type(typed, delay=30)
        page.wait_for_timeout(2500)
    else:
        page.wait_for_timeout(400)
    opts = _rs_options(page, ident)
    texts = [t.strip() for t in opts.all_inner_texts()]
    choice = pick_option(val, texts) if texts else None
    if not choice and typed:
        parts = [x.strip().lower() for x in val.split(",")]
        want = [US_STATES.get(x.upper(), x).lower() for x in parts]
        choice = next((t for t in texts if all(w in t.lower() for w in want)), None) or \
            next((t for t in texts if parts[0] in t.lower()), None)
    if not choice:
        page.keyboard.press("Escape")
        raise RuntimeError(f"required option unavailable: {val[:60]}")
    opts.nth(texts.index(choice)).click()
    page.wait_for_timeout(300)
    page.keyboard.press("Escape")
    return choice


def gh_fill(page, actions, tolerant=False):
    results = []
    for name, kind, val in actions:
        shown_val = val
        try:
            if kind == "file":
                box = page.locator(f"input[type=file][id='{name}'], input[type=file][name='{name}']")
                box = box.first if box.count() else page.locator("input[type=file]").first
                log(f"upload file: {attachment_details(val)}")
                box.set_input_files(val)
                ok = _read_back(box, kind, val)
            else:
                box, ident = _gh_box(page, name)
                if box is None and name in OPTIONAL_GH_FIELDS:
                    results.append(dict(key=name, kind=kind, status="skipped", error="", value=val))
                    continue
                if box is None:
                    raise RuntimeError(f"field not found on page: {name}")
                combo = box.get_attribute("role") == "combobox"
                if combo:
                    typed = val.split(",")[0].strip() if ident in LOCATION_IDS else (val.split("|")[0] if ident == "country" else
                            (val.split("|")[0].split(" - ")[-1] if ident.startswith("school") else None))
                    for v in ([val] if kind != "check" else [x for x in val.split("|") if x] or [val]):
                        shown_val = _rs_pick(page, box, ident, v, typed=typed)
                    shown = _rs_shown(box)
                    # phone-country widget displays only the dial code ("United States +1" shows as "+1")
                    ok = shown_val in shown or (ident == "country" and any(x and shown_val.endswith(" " + x) for x in shown))
                elif kind == "text":
                    box.fill(val)
                    ok = _read_back(box, kind, val)
                elif kind == "select":
                    box.select_option(label=val)
                    ok = _read_back(box, kind, val)
                elif kind == "tick":
                    box.check(force=True)
                    ok = box.is_checked()
                elif kind == "check":
                    target = page.locator(f"input[name='{name}'][value='{val}']")
                    if not target.count():
                        # New Greenhouse boards: <fieldset id=name> with inputs valued by API id and a
                        # <label for=...> carrying the option text. Click the label so React sees it.
                        scope = page.locator(f"fieldset[id='{name}']")
                        lab = (scope.first if scope.count() else page).locator("label").filter(
                            has_text=re.compile(rf"^\s*{re.escape(val)}\s*$")).first
                        fid = lab.get_attribute("for") if lab.count() else None
                        target = page.locator(f"[id='{fid}']") if fid else page.get_by_label(val, exact=True)
                        if lab.count() and not target.first.is_checked():
                            lab.click()
                    if not target.first.is_checked():
                        target.first.check(force=True)
                    box = target.first
                    ok = box.is_checked()
                else:
                    ok = _read_back(box, kind, val)
            page.wait_for_timeout(150)
            if not ok:
                raise ValueError("value readback mismatch")
            results.append(dict(key=name, kind=kind, status="filled", error="", value=shown_val))
        except Exception as e:
            results.append(dict(key=name, kind=kind, status="failed", error=str(e).splitlines()[0][:200], value=val))
            if not tolerant:
                raise
    return results


# ---------------------------------------------------------------- lever / ashby (generic form)
FIELDS_JS = r"""() => {
  const vis = e => e.type === 'file' || !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const clean = e => { if (!e) return ''; const copy = e.cloneNode(true);
    copy.querySelectorAll('select, option, input, textarea, button, [role=option], label:has(input)').forEach(x => x.remove());
    return (copy.innerText || copy.textContent || '').split(/\r?\n/).map(x => x.trim()).find(Boolean)?.slice(0, 200) || ''; };
  const qtext = e => { let c = null; for (const sel of ['.application-question', 'fieldset', '[role=radiogroup]', '[class*="fieldEntry"]', '[class*="question"]', 'li']) { c = e.closest(sel); if (c) break; }
    if (!c) return ''; const l = c.querySelector('legend, .application-label, [class*="label"], label:not(:has(input))'); return clean(l || c); };
  const out = []; let i = 0;
  for (const e of document.querySelectorAll('input, textarea, select')) {
    if (['hidden','submit','button','image','reset','search'].includes(e.type) || e.disabled || !vis(e)) continue;
    e.setAttribute('data-ra', String(i));
    let label = '';
    if (e.id) { const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]'); if (l) label = clean(l); }
    const q = qtext(e);
    if (e.type === 'radio' || e.type === 'checkbox') label = label || clean(e.closest('label')) || e.value || '';
    else if (!label) label = q || e.getAttribute('aria-label') || e.placeholder || e.name || '';
    const req = e.required || e.getAttribute('aria-required') === 'true' || /[*✱]/.test(q) || /[*✱]/.test(label);
    const heading = clean(e.closest('fieldset')?.querySelector('legend') || e.parentElement?.previousElementSibling);
    out.push({i, tag: e.tagName.toLowerCase(), type: e.type, name: e.name, id: e.id, aria_label: e.getAttribute('aria-label') || '', heading,
      value: e.value, label: label.trim().slice(0, 200), question: q, required: req,
      options: e.tagName === 'SELECT' ? [...e.options].map(o => o.text.trim()).filter(Boolean) :
        (e.type === 'radio' || e.type === 'checkbox') ? [label] : []});
    i++;
  }
  return out;
}"""


def generic_plan(fields, prof, bank, company, mfields=None, resolver=None):
    resolver = resolver or resolve
    actions, missing, groups = [], [], {}
    mfields = mfields if mfields is not None else []
    resume_ids, ambiguous_files = resume_file_indices(fields)
    for f in fields:
        if f["type"] in ("radio", "checkbox"):
            groups.setdefault(f["name"] or f"cb{f['i']}", []).append(f)
            continue
        label = f["label"]
        if f["type"] == "file":
            if f['i'] in resume_ids:
                actions.append((f["i"], "file", prof["resume"]))
            elif f["required"]:
                missing.append((strip_label(label), 'ambiguous_file') if ambiguous_files and not is_cover_file(f) else strip_label(label))
            continue
        val = resolver(label, prof, bank, company)
        if f["tag"] == "select":
            opt = pick_option(val, f["options"]) if val else None
            if opt:
                actions.append((f["i"], "select", opt))
            elif f["required"]:
                missing.append(strip_label(label))
        elif text_answer(val):
            actions.append((f["i"], "text", text_answer(val)))
        else:
            if f["required"]:
                missing.append(strip_label(label))
            mfields.append((strip_label(label), f["i"], "textarea" if f["tag"] == "textarea" else "text", f["required"]))
    for g in groups.values():
        question = g[0]["question"] or g[0]["label"]
        req = any(x["required"] for x in g)
        if len(g) == 1 and g[0]["type"] == "checkbox" and not g[0]["question"]:
            question = g[0]["label"]
        val = resolver(question, prof, bank, company)
        opt = pick_option(val, [x["label"] for x in g]) if val else None
        if opt:
            actions.append((next(x["i"] for x in g if x["label"] == opt), "click", opt))
        elif req:
            missing.append(strip_label(question))
    return actions, missing


ASHBY_YESNO_JS = r"""() => {
  const out = []; let i = 0;
  for (const c of document.querySelectorAll('[class*="fieldEntry"], [class*="field-entry"]')) {
    const btns = [...c.querySelectorAll('button')].filter(b => /^(yes|no)$/i.test(b.innerText.trim()));
    if (btns.length !== 2) continue;
    const l = c.querySelector('label, [class*="label"]');
    const q = ((l && l.innerText) || '').split(/\r?\n/)[0].trim();
    btns.forEach(b => b.setAttribute('data-ra-yn', i + ':' + b.innerText.trim().toLowerCase()));
    out.push({i, question: q, required: /[*✱]/.test(q) || !!c.querySelector('[class*="required" i]')});
    i++;
  }
  return out;
}"""


def ashby_extras(page, prof, bank, company):
    """Ashby renders Yes/No questions as button pairs and location as an autocomplete; FIELDS_JS sees neither."""
    results, missing = [], []
    try:
        groups = page.evaluate(ASHBY_YESNO_JS)
    except Exception:
        groups = []
    for g in groups:
        val = resolve(g["question"], prof, bank, company)
        opt = pick_option(val, ["Yes", "No"]) if val else None
        if opt:
            page.locator(f"[data-ra-yn='{g['i']}:{opt.lower()}']").first.click()
            results.append(dict(key=f"yn{g['i']}", kind="click", status="filled", error="", value=opt))
        elif g["required"]:
            missing.append(strip_label(g["question"]))
    loc = page.locator("input[placeholder^='Start typing' i]")
    for k in range(loc.count()):
        box = loc.nth(k)
        if box.input_value().strip():
            continue
        try:
            label = box.evaluate("e => { const c = e.closest('[class*=fieldEntry],[class*=field-entry]'); "
                                 "const l = c && c.querySelector('label, [class*=label]'); return ((l && l.innerText) || '').split(/\\r?\\n/)[0].trim(); }") or ""
        except Exception:
            label = ""
        required = bool(re.search(r"[*\u2731]", label))
        question = strip_label(label)
        if not question or re.search(r"locat|where .{0,30}(work|based|live|reside)|city", question, re.I):
            city = prof.get("city", "")
            state_re = re.compile("|".join(rf"\b{re.escape(s)}\b" for s in [prof.get("state", "")] + HOME_STATES if s) or ".", re.I)
            filled, err = None, ""
            # Small towns are often missing from the location service: fall back to the nearest listed city, then any option.
            for attempt in [city] + [c for c in (prof.get("location_fallbacks") or ["Madison"]) if c != city]:
                try:
                    box.click(); box.fill(""); box.type(attempt, delay=60)
                    opts = page.locator("[role=option]")
                    opts.first.wait_for(timeout=6000)
                    pick = opts.filter(has_text=state_re)
                    (pick.first if pick.count() else opts.first).click()
                    filled = box.input_value() or attempt
                    break
                except Exception as e:
                    err = str(e).splitlines()[0][:150]
            if filled:
                results.append(dict(key=f"loc{k}", kind="text", status="filled", error="", value=filled))
            else:
                results.append(dict(key=f"loc{k}", kind="text", status="failed", error=f"Location field: no option offered for {city} ({err})", value=city))
            continue
        # Any other type-ahead (e.g. "How did you hear about us?"): type each stored alternative, pick a listed option.
        val = resolve(question, prof, bank, company)
        picked = None
        for alt in [a for a in (val or "").split("|") if a and not a.startswith("__")]:
            try:
                box.click(); box.fill(""); box.type(alt[:12], delay=60)
                page.wait_for_timeout(1500)
                opts = [t.strip() for t in page.locator("[role=option]").all_inner_texts() if t.strip()]
                choice = pick_option(alt, opts) if opts else None
                if choice:
                    page.locator("[role=option]").filter(has_text=choice).first.click()
                    picked = choice
                    break
            except Exception:
                continue
        if picked:
            results.append(dict(key=f"ta{k}", kind="text", status="filled", error="", value=picked))
        else:
            try:
                box.fill("")
            except Exception:
                pass
            if required:
                missing.append(question)
    return results, missing


def generic_fill(page, actions, tolerant=False):
    results = []
    for i, kind, val in actions:
        try:
            box = page.locator(f"[data-ra='{i}']").first
            if kind == "file":
                log(f"upload file: {attachment_details(val)}")
                box.set_input_files(val)
                page.wait_for_timeout(2500)
            elif kind == "text":
                box.fill(val)
            elif kind == "select":
                box.select_option(label=val)
            elif kind == "click":
                box.check(force=True)
            page.wait_for_timeout(150)
            if not _read_back(box, kind, val) and kind == "text":
                # Lever's resume parser can overwrite fields a moment after upload: wait, refill once.
                page.wait_for_timeout(2000)
                box.fill(val)
                page.wait_for_timeout(300)
            if not _read_back(box, kind, val):
                raise ValueError("value readback mismatch")
            results.append(dict(key=i, kind=kind, status="filled", error="", value=val))
        except Exception as e:
            results.append(dict(key=i, kind=kind, status="failed", error=str(e).splitlines()[0][:200], value=val))
            if not tolerant:
                raise
    return results


# ---------------------------------------------------------------- submit
def confirmation_text(text):
    # Validation errors can quote submission words without confirming an application.
    if re.search(r'validation|error|could not|not submitted|failed to submit', text, re.I):
        return False
    return any(re.search(p, text, re.I) for p in SUCCESS_PATTERNS)


def confirm_submission(page, baseline_url, baseline_text_hash, baseline_text='', timeout=30000):
    """Require changed path or missing form, new application confirmation text."""
    try:
        page.wait_for_function("([before, patterns]) => { const t=document.body.innerText; return (location.pathname !== before || !document.querySelector('form')) && patterns.some(p => new RegExp(p,'i').test(t)); }",
                               arg=[urlparse(baseline_url).path, SUCCESS_PATTERNS], timeout=timeout)
    except Exception:
        return False
    text = page.inner_text('body')
    return (urlparse(page.url).path != urlparse(baseline_url).path or not page.locator('form').count()) and confirmation_text(text) and not confirmation_text(baseline_text) and hashlib.sha256(text.encode()).hexdigest() != baseline_text_hash


def save_evidence(conn, attempt_id, page=None, kind='screenshot', path=None, note=None, doc_hashes=None):
    content = (page.inner_text('body') if page else note or '')
    if page and not path:
        PROOF.mkdir(exist_ok=True)
        path = PROOF / f'attempt-{attempt_id}.png'
        page.screenshot(path=str(path), full_page=True)
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest() if path else hashlib.sha256(content.encode()).hexdigest()
    stored_path = note if kind == 'note' else str(path) if path else None
    hashes = {'weak': True} if kind == 'note' else doc_hashes or {}
    conn.execute('INSERT INTO evidence(attempt_id,kind,path,sha256,url,page_text_sha256,doc_hashes,ts) VALUES (?,?,?,?,?,?,?,?)',
                 (attempt_id, kind, stored_path, digest, page.url if page else None,
                  hashlib.sha256(content.encode()).hexdigest(), json.dumps(hashes), state.now()))
    conn.commit()


def _visible(locator):
    try:
        return locator.is_visible()
    except Exception:
        return False


def submit_and_confirm(page, confirmation_timeout=30000):
    btn = page.get_by_role("button", name=re.compile(r"submit( your)?( application)?|send application", re.I))
    if not btn.count():
        return "needs_you", "No recognizable submit button"
    before_url = page.url
    before_text = page.inner_text('body')
    before_hash = hashlib.sha256(before_text.encode()).hexdigest()
    clicked_at = dt.datetime.now(dt.timezone.utc)
    btn.last.click()
    sec = page.locator("[id^='security-input']")
    try:
        sec.first.wait_for(state="visible", timeout=8000)
    except Exception:
        pass
    if sec.count():
        import seccode
        company = CURRENT_JOB.get('company', '')
        if not (company and seccode.configured()):
            return "needs_you", "Security code required - IMAP not configured"
        code = seccode.wait_for_code(company, clicked_at, timeout=180)
        if not code:
            return "needs_you", "Security code required - no code email within 3 min"
        n = sec.count()
        if n >= len(code):
            for i, ch in enumerate(code):
                sec.nth(i).fill(ch)
        else:
            sec.first.fill(code)
        page.wait_for_timeout(500)
        before_url, before_text = page.url, page.inner_text('body')
        before_hash = hashlib.sha256(before_text.encode()).hexdigest()
        btn.last.click()
        log(f"security code entered for {company}")
    if confirm_submission(page, before_url, before_hash, before_text, confirmation_timeout):
        return "applied", ""
    challenge = page.locator("iframe[src*='hcaptcha'][title*='challenge' i], iframe[src*='recaptcha/api2/bframe'], iframe[title*='challenge' i]")
    if any(_visible(challenge.nth(i)) for i in range(min(challenge.count(), 5))):
        return "needs_you", "Blocked by CAPTCHA - not submitted"   # e.g. Lever's hCaptcha puzzle over the submit button
    alerts = page.locator("[role=alert], [class*='error' i]")
    # only errors actually shown: Lever ships hidden templates such as 'File exceeds the maximum upload size of 100MB'
    errs = [alerts.nth(i).inner_text().strip() for i in range(min(alerts.count(), 40)) if _visible(alerts.nth(i))]
    errs = [e for e in errs if e][:3]
    if errs:  # validation errors first: an idle invisible-reCAPTCHA frame is not a challenge
        return "needs_you", "Form rejected: " + " | ".join(errs)[:300]
    return "unconfirmed", "Clicked submit, no confirmation seen - check email before re-applying"


def shot(page, job):
    PROOF.mkdir(exist_ok=True)
    p = PROOF / f"{dt.date.today()}_{ckey(job['company'])}_{re.sub(r'[^a-z0-9]+', '-', job['title'].lower())[:50]}.png"
    try:
        page.screenshot(path=str(p), full_page=True)
    except Exception:
        pass
    return p.name


def _new_attempt(conn, job, prof, note=''):
    used = {k: prof.get(k) for _, k in PROFILE_FIELDS if prof.get(k)}
    profile_digest = hashlib.sha256(json.dumps(used, sort_keys=True, default=str).encode()).hexdigest()
    resume_digest = None
    resume_path = Path(prof.get('resume', ''))
    if resume_path.is_file():
        digest = hashlib.sha256()
        with resume_path.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
        resume_digest = digest.hexdigest()
    return state.create_attempt(conn, job['url'], note, profile_digest, resume_digest)


_GH_Q_CACHE = {}


def gh_question_meta(job):
    """Greenhouse API field name -> {label, required}; react-select inputs carry the
    API name only in their id, so DOM lookups by name miss them."""
    key = (job.get('token'), str(job.get('job_id') or ''))
    if key not in _GH_Q_CACHE:
        meta = {}
        try:
            d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{key[0]}/jobs/{key[1]}?questions=true")
            for q in (d.get('questions') or []) + (d.get('location_questions') or []):
                for fld in q.get('fields') or []:
                    if fld.get('name'):
                        meta[fld['name'].removesuffix('[]')] = dict(label=clean_field_label(clean(q.get('label', ''))), required=bool(q.get('required')))
        except Exception:
            pass
        _GH_Q_CACHE[key] = meta
    return _GH_Q_CACHE[key]


def _record_attempt(conn, attempt_id, job, results, missing, fields=(), error='', prof=None, bank=None):
    by_key = {str(f.get('i')): f for f in fields}
    by_key.update({str(f.get('id')): f for f in fields if f.get('id')})
    by_key.update({str(f.get('name')): f for f in fields if f.get('name')})
    problems = []
    for r in results:
        f = by_key.get(str(r['key'])) or by_key.get(str(r['key']).removesuffix('[]'), {})
        base = str(r['key']).removesuffix('[]')
        shown_label = strip_label(f.get('question') or f.get('label') or '')
        if job.get('source') == 'greenhouse' and job.get('token') and re.fullmatch(r'question_\d+', base):
            # The API question text is authoritative; DOM label scraping can attach a neighbour's label.
            meta = gh_question_meta(job).get(base)
            if meta and meta['label']:
                f = dict(f, question=meta['label'], required=meta['required'])
        label = strip_label(f.get('question') or f.get('label') or str(r['key']))
        required = f.get('required', True)
        fill_error = 'field fill failed' if state.SENSITIVE.search(label) and r['status'] == 'failed' else r.get('error', '')
        source = r.get('source')
        if not source:
            if r['kind'] == 'file' and re.search('resume|cv', label, re.I):
                source = 'profile'
            else:
                _, source, _, _ = answer_store.resolve(conn, label, job['company'])
                source = source or 'profile'
        state.record_field(conn, attempt_id, r['key'], label, required, r['kind'], r['status'],
                           r.get('value', ''), fill_error, source)
        if r.get('doc_hash'):
            conn.execute('INSERT INTO evidence(attempt_id,kind,path,sha256,doc_hashes,ts) VALUES (?,?,?,?,?,?)',
                         (attempt_id, 'document', r.get('value'), r['doc_hash'], json.dumps({'cover_letter': r['doc_hash']}), state.now()))
        if r['status'] == 'failed':
            if str(fill_error).startswith('draft_rejected'):
                if not required:
                    conn.execute('INSERT INTO blockers(attempt_id,label,field_key,kind,status,ts) VALUES (?,?,?,?,?,?)',
                                 (attempt_id, label, str(r['key']), 'draft_rejected', 'open', state.now()))
                conn.execute("UPDATE blockers SET kind='draft_rejected' WHERE attempt_id=? AND field_key=? AND status='open'", (attempt_id, str(r['key'])))
            problems.append(f"{label}: {fill_error}")
    for item in missing:
        label, file_blocker = item if isinstance(item, tuple) else (item, '')
        known = next((f for f in fields if strip_label(f.get('question') or f.get('label')) == label), {})
        kind = 'textarea' if known.get('tag') == 'textarea' else known.get('type') or 'text'
        value = resolve(label, prof or {}, bank or [], job['company'])
        status = 'failed' if value and str(kind).startswith('select') else 'unanswered'
        _, _, blocker_kind, answer_id = answer_store.resolve(conn, label, job['company'])
        reason = file_blocker or (f'{blocker_kind} answer id {answer_id}' if blocker_kind else ('required option unavailable' if status == 'failed' else 'no approved answer'))
        key = known.get('i', known.get('name', label))
        state.record_field(conn, attempt_id, key, label, True, kind, status, value, reason, '')
        if file_blocker or blocker_kind:
            conn.execute('UPDATE blockers SET kind=? WHERE attempt_id=? AND field_key=? AND status=\'open\'', (file_blocker or blocker_kind, attempt_id, str(key)))
        problems.append(f"{label}: {reason}")
    if error:
        state.record_field(conn, attempt_id, '__form__', 'Application form', True, 'form', 'failed', error=error)
        problems.append(error)
    target = 'blocked' if problems else 'ready_for_review'
    state.set_state(conn, attempt_id, target)
    note = '; '.join(problems) if problems else f"{len(results)} fields filled and verified; review before submit"
    previous_note = conn.execute('SELECT note FROM attempts WHERE id=?', (attempt_id,)).fetchone()[0]
    if previous_note and previous_note.startswith('resumes attempt'):
        note = previous_note + '; ' + note
    elif previous_note == 'manual':
        note = 'manual; ' + note
    conn.execute('UPDATE attempts SET note=? WHERE id=?', (note, attempt_id))
    conn.commit()
    return target, note


def prepare_lead(job, prof):
    """Account-gated or aggregator postings are never automated. Prepare a grounded
    cover letter and hand the posting to the user as a lead."""
    note = f"LEAD ({job['source']}, apply yourself): {job['apply_url']}"
    # API cost: leads are applied to by hand and most never are, so only strong ones get a letter up front.
    if prof.get('cover_letters', True) and (job.get('fit') or 0) >= prof.get('lead_cover_letter_min_fit', 70):
        try:
            import smart
            letter, path = smart.cover_letter(prof, job)
            body = letter.split('\n\n', 1)[-1].rsplit('\n\n', 1)[0]
            problems = smart.grounding_problems(body, smart.grounded_resume_text(prof), prof.get('never_claim', []), profile=prof, job=job)
            if problems:
                _save_rejected(job, 'Cover letter', letter, problems)
                path.unlink(missing_ok=True)
                note += " | cover letter rejected: " + smart.format_problems(problems)
            else:
                note += f" | cover letter: {path}"
        except Exception as e:
            note += f" | cover letter failed: {str(e)[:80]}"
    return note


def _record_variant(url, variant):
    with db() as conn:
        if 'resume_variant' not in {c[1] for c in conn.execute("PRAGMA table_info(jobs)")}:
            conn.execute("ALTER TABLE jobs ADD COLUMN resume_variant TEXT")
        conn.execute("UPDATE jobs SET resume_variant=? WHERE url=?", (variant, url))
        conn.commit()


def tailored_profile(job, prof):
    """Swap in the track resume for this job (approved variants, reordered per posting)."""
    if not prof.get('resume_variants', True):
        return prof
    try:
        import resumes
        path, variant = resumes.for_job(job)
        job['resume_variant'] = variant
        _record_variant(job['url'], variant)
        return dict(prof, resume=str(path))
    except Exception as e:
        log(f"resume variant fallback ({job.get('company')}): {str(e).splitlines()[0][:120]}")
        return prof


def process(job, prof, bank, submit, ctx, selector_timeout=25000, confirmation_timeout=30000):
    _set_current_job(job)
    prof = tailored_profile(job, prof)
    if job.get('source') == 'icims' and prof.get('icims_auto', False):
        import icims
        return icims.apply_job(ctx.new_page() if not hasattr(ctx, 'goto') else ctx, job, prof, bank, dry=not submit)
    if job.get('source') == 'workable' and prof.get('workable_auto', False):
        import workable
        return workable.apply_job(ctx.new_page() if not hasattr(ctx, 'goto') else ctx, job, prof, bank, dry=not submit)
    if job.get('source') == 'smartrecruiters' and prof.get('smartrecruiters_auto', False):
        import smartrecruiters
        return smartrecruiters.apply_job(ctx.new_page() if not hasattr(ctx, 'goto') else ctx, job, prof, bank, dry=not submit)
    if job.get('source') == 'workday' and prof.get('workday_auto', False):
        import workday
        # run() passes a BrowserContext; give apply_job a page so it returns (status, note)
        return workday.apply_job(ctx.new_page() if not hasattr(ctx, 'goto') else ctx, job, prof, bank, submit)
    if job.get('source') == 'employer' and prof.get('employer_site_auto', True):
        import ats_form   # generic multi-page form filler: start button, fields, next, submit
        return ats_form.apply_job(ctx.new_page() if not hasattr(ctx, 'goto') else ctx, job, prof, bank, dry=not submit)
    if job.get('source') in AGENCY_SOURCES:
        import agencies
        return 'needs_you', f"{agencies.lead_note()} | {prepare_lead(job, prof)}"
    if job.get('source') in LEAD_SOURCES:
        return ('manual' if job.get('source') in ('adzuna', 'remotive', 'himalayas', 'indeed', 'google') else 'needs_you'), prepare_lead(job, prof)
    with db() as conn:
        attempt = _new_attempt(conn, job, prof)
        page = None
        try:
            page = ctx.new_page()
            page.goto(job['apply_url'], wait_until='domcontentloaded', timeout=45000)
            page.wait_for_selector('input, textarea, select', timeout=selector_timeout)
            fields = page.evaluate(FIELDS_JS)
            results, missing, _ = prepare_fields(page, job, prof, bank, fields)
            attempt_state, note = _record_attempt(conn, attempt, job, results, missing, fields, prof=prof, bank=bank)
            if attempt_state == 'blocked':
                return 'needs_you', note
            if not submit:
                return 'ready', note
            if any(r.get('source') in ('draft', 'cover_letter') for r in results) and not (prof.get('permissions', {}).get('submit', {}).get('scope', {}).get('allow_drafts') is True):
                return 'ready', 'Drafts require review before submission'
            allowed, reason = policy.check('submit', {**job, 'submitted_today': prof.get('_submitted_today', 0)}, prof)
            if not allowed:
                state.record_field(conn, attempt, '__permission__', 'Submit permission', True, 'policy', 'failed', error=reason)
                state.set_state(conn, attempt, 'blocked')
                return 'needs_you', reason
            try:
                state.begin_submit(conn, job['url'])
            except (state.DuplicateSubmission, ValueError) as e:
                state.record_field(conn, attempt, '__duplicate__', 'Submission safety', True, 'policy', 'failed', error=str(e))
                state.set_state(conn, attempt, 'blocked')
                return 'needs_you', str(e)
            CURRENT_JOB.clear(); CURRENT_JOB.update(job)
            status, submit_note = submit_and_confirm(page, confirmation_timeout)
            if status == 'applied':
                state.set_state(conn, attempt, 'confirmed', followup_days())
                save_evidence(conn, attempt, page)
            elif status == 'needs_you' and ('CAPTCHA' in submit_note or CLIENT_VALIDATION.search(submit_note)):
                # definitively not submitted (CAPTCHA wall / client-side required-field error): release so a fixed run can retry
                state.set_state(conn, attempt, 'abandoned')
                status = 'captcha' if 'CAPTCHA' in submit_note else 'needs_you'
            else:
                status = 'unconfirmed'
                state.set_state(conn, attempt, 'uncertain')
            conn.execute('UPDATE jobs SET status=? WHERE url=?', (status, job['url']))
            conn.commit()
            return status, (submit_note + ' ' if submit_note else '') + f'[proof: {shot(page, job)}]'
        except Exception as e:
            if re.search(r'HTTP (?:Error )?404\b', str(e), re.I) and conn.execute('SELECT state FROM attempts WHERE id=?', (attempt,)).fetchone()[0] == 'new':
                state.set_state(conn, attempt, 'blocked')
                state.set_state(conn, attempt, 'abandoned')
                return 'closed', 'posting 404'
            error = f"Automation error: {str(e).splitlines()[0][:200]}"
            current = conn.execute('SELECT state FROM attempts WHERE id=?', (attempt,)).fetchone()[0]
            if current == 'new':
                _record_attempt(conn, attempt, job, [], [], error=error)
            elif current == 'submitting':
                state.set_state(conn, attempt, 'uncertain')
                conn.execute("UPDATE jobs SET status='unconfirmed' WHERE url=?", (job['url'],))
                conn.commit()
                return 'unconfirmed', error
            return 'needs_you', error
        finally:
            if page:
                page.close()


COVER_JS = r"""() => { const out = [];
  for (const e of document.querySelectorAll('input[type=file], textarea')) {
    const nid = ((e.name || '') + ' ' + (e.id || '')).toLowerCase();
    if (/resume|\bcv\b/.test(nid)) continue;
    let lab = '';
    if (e.id) { const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]'); if (l) lab = l.innerText; }
    if (!lab) { const c = e.closest('.application-question, fieldset, [class*="fieldEntry"], [class*="field"]'); if (c) lab = (c.innerText || '').slice(0, 150); }
    const ctx = (lab + ' ' + nid).toLowerCase();
    if (/cover.?letter|additional information/.test(ctx)) { e.setAttribute('data-cl', String(out.length)); out.push(e.type === 'file' ? 'file' : 'text'); }
  }
  return out; }"""


def attach_cover(page, prof, job):
    """If the form has a cover-letter slot, write a tailored letter and attach/paste it. Returns note or ''."""
    if not prof.get("cover_letters", True):
        return ""
    slots = page.evaluate(COVER_JS)
    if not slots:
        return ""
    import smart
    text, path = smart.cover_letter(prof, job)
    if "file" in slots:
        page.locator(f"[data-cl='{slots.index('file')}']").first.set_input_files(str(path))
    else:
        page.locator(f"[data-cl='{slots.index('text')}']").first.fill(text)
    return f"COVER LETTER attached ({path.name}) - read it"


DEMO_RE = re.compile(r"gender|transgender|sexual orientation|ethnicit|race|hispanic|veteran|disabilit|\bidentify\b", re.I)


def gh_demographics(page, job):
    """Fill unfilled EEO comboboxes from approved answers (default: decline); tick the demographic-data consent if present."""
    out, done = [], set()
    with db() as conn:
        # Answering one question can re-render the section (e.g. Hispanic/Latino reveals Race), detaching
        # labels collected earlier; re-read the form after every answer until nothing new is left.
        for _ in range(8):
            todo = None
            for lab in page.locator("label[for]").all():
                try:
                    text, fid = (lab.inner_text() or "").strip(), lab.get_attribute("for")
                except Exception:
                    continue
                if fid in done or not DEMO_RE.search(text):
                    continue
                box = page.locator(f"[id='{fid}']")
                if not box.count() or box.first.get_attribute("role") != "combobox" or _rs_shown(box.first):
                    done.add(fid)
                    continue
                todo = (text, fid, box)
                break
            if not todo:
                break
            text, fid, box = todo
            done.add(fid)
            try:
                val = answer_store.resolve(conn, strip_label(text), job["company"])[0] or "__decline__"
                out.append(dict(key=fid, kind="select", status="filled", error="", value=_rs_pick(page, box.first, fid, val), source="demographics"))
                page.wait_for_timeout(400)
            except Exception as e:
                out.append(dict(key=fid or text[:40], kind="select", status="failed", error=str(e)[:200], value="", source="demographics"))
    if any(r["status"] == "filled" for r in out):
        for el in page.locator("input[type=checkbox]").all():
            ctx = el.evaluate("e => (e.closest('div,fieldset,label')?.parentElement?.innerText || '')").lower()
            if "demographic data" in ctx and not el.is_checked():
                el.check(force=True)
                out.append(dict(key="demographic_consent", kind="check", status="filled" if el.is_checked() else "failed", error="", value="checked", source="demographics"))
    return out


def _save_rejected(job, label, text, problems):
    """Keep rejected drafts for review; they are never typed or attached."""
    try:
        import smart
        d = ROOT / "cover_letters" / "rejected"
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9]+", "", job.get("company") or "x")
        key = re.sub(r"[^A-Za-z0-9_-]+", "", str(job.get("job_id") or "")) or hashlib.sha256(str(job.get("url", "")).encode()).hexdigest()[:12]
        tag = hashlib.sha256(label.encode()).hexdigest()[:8]
        (d / f"{safe}_{key}_{tag}.txt").write_text(
            f"JOB: {job.get('company')} - {job.get('title')}\nURL: {job.get('url')}\nFIELD: {label}\n"
            f"REASONS: {smart.format_problems(problems, 20)}\n\n{text}\n")
    except Exception:
        pass


def prepare_fields(page, job, prof, bank, fields=None, manual=False):
    """Plan, fill, and draft through one path for all execution modes."""
    import smart
    fields = fields if fields is not None else page.evaluate(FIELDS_JS)
    mfields = []
    if job['source'] == 'greenhouse' and not manual:
        actions, missing = gh_plan(job['token'], job['job_id'], prof, bank, job['company'], mfields)
        if page.locator("[id='country'][role=combobox]").count() and not any(a[0] == 'country' for a in actions):
            actions.insert(0, ('country', 'select', prof.get('country', 'United States')))  # phone-widget country, required once phone is set
        filler = gh_fill
    else:
        actions, missing = generic_plan(fields, prof, bank, job['company'], mfields)
        filler = generic_fill
    results = [] if manual else filler(page, actions, tolerant=True)
    if job['source'] == 'greenhouse' and not manual:
        results += gh_demographics(page, job)
    if job['source'] == 'ashby' and not manual:
        extra, extra_missing = ashby_extras(page, prof, bank, job['company'])
        results += extra
        missing += extra_missing
    manual_answers = []
    if manual:
        labels = {str(f['i']): strip_label(f.get('question') or f.get('label')) for f in fields}
        manual_answers = [(labels.get(str(key), ''), '(attach ' + Path(value).name + ')' if kind == 'file' else value)
                          for key, kind, value in actions]
        results = [dict(key=key, kind=kind, status='skipped', value=value)
                   for key, kind, value in actions]
    if prof.get('draft_answers', True):
        for label, key, kind, required in mfields:
            if not smart.draftable(label, kind, required):
                continue
            try:
                value = smart.draft(prof, job, label)
                problems = smart.grounding_problems(value, smart.grounded_resume_text(prof), prof.get('never_claim', []), profile=prof, job=job)
                if problems:
                    _save_rejected(job, label, value, problems)
                    raise ValueError('draft_rejected: ' + smart.format_problems(problems))
                if manual:
                    manual_answers.append((label, value))
                else:
                    box = page.locator(f"[id='{key}']" if job['source'] == 'greenhouse' else f"[data-ra='{key}']").first
                    box.fill(value)
                    if not _read_back(box, 'text', value):
                        raise ValueError('value readback mismatch')
                results.append(dict(key=key, kind=kind, status='filled', error='', value=value, source='draft'))
                if label in missing:
                    missing.remove(label)
            except Exception as e:
                results.append(dict(key=key, kind=kind, status='failed', error=str(e)[:300] if 'draft_rejected' in str(e) else 'draft failed', value='', source='draft'))
                if label in missing:
                    missing.remove(label)
    if prof.get('cover_letters', True) and page.evaluate(COVER_JS):
        try:
            letter, path = smart.cover_letter(prof, job)
            body = letter.split('\n\n', 1)[-1].rsplit('\n\n', 1)[0]
            problems = smart.grounding_problems(body, smart.grounded_resume_text(prof), prof.get('never_claim', []), profile=prof, job=job)
            if problems:
                _save_rejected(job, 'Cover letter', letter, problems)
                raise ValueError('draft_rejected: ' + smart.format_problems(problems))
            if manual:
                manual_answers.append(('Cover letter', f'(attach {path.name})\n\n{letter}'))
            else:
                slots = page.evaluate(COVER_JS)
                box = page.locator(f"[data-cl='{slots.index('file' if 'file' in slots else 'text')}']").first
                box.set_input_files(str(path)) if 'file' in slots else box.fill(letter)
            results.append(dict(key='__cover_letter__', kind='file', status='filled', value=str(path), source='cover_letter', doc_hash=hashlib.sha256(path.read_bytes()).hexdigest()))
            missing = [m for m in missing if not re.search(r'cover.?letter', m, re.I)]
        except Exception as e:
            err = str(e)[:300] if 'draft_rejected' in str(e) else f'cover_letter_error: {e}'[:200]
            results.append(dict(key='__cover_letter__', kind='file', status='failed', error=err, value='', source='cover_letter'))
    return results, missing, manual_answers


MANUAL = {}  # url -> list of (label, value) for jobs the user fills in their own Chrome


def stage_manual(job, prof, bank, ctx):
    """Sites that flag automated browsers (Ashby): read the form, prepare every answer, open the job in the user's own Chrome."""
    import subprocess, smart
    page = ctx.new_page()
    try:
        page.goto(job["apply_url"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_selector("input, textarea", timeout=25000)
        page.wait_for_timeout(1500)
        fields = page.evaluate(FIELDS_JS)
        slots = page.evaluate(COVER_JS)
    finally:
        pass
    results, missing, answers = prepare_fields(page, job, prof, bank, fields, manual=True)
    page.close()
    note = []
    MANUAL[job["url"]] = answers
    subprocess.Popen(["google-chrome", "--new-window", job["apply_url"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    note.insert(0, "OPENED IN YOUR OWN CHROME (Ashby flags automated browsers) - paste answers from the checklist")
    if missing:
        note.append("You answer: " + "; ".join(missing))
    return None, "staged", " || ".join(note), results, missing, fields


def stage_job(job, prof, bank, ctx, selector_timeout=25000):
    _set_current_job(job)
    """Fill a visible tab and persist the outcome before handing it to the user."""
    if job.get('source') == 'workday' and prof.get('workday_auto', False):
        import workday
        return workday.apply_job(ctx, job, prof, bank, False)
    with db() as conn:
        if job['source'] in LEAD_SOURCES:
            return None, 'manual' if job['source'] in ('adzuna', 'remotive', 'himalayas', 'indeed', 'google') else 'needs_you', prepare_lead(job, prof)
        manual_job = job['source'] in prof.get('manual_sources', ['ashby'])
        attempt = _new_attempt(conn, job, prof, 'manual' if manual_job else '')
        if manual_job:
            try:
                page, status, note, results, missing, fields = stage_manual(job, prof, bank, ctx)
                outcome, recorded = _record_attempt(conn, attempt, job, results, missing, fields, prof=prof, bank=bank)
                return page, 'needs_you' if outcome == 'blocked' else status, recorded if missing else note
            except Exception as e:
                error = f"Could not prepare: {str(e).splitlines()[0][:120]}"
                _record_attempt(conn, attempt, job, [], [], error=error)
                return None, 'needs_you', error
        page = None
        try:
            page = ctx.new_page()
            page.goto(job['apply_url'], wait_until='domcontentloaded', timeout=45000)
            page.wait_for_selector('input, textarea, select', timeout=selector_timeout)
            fields = page.evaluate(FIELDS_JS)
            results, missing, _ = prepare_fields(page, job, prof, bank, fields)
            outcome, note = _record_attempt(conn, attempt, job, results, missing, fields, prof=prof, bank=bank)
            return page, 'staged' if outcome == 'ready_for_review' else 'needs_you', note
        except Exception as e:
            if re.search(r'HTTP (?:Error )?404\b', str(e), re.I) and conn.execute('SELECT state FROM attempts WHERE id=?', (attempt,)).fetchone()[0] == 'new':
                state.set_state(conn, attempt, 'blocked')
                state.set_state(conn, attempt, 'abandoned')
                return page, 'closed', 'posting 404'
            error = f"Automation error: {str(e).splitlines()[0][:200]}"
            _record_attempt(conn, attempt, job, [], [], error=error)
            return page, 'needs_you', error


def summary_page(staged):
    e = html.escape
    def block(j):
        ans = MANUAL.get(j["url"])
        if not ans:
            return ""
        items = "".join(f"<div class=a><div class=l>{e(l[:140])}</div><textarea readonly rows={min(12, 2 + len(v) // 90)}>{e(v)}</textarea>"
                        f"<button onclick='navigator.clipboard.writeText(this.previousElementSibling.value);this.textContent=\"Copied\"'>Copy</button></div>"
                        for l, v in ans)
        return (f"<tr><td></td><td colspan=7><details open><summary>Answers to paste - {e(j['company'])}</summary>{items}"
                f"<p class=m>After submitting: <code>.venv/bin/python apply.py confirm '{e(j['url'])}' --evidence 'confirmation note or file'</code></p></details></td></tr>")
    rows = "".join(f"<tr><td>{i}</td><td>{e(j['company'])}</td><td>{e(j['title'])}</td><td>{e(j['hq'])}</td><td>{e(j['salary'] or '')}</td><td>{j.get('fit') or ''}</td><td>{e(n)}</td><td>{e(j.get('ghost_why') or '')}</td></tr>" + block(j)
                   for i, (j, n) in enumerate(staged, 2))
    p = PROOF / f"{dt.date.today()}_tonight.html"
    PROOF.mkdir(exist_ok=True)
    p.write_text("<!doctype html><meta charset='utf-8'><title>Tonight's applications</title><style>body{font:15px system-ui;background:#111418;color:#d8dde3;"
                 "max-width:1200px;margin:2rem auto;padding:1rem}table{border-collapse:collapse;width:100%}td,th{padding:.55rem;border-bottom:1px solid #2a3038;"
                 "text-align:left;vertical-align:top}textarea{width:100%;background:#1a2027;color:#d8dde3;border:1px solid #2a3038;font:14px system-ui}"
                 "button{background:#2a3a55;color:#d8dde3;border:0;padding:.3rem .8rem;margin:.2rem 0 .8rem;cursor:pointer}.l{color:#9fb3c8;margin-top:.5rem}"
                 ".m{color:#9fb3c8}code{color:#7cb4ff}summary{cursor:pointer;color:#7cb4ff}</style><h1>" + str(len(staged)) + " applications ready</h1><p>Each tab after this one is filled in. Review, answer anything "
                 "listed below, solve any CAPTCHA, click Submit. Confirmed submissions are logged automatically. Close Chrome when done.</p><table><tr><th>Tab</th>"
                 "<th>Company</th><th>Role</th><th>HQ</th><th>Salary</th><th>Fit</th><th>To do</th><th>Signals</th></tr>" + rows + "</table>")
    return p


def company_sigs(conn):
    try:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(company_signals)")]
        return {r[0]: dict(zip(cols, r)) for r in conn.execute("SELECT * FROM company_signals")}
    except Exception:
        return {}


def teach(conn, url, submitted):
    try:
        import plan
        conn.row_factory = sqlite3.Row
        j = conn.execute("SELECT * FROM jobs WHERE url=? OR apply_url=?", (url, url)).fetchone()
        if j and j["learn_label"] is None:
            j = dict(j)
            plan.learn(j, company_sigs(conn).get(j["company"]), submitted)
            conn.execute("UPDATE jobs SET learn_label=? WHERE url=?", (1 if submitted else 0, j["url"]))
            conn.commit()
    except Exception as e:
        log(f"learn failed: {e}")
    finally:
        conn.row_factory = None


def _tab_closed(conn, url):
    conn.execute("UPDATE attempts SET note='tab closed, intent unknown', updated=? WHERE id=(SELECT id FROM attempts WHERE job_url=? ORDER BY id DESC LIMIT 1)", (state.now(), url))
    conn.commit()
    log(f"tab closed, intent unknown {url}")


def confirm_human(conn, job, page, baseline):
    if not confirm_submission(page, *baseline, timeout=100):
        return False
    attempt_id = state.begin_submit(conn, job['url'])
    state.set_state(conn, attempt_id, 'confirmed', followup_days())
    save_evidence(conn, attempt_id, page, kind='human')
    conn.execute("UPDATE jobs SET status='applied', applied_at=date('now','localtime'), notes='Submitted by you (confirmation seen)', updated=CURRENT_TIMESTAMP WHERE url=?", (job['url'],))
    conn.commit()
    log_application(dict(job), 'applied', 'Submitted by you (confirmation seen)')
    return True


def run_staged(prof, bank, conn, candidates):
    from playwright.sync_api import sync_playwright
    cap = prof.get("stage_cap", 12)
    with sync_playwright() as pw:
        kw = dict(user_data_dir=str(ROOT / ".chrome-profile"), headless=False, viewport=None, args=["--start-maximized"])
        try:
            ctx = pw.chromium.launch_persistent_context(channel="chrome", **kw)
        except Exception:
            ctx = pw.chromium.launch_persistent_context(**kw)
        first = ctx.pages[0] if ctx.pages else ctx.new_page()
        staged, open_pages, baselines = [], {}, {}
        for job in candidates:
            if len(staged) >= cap:
                break
            page, status, note = stage_job(job, prof, bank, ctx)
            conn.execute("UPDATE jobs SET status=?, notes=?, updated=CURRENT_TIMESTAMP WHERE url=?", (status, note, job["url"]))
            conn.commit()
            log(f"{status:12} {job['company']} - {job['title']} {note}")
            if status in ("applied", "unconfirmed"):
                log_application(job, status, note)
            if status == "staged":
                staged.append((job, note))
                if page:
                    open_pages[job["url"]] = page
                    baseline_text = page.inner_text('body')
                    baselines[job['url']] = (page.url, hashlib.sha256(baseline_text.encode()).hexdigest(), baseline_text)
            elif page:
                page.close()
        export()
        if not staged:
            ctx.close()
            return 0
        first.goto(summary_page(staged).as_uri())
        first.bring_to_front()
        log(f"staged {len(staged)} applications in Chrome - waiting for you")
        deadline = time.time() + prof.get("stage_hours", 20) * 3600
        done = set()
        while time.time() < deadline and any(not p.is_closed() for p in list(open_pages.values()) + [first]):
            for url, p in open_pages.items():
                if url in done:
                    continue
                if p.is_closed():
                    done.add(url)
                    _tab_closed(conn, url)
                    continue
                job = next(j for j, _ in staged if j['url'] == url)
                try:
                    ok = confirm_human(conn, job, p, baselines[url])
                except Exception:
                    ok = False
                if ok:
                    done.add(url)
                    log(f"applied      {job['company']} - {job['title']} (confirmed)")
                    teach(conn, url, True)
                    export()
            time.sleep(5)
        for url in open_pages:
            if url not in done:
                _tab_closed(conn, url)
        try:
            ctx.close()
        except Exception:
            pass
        export()
        return conn.execute("SELECT count(*) FROM jobs WHERE status='applied' AND applied_at=date('now','localtime')").fetchone()[0]


def cap_key(company):
    """Company identity for caps: ignores case, punctuation and legal suffixes (Inc, LLC, GmbH...)."""
    return state.normalize_company(company or "").replace(" ", "")


def company_submissions(conn, window_days=30):
    """Submitted (applied or unconfirmed) applications per company key over the last window_days."""
    since = (dt.date.today() - dt.timedelta(days=window_days)).isoformat()
    counts = {}
    for (company,) in conn.execute("SELECT company FROM jobs WHERE status IN ('applied','unconfirmed','interview','rejected','offer') "
                                   "AND applied_at != '' AND applied_at >= ?", (since,)):
        key = cap_key(company)
        counts[key] = counts.get(key, 0) + 1
    return counts


def company_cap_reason(k, recent, tonight, prof):
    """Why this company must wait, or '' if a submission is allowed."""
    per_night = prof.get("company_per_night", 1)
    per_window = 15 if k == cap_key('Microsoft') else prof.get("company_per_window", 2)
    window = 30 if k == cap_key('Microsoft') else prof.get("company_window_days", 30)
    if tonight.get(k, 0) >= per_night:
        return f"company cap: {per_night} per night"
    if recent.get(k, 0) >= per_window:
        return f"company cap: {per_window} per {window} days"
    return ""


def learned_cap_reason(conn, company, cache={}):
    """Limits employers announced themselves (mail_sort records 'up to three roles within 60 days')."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_limits'").fetchone():
        return ""
    k = cap_key(company)
    for name, max_apps, days in conn.execute("SELECT company, max_apps, window_days FROM company_limits"):
        if cap_key(name) != k:
            continue
        since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        sent = sum(1 for (c,) in conn.execute("SELECT company FROM jobs WHERE status IN ('applied','unconfirmed','interview','rejected','offer') "
                                               "AND applied_at >= ?", (since,)) if cap_key(c) == k)
        if sent >= max_apps:
            return f"employer limit: {max_apps} applications per {days} days"
    return ""


ANSWER_BLOCKERS = ('unapproved_answer', 'stale_answer', 'text', 'select', 'checkbox')


def reconcile_blockers(conn, prof):
    """Re-check open answer blockers against the current answer bank and fill rules.
    A blocker whose question now resolves is closed so its job re-enters the queue;
    rejected drafts are regenerated and checked again. Legal waivers never resolve automatically."""
    prof = prof or {}
    marks = ','.join('?' * len(ANSWER_BLOCKERS))
    rows = conn.execute(f"""SELECT b.id, b.label, coalesce(j.company, '') FROM blockers b JOIN attempts a ON a.id=b.attempt_id
                            LEFT JOIN jobs j ON j.url=a.job_url WHERE b.status='open' AND b.kind IN ({marks})""", ANSWER_BLOCKERS).fetchall()
    closed = 0
    for ident, label, company in rows:
        if not label or WAIVER.search(label):
            continue
        if resolve(label, prof, [], company):
            conn.execute("UPDATE blockers SET status='resolved', resolved_by=NULL WHERE id=?", (ident,))
            closed += 1
    # Old draft text is not stored in the database. Regenerate it under the current
    # grounding rules; a fabricated claim creates a fresh blocker on the new attempt.
    closed += conn.execute("UPDATE blockers SET status='resolved', resolved_by=NULL WHERE status='open' AND kind='draft_rejected'").rowcount
    conn.commit()
    return closed


def application_candidates(conn, min_fit):
    """Retry jobs once their open blockers are gone, including jobs without an attempt."""
    conn.create_function('is_remote_listing', 1, lambda location: int(remote_listing(location)))
    profile = min_fit if isinstance(min_fit, dict) else {'min_fit': min_fit}
    cursor = conn.execute("""SELECT * FROM jobs WHERE source NOT IN ('eightfold','microsoft') AND
        (status IN ('new','ready') OR (status='needs_you' AND NOT EXISTS
        (SELECT 1 FROM blockers b JOIN attempts a ON a.id=b.attempt_id
         WHERE a.job_url=jobs.url AND b.status='open')))
        ORDER BY CASE WHEN source IN ('greenhouse','lever','ashby','workday','workable','smartrecruiters','icims') THEN 0 ELSE 1 END,
                 coalesce(fit,0) + CASE WHEN coalesce(posted_at,'') >= date('now','-2 day') THEN 5
                                        WHEN coalesce(posted_at,'') >= date('now','-7 day') THEN 2
                                        WHEN coalesce(posted_at,'') != '' AND posted_at < date('now','-30 day') THEN -5 ELSE 0 END DESC,
                 CASE WHEN is_remote_listing(location) THEN 0 ELSE 1 END,   -- remote only breaks ties
                 score DESC, found_at DESC""")   # best skills match first (fresh postings nudged up)
    names = [col[0] for col in cursor.description]
    rows = cursor.fetchall()
    return [row for row in rows if eligible_job(dict(zip(names, row)), profile)]


def close_404_postings(conn):
    """Retire earlier form failures caused by a missing posting."""
    changed = conn.execute("""UPDATE jobs SET status='closed', notes='posting 404', updated=CURRENT_TIMESTAMP
        WHERE status='needs_you' AND EXISTS (
            SELECT 1 FROM attempts a JOIN field_results f ON f.attempt_id=a.id
            WHERE a.job_url=jobs.url AND f.field_key='__form__'
              AND f.error LIKE 'Automation error: HTTP Error 404%')""").rowcount
    conn.commit()
    return changed


def retry_form_timeouts(conn):
    """One-time retry of old selector failures after adapter fixes."""
    changed = conn.execute("""UPDATE jobs SET status='new', notes='', updated=CURRENT_TIMESTAMP
        WHERE status='needs_you' AND source IN ('smartrecruiters','ashby')
          AND notes LIKE '%Page.wait_for_selector: Timeout 25000ms exceeded%'
          AND notes NOT LIKE '%stale: posting removed%'""").rowcount
    conn.commit()
    return changed


def verify_sweep():
    """Finish Workday email verifications whose links are still valid."""
    import workday
    try:
        from playwright.sync_api import sync_playwright
        with db() as conn, sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(user_agent=UA["User-Agent"])
                n = workday.sweep_verifications(conn, load_keys(), ctx.new_page, log)
            finally:
                browser.close()
        if n:
            log(f"verify sweep: {n} Workday account(s) verified")
    except Exception as exc:
        log(f"verify sweep skipped: {str(exc).splitlines()[0][:120]}")


def run(dry=False):
    scan()
    if not dry:
        verify_sweep()
    prof = prepare_profile(json.loads(CONFIG.read_text()))
    if not (prof.get("email") and prof.get("phone") and Path(prof["resume"]).exists()):
        raise SystemExit("profile.json needs email, phone and a valid resume path")
    bank = []
    exempt = {ckey(c) for c in prof.get("never_twice_exceptions", [])}
    submit = prof.get("auto_submit", False) and not dry
    cap, q_cap = prof.get("daily_cap", 8), prof.get("needs_you_cap", 10)
    sent = queued = confirmed = 0
    if prof.get("mode", "stage") in ("stage", "prepare") and not dry:
        with db() as conn:
            conn.row_factory = sqlite3.Row
            import plan
            sigs = company_sigs(conn)
            cands = [dict(r) for r in conn.execute("SELECT * FROM jobs WHERE source NOT IN ('eightfold','microsoft') AND status IN ('new','ready','needs_you','staged') "
                                                   "AND source NOT IN ('roberthalf','michaelpage','adecco') AND coalesce(ghost,0) < ? ORDER BY CASE WHEN source IN "
                                                   "('greenhouse','lever','ashby','workday','workable','smartrecruiters','icims') THEN 0 ELSE 1 END, fit DESC",
                                                   (prof.get("max_ghost", 70),)).fetchall() if eligible_job(r, prof)]
            for j in cands:
                j["value"] = plan.value(j, sigs.get(j["company"]), prof)
                conn.execute("UPDATE jobs SET value=? WHERE url=?", (j["value"], j["url"]))
            conn.commit()
            chosen = plan.pick(cands, prof, sigs)
            log(f"plan: {len(chosen)} jobs chosen from {len(cands)} for a {prof.get('nightly_minutes', 60)}-minute session")
            if prof.get('mode') == 'prepare':
                from playwright.sync_api import sync_playwright
                with sync_playwright() as pw:
                    visible = bool(os.environ.get('RA_VISIBLE'))
                    browser = pw.chromium.launch(headless=not visible, slow_mo=250 if visible else 0)
                    try:
                        for job in chosen[:prof.get('stage_cap', 12)]:
                            status, note = process(job, prof, bank, False, browser)
                            conn.execute('UPDATE jobs SET status=?,notes=? WHERE url=?', (status, note, job['url']))
                            conn.commit()
                    finally:
                        browser.close()
                n = 0
            else:
                n = run_staged(prof, bank, conn, chosen)
        log(f"run done: {n} confirmed submissions")
        return
    from playwright.sync_api import sync_playwright
    with db() as conn:
        retired = close_404_postings(conn)
        reopened = reconcile_blockers(conn, prof)
        retried = retry_form_timeouts(conn)
    if retired:
        log(f"closed: {retired} missing postings (HTTP 404)")
    if reopened:
        log(f"reconcile: {reopened} answer blockers now resolve; their jobs re-enter the queue")
    if retried:
        log(f"retry: {retried} application form timeouts re-enter the queue")
    with db() as conn, sync_playwright() as pw:
        conn.row_factory = sqlite3.Row
        sent = conn.execute("SELECT count(*) FROM jobs WHERE status IN ('applied','unconfirmed') AND applied_at=date('now','localtime')").fetchone()[0]
        visible = bool(os.environ.get('RA_VISIBLE'))
        browser = pw.chromium.launch(headless=not visible, slow_mo=250 if visible else 0)
        ctx = browser.new_context(user_agent=UA["User-Agent"], viewport={"width": 1280, "height": 1600})
        claimed = {ckey(r["company"]): r["url"] for r in conn.execute(
            f"SELECT company,url FROM jobs WHERE status IN ({','.join('?'*len(CLAIMED))})", CLAIMED)}
        recent = company_submissions(conn, prof.get("company_window_days", 30))
        recent[cap_key('Microsoft')] = company_submissions(conn, 30).get(cap_key('Microsoft'), 0)
        tonight = company_submissions(conn, 0)  # already submitted today counts toward the per-night cap
        deferred = set()
        candidates = [dict(job) for job in application_candidates(conn, prof)]
        import agencies
        agency_jobs = [j for j in candidates if j["source"] in AGENCY_SOURCES and not overlevel_title(j["title"]) and role_ok(j["title"])]
        for job in agencies.daily_picks(agency_jobs, conn, prof.get("agency_daily_cap", 2)):
            status, note = process(job, prof, bank, submit, ctx)
            conn.execute("UPDATE jobs SET status=?, notes=?, updated=CURRENT_TIMESTAMP WHERE url=?", (status, note, job["url"]))
            conn.commit()
            log(f"{status:12} {job['company']} - {job['title']} (agency pick, fit {round(job.get('fit') or 0)})")
        for job in candidates:
            if job["source"] in AGENCY_SOURCES:
                continue  # agencies are capped per day above, not per company
            k = ckey(job["company"])
            if overlevel_title(job["title"]):
                conn.execute("UPDATE jobs SET status='low_fit', notes='overlevel', updated=CURRENT_TIMESTAMP WHERE url=?", (job["url"],))
                conn.commit()
                continue
            if not role_ok(job["title"]):
                conn.execute("UPDATE jobs SET status='skip', notes='role filter: product management, not program/ops', updated=CURRENT_TIMESTAMP WHERE url=?", (job["url"],))
                conn.commit()
                log(f"{'skip':12} {job['company']} - {job['title']} role filter: product management, not program/ops")
                continue
            if prof.get("one_per_company", False) and k not in exempt and k != ckey('Microsoft') and claimed.get(k, job["url"]) != job["url"]:
                conn.execute("UPDATE jobs SET status='skipped_company', updated=CURRENT_TIMESTAMP WHERE url=?", (job["url"],))
                continue
            ck = cap_key(job["company"])
            capped = company_cap_reason(ck, recent, tonight, prof) or learned_cap_reason(conn, job["company"])
            if capped:
                if ck not in deferred:
                    log(f"{'deferred':12} {job['company']} - {job['title']} {capped}")
                deferred.add(ck)
                continue
            if submit and sent >= cap:
                break
            if queued >= q_cap:
                break
            prof['_submitted_today'] = sent
            status, note = process(job, prof, bank, submit, ctx)
            applied_at = dt.date.today().isoformat() if status in ("applied", "unconfirmed") else ""
            conn.execute("UPDATE jobs SET status=?, notes=?, applied_at=?, updated=CURRENT_TIMESTAMP WHERE url=?",
                         (status, note, applied_at, job["url"]))
            conn.commit()
            log(f"{status:12} {job['company']} - {job['title']} {note}")
            if status in ("applied", "unconfirmed"):
                log_application(job, status, note)
            if status in CLAIMED:
                claimed[k] = job["url"]
            if status in ("applied", "unconfirmed"):
                sent += 1
                confirmed += status == "applied"
                tonight[ck] = tonight.get(ck, 0) + 1
                recent[ck] = recent.get(ck, 0) + 1
                time.sleep(random.uniform(30, 90))
            elif status in ("needs_you", "ready"):
                queued += 1
        browser.close()
    export()
    log(f"run done: {confirmed} confirmed this run, {sent} submitted today (incl. unconfirmed), {queued} queued{' (dry run)' if dry else ''}")


# ---------------------------------------------------------------- outputs
def export():
    with db() as conn:
        rows = conn.execute(f"SELECT * FROM jobs WHERE status IN ({','.join('?'*len(STATUS_ORDER))})", STATUS_ORDER).fetchall()
        cols = [c[1] for c in conn.execute("PRAGMA table_info(jobs)")]
    recs = []
    for r in rows:
        r = dict(zip(cols, r))
        date = r["applied_at"] or (r["updated"] or "")[:10]
        follow = (dt.date.fromisoformat(r["applied_at"]) + dt.timedelta(days=10)).isoformat() if r["applied_at"] else ""
        recs.append({"Date": date, "Status": STATUS_LABEL.get(r["status"], r["status"]), "Company": r["company"], "Job Title": r["title"],
                     "HQ": r["hq"], "Location": r["location"], "Salary (posted)": r["salary"], "Fit Score": r.get("fit") or r["score"],
                     "Follow Up By": follow, "Ghost Risk": r.get("ghost") or 0, "Signals": r.get("ghost_why") or "", "Source": r["source"], "Job URL": r["url"], "Notes": r["notes"], "_o": STATUS_ORDER.index(r["status"])})
    recs.sort(key=lambda x: (x["Company"].lower()))
    recs.sort(key=lambda x: x["_o"])
    recs.sort(key=lambda x: x["Date"], reverse=True)
    CSV_OUT = out_dir() / "applications.csv"
    with CSV_OUT.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=[k for k in (recs[0] if recs else {"Date": 0, "Status": 0, "Company": 0, "Job Title": 0}) if k != "_o"],
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(recs)
    log(f"export: {len(recs)} rows -> {CSV_OUT.name}")


def report():
    with db() as conn:
        rows = conn.execute("SELECT company,title,location,url,score,status,hq,salary FROM jobs WHERE status NOT IN ('skip','closed') ORDER BY score DESC LIMIT 200").fetchall()
    e = html.escape
    t = "".join(f"<tr><td>{e(c)}</td><td><a href='{e(u, quote=True)}'>{e(ti)}</a></td><td>{e(h)}</td><td>{e(l)}</td><td>{e(sa)}</td><td>{s}</td><td>{e(st)}</td></tr>"
                for c, ti, l, u, s, st, h, sa in rows)
    (ROOT / "jobs.html").write_text("<!doctype html><meta charset='utf-8'><title>Remote Job Shortlist</title><style>body{font:15px system-ui;background:#111418;color:#d8dde3;"
        "max-width:1300px;margin:2rem auto;padding:1rem}a{color:#7cb4ff}table{border-collapse:collapse;width:100%}td,th{padding:.55rem;border-bottom:1px solid #2a3038;"
        "text-align:left}tr:hover{background:#1a2027}</style><h1>Remote job shortlist</h1><table><tr><th>Company</th><th>Role</th><th>HQ</th><th>Location</th>"
        "<th>Salary</th><th>Score</th><th>Status</th></tr>" + t + "</table>")
    log(f"report: jobs.html ({len(rows)} jobs)")


def mark(url, status):
    if status == 'applied':
        raise SystemExit('Use confirm URL --evidence FILE_OR_NOTE to record a manual submission')
    with db() as conn:
        n = conn.execute("UPDATE jobs SET status=?, applied_at=CASE WHEN ?='applied' AND applied_at='' THEN date('now','localtime') ELSE applied_at END, "
                         "updated=CURRENT_TIMESTAMP WHERE url=? OR apply_url=?", (status, status, url, url)).rowcount
    print("Updated" if n else "URL not found")
    if n and status in ("applied", "skip"):
        with db() as conn:
            teach(conn, url, status == "applied")
    export()


def manual(url):
    from playwright.sync_api import sync_playwright
    prof = prepare_profile(json.loads(CONFIG.read_text()))
    bank = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=False)
        page = b.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        actions, missing = generic_plan(page.evaluate(FIELDS_JS), prof, bank, "")
        generic_fill(page, actions)
        print("Still needed:", "; ".join(missing) or "none")
        input("Finish and submit in the browser, then press Enter (record confirmation with: python apply.py confirm URL --evidence NOTE)...")
        b.close()


def blockers_cmd():
    with db() as conn:
        for _, url, label, _, kind in state.open_blockers(conn):
            row = conn.execute('SELECT company,title FROM jobs WHERE url=?', (url,)).fetchone()
            print(f"{row[0] if row else url} | {row[1] if row else ''} | {label} | {kind}")


def answers_cmd(action, ids=(), category=None, confirm=False, review_days=None):
    with db() as conn:
        if action == 'import':
            print(f'Imported {answer_store.import_bank(conn, ANSWERS)} pending answers')
        elif action == 'reclassify':
            ids_pending = answer_store.reclassify(conn)
            print('Reclassified answers; approval reset to pending for IDs: ' + (', '.join(map(str, ids_pending)) or 'none'))
        elif action == 'review':
            for ident, cat, pattern, answer, source, status, review_by in conn.execute(
                    'SELECT id,category,match_regex,answer,source,status,review_by FROM answer_store ORDER BY id'):
                shown = (cat + ' ' + answer) if cat in ('eeo', 'attestation') and answer in ('__decline__', '__agree__') else ('[masked]' if cat in ('eeo', 'attestation') else answer)
                print(f'{ident} | {cat} | {pattern} | {shown} | {source} | {status} | {review_by or ""}')
        elif action == 'approve':
            count = answer_store.approve(conn, ids, category, confirm, review_days)
            approved = list(conn.execute("SELECT id,match_regex FROM answer_store WHERE status='approved'"))
            for blocker_id, label in conn.execute("SELECT id,label FROM blockers WHERE status='open' AND kind IN ('unapproved_answer','stale_answer')"):
                match = next((ident for ident, pattern in approved if re.search(pattern, label, re.I)), None)
                if match:
                    conn.execute("UPDATE blockers SET status='resolved',resolved_by=? WHERE id=?", (match, blocker_id))
            conn.commit()
            print(f'Approved {count} answers')
        elif action == 'reject':
            for ident in ids:
                conn.execute("UPDATE answer_store SET approved=0,status='rejected' WHERE id=?", (ident,))
            conn.commit()
            print(f'Rejected {len(ids)} answers')
        elif action == 'reopen':
            print(f'Reopened {answer_wizard.reopen(conn, ids)} answers')
        elif action == 'wizard':
            if answer_wizard.run(conn, category, ROOT / 'reports'):
                raise SystemExit(1)


def status_cmd():
    with db() as conn:
        if conn.execute("SELECT 1 FROM answer_store WHERE status='pending' LIMIT 1").fetchone():
            print('Next: .venv/bin/python apply.py answers wizard')
        print('company | title | state | filled/failed/unanswered | open blockers | evidence')
        for row in conn.execute('''SELECT a.id,j.company,j.title,a.state FROM attempts a LEFT JOIN jobs j ON j.url=a.job_url ORDER BY a.id'''):
            ident, company, title, state_name = row
            counts = dict(conn.execute('SELECT status,count(*) FROM field_results WHERE attempt_id=? GROUP BY status', (ident,)).fetchall())
            blockers = ', '.join(f'{label} ({kind})' for label, kind in conn.execute("SELECT label,kind FROM blockers WHERE attempt_id=? AND status='open'", (ident,)))
            evidence = 'y' if conn.execute('SELECT 1 FROM evidence WHERE attempt_id=?', (ident,)).fetchone() else 'n'
            print(f'{company or ""} | {title or ""} | {state_name} | {counts.get("filled",0)}/{counts.get("failed",0)}/{counts.get("unanswered",0)} | {blockers} | {evidence}')


def response_cmd(url, kind, summary='', file_path=None):
    with db() as conn:
        canonical = followup.record_response(conn, url, kind, summary, file_path)
    if kind in followup.STATUS:
        mark(canonical, followup.STATUS[kind])
    print('Response recorded')


def followups_cmd():
    with db() as conn:
        for _, company, title, url, due, status in followup.due_followups(conn):
            print(f'{due} | {company or ""} | {title or ""} | {status} | {url}')


def followup_draft_cmd(url):
    profile = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    with db() as conn:
        path = followup.draft(conn, url, ROOT / 'followups', profile)
    print(path)


def recruiter_email_cmd(url):
    import smart
    profile = json.loads(CONFIG.read_text())
    profile['_db_path'] = str(DB)
    with db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT rowid AS draft_id, * FROM jobs WHERE url=? OR apply_url=?', (url, url)).fetchone()
    if not row:
        raise SystemExit('Job not in database')
    job = dict(row)
    resume = smart.grounded_resume_text(profile)
    draft = smart.recruiter_email(profile, job)
    if not draft or not smart.check_grounding(draft, resume, profile.get('never_claim', []), profile=profile, job=job):
        raise SystemExit('Recruiter email failed grounding check; no draft written')
    company = re.sub(r'[^A-Za-z0-9]+', '_', job['company']).strip('_') or 'company'
    ident = re.sub(r'[^A-Za-z0-9_-]+', '', str(job.get('job_id') or job['draft_id']))
    path = ROOT / 'followups' / f'recruiter_{company}_{ident}.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'DRAFT - not sent\n\n{draft}\n', encoding='utf-8')
    print(path)


def page_cmd():
    with db() as conn:
        path = followup.write_page(conn, ROOT / 'reports' / 'status.html')
    print(path)


def income_cmd(x):
    if x.income_action == 'inventory':
        if x.file:
            income.save_inventory(ROOT, json.loads(Path(x.file).read_text()))
        else:
            income.inventory(ROOT)
        print(ROOT / 'income' / 'inventory.json')
        return
    if x.income_action == 'offers':
        for score, name, why in income.offers(ROOT): print(f'{score} | {name} | {why}')
        return
    if x.income_action == 'floors':
        floors = income.load_inventory(ROOT).get('salary_floors', {})
        for key in income.SALARY_FLOOR_KEYS:
            value = floors.get(key)
            print(f'{key}: {value if value is not None else "unset"}')
        return
    if x.income_action == 'score-job':
        result = income.score_job(ROOT, x.minimum, x.maximum, x.unit, x.employment_type, x.hours)
        print(f'{result["label"]} | score {result["score"]} | floor {result["floor"] if result["floor"] is not None else "unset"} | annual range {result["annual_min"] if result["annual_min"] is not None else "unknown"} to {result["annual_max"] if result["annual_max"] is not None else "unknown"}')
        if result['note']: print(result['note'])
        return
    with db() as conn:
        action = x.income_action
        if action == 'new': print(income.new(conn, x.name, x.hypothesis, x.kill, x.min_days, x.budget_cap_cents))
        elif action == 'source': print(income.source(conn, ROOT, x.exp, x.url))
        elif action == 'claim':
            kind = 'fact' if x.fact is not None else 'hypothesis' if x.hypothesis is not None else 'estimate'
            body = x.fact if kind == 'fact' else x.hypothesis if kind == 'hypothesis' else x.estimate
            print(income.claim(conn, x.exp, body, kind, x.source, x.quote, x.test, x.kill, x.basis))
        elif action == 'claims':
            for row in income.claims(conn, x.exp): print(' | '.join(str(cell or '') for cell in row))
        elif action == 'prepare':
            profile = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
            for path in income.prepare(conn, ROOT, x.exp, profile): print(path)
        elif action == 'log': income.log_event(conn, x.exp, x.kind, x.value, x.count, x.note)
        elif action == 'report':
            evidence, verdict, flags = income.report(conn, ROOT, x.exp)
            print(json.dumps(evidence, indent=2))
            print(verdict)
            for flag in flags: print(flag)
        elif action == 'publish':
            profile = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
            ok, message = income.publish(conn, x.exp, x.platform, x.url, profile)
            if not ok:
                print('Manual checklist: review the draft, verify claims, publish yourself, then grant scoped publish recording permission.')
                raise SystemExit(message)
            print(message)
        elif action == 'status':
            print('id | name | status | over budget')
            for row in income.status(conn): print(' | '.join(map(str, row)))


def confirm_cmd(url, evidence):
    with db() as conn:
        row = conn.execute("SELECT id,state FROM attempts WHERE job_url=? ORDER BY id DESC LIMIT 1", (url,)).fetchone()
        if not row or row[1] != 'ready_for_review':
            raise SystemExit('No ready attempt for URL')
        path = Path(evidence)
        if not path.is_file() and not evidence.strip():
            raise SystemExit('Evidence file or note required')
        state.begin_submit(conn, url)
        if path.is_file():
            save_evidence(conn, row[0], kind='manual', path=path)
        else:
            save_evidence(conn, row[0], kind='note', note=evidence)
        state.set_state(conn, row[0], 'confirmed', followup_days())
        conn.execute("UPDATE jobs SET status='applied',applied_at=date('now','localtime') WHERE url=?", (url,))
        conn.commit()


def reconcile_cmd(attempt_id, outcome, evidence):
    with db() as conn:
        row = conn.execute('SELECT job_url FROM attempts WHERE id=?', (attempt_id,)).fetchone()
        if not row:
            raise SystemExit('Unknown attempt')
        if outcome == 'confirmed':
            if not evidence:
                raise SystemExit('Evidence required to confirm')
            path = Path(evidence)
            save_evidence(conn, attempt_id, kind='manual' if path.is_file() else 'note', path=path if path.is_file() else None,
                          note=None if path.is_file() else evidence)
        state.reconcile(conn, attempt_id, outcome == 'confirmed', followup_days())
        conn.execute('UPDATE jobs SET status=? WHERE url=?', ('applied' if outcome == 'confirmed' else 'needs_you', row[0]))
        conn.commit()


def review_cmd(ctx=None):
    prof = prepare_profile(json.loads(CONFIG.read_text()))
    with db() as conn:
        conn.row_factory = sqlite3.Row
        candidates = [dict(r) for r in conn.execute("SELECT j.* FROM jobs j WHERE EXISTS (SELECT 1 FROM attempts a WHERE a.job_url=j.url AND a.state='ready_for_review')")]
        if ctx is None:
            run_staged(prof, [], conn, candidates)
        else:
            for job in candidates:
                page, status, note = stage_job(job, prof, [], ctx)
                if page:
                    page.close()


def answer_cmd(url, label_part, value):
    with db() as conn:
        matches = []
        for ident, job_url, label, key, kind in state.open_blockers(conn, url):
            if label_part.lower() in label.lower():
                matches.append((ident, label))
        if not matches:
            raise SystemExit('No matching open blocker for that URL and label')
        for ident, label in matches:
            cat = answer_store.category(label)
            expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=90 if cat == 'salary' else 365)).isoformat()
            scope = json.dumps({'explicit_id': True}) if cat in answer_store.RESTRICTED else conn.execute('SELECT company FROM jobs WHERE url=?', (url,)).fetchone()[0]
            cur = conn.execute('INSERT INTO answer_store(match_regex,question_text,answer,sensitive,source,approved,verified_at,review_by,scope,ts,category,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                               ('^' + re.escape(label) + '$', label, value, int(bool(state.SENSITIVE.search(label))), 'user_cli', 1, state.now(), expiry, scope, state.now(), cat, 'approved'))
            conn.execute("UPDATE blockers SET status='resolved', resolved_by=? WHERE id=?", (cur.lastrowid, ident))
        conn.commit()
    print(f'Recorded answer and resolved {len(matches)} blocker(s).')


def resume_cmd(url, visible=False, ctx=None):
    if ctx is None:
        from playwright.sync_api import sync_playwright
    prof = prepare_profile(json.loads(CONFIG.read_text()))
    bank = []
    with db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM jobs WHERE url=? OR apply_url=?', (url, url)).fetchone()
        if not row:
            raise SystemExit('Job not in database')
        job = dict(row)
        previous = conn.execute('SELECT id FROM attempts WHERE job_url=? ORDER BY id DESC LIMIT 1', (job['url'],)).fetchone()
        attempt = _new_attempt(conn, job, prof, f"resumes attempt {previous['id']}" if previous else 'resume')
        browser = None
        if ctx is None:
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(headless=not visible)
            ctx = browser
        page = ctx.new_page()
        try:
            page.goto(job['apply_url'], wait_until='domcontentloaded', timeout=45000)
            page.wait_for_selector('input, textarea, select', timeout=25000)
            fields = page.evaluate(FIELDS_JS)
            results, missing, _ = prepare_fields(page, job, prof, bank, fields)
            outcome, note = _record_attempt(conn, attempt, job, results, missing, fields, prof=prof, bank=bank)
            conn.execute('UPDATE jobs SET status=?, notes=?, updated=CURRENT_TIMESTAMP WHERE url=?',
                         ('ready' if outcome == 'ready_for_review' else 'needs_you', note, job['url']))
            conn.commit()
            print(f'{outcome}: {note}')
        except Exception as e:
            error = f"Resume failed: {str(e).splitlines()[0][:200]}"
            _record_attempt(conn, attempt, job, [], [], error=error)
            conn.execute("UPDATE jobs SET status='needs_you', notes=? WHERE url=?", (error, job['url']))
            conn.commit()
            print(error)
        finally:
            page.close()
            if browser:
                browser.close()
                playwright.stop()

def skills_cmd():
    import insights, subprocess
    prof = prepare_profile(json.loads(CONFIG.read_text()))
    with db() as conn:
        out = insights.report(conn, prof, log)
    if out:
        subprocess.Popen(["xdg-open", str(out)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def signals_cmd():
    import signals
    with db() as conn:
        signals.refresh(conn, log)
        print("\nCompany signals (PERM filings 2y / WARN notices 180d / surge):")
        for r in conn.execute("SELECT company, perm_cases, warn_count, warn_employees, surge_note FROM company_signals "
                              "WHERE perm_cases>0 OR warn_count>0 OR surge>0 ORDER BY warn_employees DESC, perm_cases DESC"):
            print(f"  {r[0]:<16} PERM {r[1]:>5}   WARN {r[2]:>2} ({r[3]} workers)   {r[4] or ''}")
        print("\nLikely ghost / compliance postings in your pool:")
        for r in conn.execute("SELECT ghost, company, title, ghost_why FROM jobs WHERE fit>=66 AND ghost>=40 ORDER BY ghost DESC LIMIT 25"):
            print(f"  {int(r[0]):>3}  {r[1]} - {r[2]}  [{r[3]}]")


def browse_cmd():
    import subprocess
    import sqlite_vec
    subprocess.run([str(ROOT / ".venv/bin/datasette"), str(DB), "-m", str(ROOT / "datasette.json"),
                    "--load-extension", sqlite_vec.loadable_path(),
                    "--static", f"static:{ROOT / 'static'}", "-o"])


def interview_cmd(url, storm, voice):
    import interview
    prof = prepare_profile(json.loads(CONFIG.read_text()))
    with db() as conn:
        conn.row_factory = sqlite3.Row
        j = conn.execute("SELECT * FROM jobs WHERE url=? OR apply_url=?", (url, url)).fetchone()
    if not j:
        raise SystemExit("Job not in database - use the URL from applications.csv")
    j = dict(j)
    print(f"Researching {j['company']}...")
    b = interview.brief(j, storm)
    print(b[:3000] + ("\n..." if len(b) > 3000 else ""))
    interview.mock(j, prof, b, voice=voice)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--dry", "--dry-run", action="store_true", help="never submit")
    links = sub.add_parser('links', help='find employer ATS links for manual Adzuna leads')
    links.add_argument('--limit', type=int)
    links.add_argument('--dry', action='store_true')
    links.add_argument('--budget', type=float, default=300, help='time budget in seconds')
    for c in ("scan", "export", "report"):
        sub.add_parser(c)
    m = sub.add_parser("mark"); m.add_argument("url")
    m.add_argument("status", choices=["applied", "interview", "rejected", "offer", "skip", "needs_you", "new"])
    a = sub.add_parser("apply"); a.add_argument("url")
    e = sub.add_parser('recruiter-email'); e.add_argument('url')
    sub.add_parser("skills", help="skill-gap report from matching jobs")
    sub.add_parser("signals", help="refresh WARN / PERM / ghost / surge signals and print the summary")
    sub.add_parser("browse", help="open the tracker in Datasette (dark)")
    iv = sub.add_parser("interview", help="company brief + spoken mock interview for one job")
    iv.add_argument("url"); iv.add_argument("--storm", action="store_true"); iv.add_argument("--no-voice", action="store_true")
    sub.add_parser('blockers')
    ans = sub.add_parser('answer'); ans.add_argument('url'); ans.add_argument('label'); ans.add_argument('value')
    res = sub.add_parser('resume'); res.add_argument('url'); res.add_argument('--visible', action='store_true')
    doc = sub.add_parser('doctor', help='inspect application forms without submitting')
    doc.add_argument('urls', nargs='*'); doc.add_argument('--fill', action='store_true')
    doc.add_argument('--from-db', type=int, metavar='N')
    ans_group = sub.add_parser('answers').add_subparsers(dest='answer_action', required=True)
    for action in ('import', 'review', 'approve', 'reject', 'reclassify', 'reopen', 'wizard'):
        parser = ans_group.add_parser(action)
        if action in ('approve', 'reject', 'reopen'):
            parser.add_argument('ids', nargs='+' if action == 'reopen' else '*', type=int)
        if action == 'wizard':
            parser.add_argument('--category', choices=answer_wizard.ORDER)
            parser.add_argument('--resume', action='store_true', help='continue remaining pending answers')
        if action == 'approve':
            parser.add_argument('--category', choices=['profile_fact','attestation','eeo','consent','salary','free_text','other'])
            parser.add_argument('--i-confirm-standing-authorization', action='store_true')
            parser.add_argument('--review-days', type=int)
    sub.add_parser('status')
    sub.add_parser('verify-sweep', help='finish pending Workday email verifications')
    setup_parser = sub.add_parser('setup', help='guided first-time setup')
    setup_parser.add_argument('--non-interactive', action='store_true')
    setup_parser.add_argument('--answers-file')
    imported = sub.add_parser('resume-import', help='review facts from a resume')
    imported.add_argument('path')
    sub.add_parser('facts', help='list resume facts and approval status')
    tracks_group = sub.add_parser('tracks', help='create and edit private career tracks').add_subparsers(dest='tracks_action', required=True)
    tracks_group.add_parser('init').add_argument('--force', action='store_true')
    tracks_group.add_parser('list')
    for action in ('add', 'edit', 'remove', 'default'):
        parser = tracks_group.add_parser(action)
        parser.add_argument('name')
        if action == 'add':
            parser.add_argument('--from', dest='source')
    response = sub.add_parser('response'); response.add_argument('url'); response.add_argument('kind', choices=followup.KINDS)
    response.add_argument('summary', nargs='?', default=''); response.add_argument('--file', dest='file_path')
    sub.add_parser('followups')
    draft = sub.add_parser('followup-draft'); draft.add_argument('url')
    sub.add_parser('page')
    sub.add_parser('review')
    confirm = sub.add_parser('confirm'); confirm.add_argument('url'); confirm.add_argument('--evidence', required=True)
    rec = sub.add_parser('reconcile'); rec.add_argument('attempt_id', type=int); rec.add_argument('outcome', choices=['confirmed','abandoned']); rec.add_argument('--evidence')
    inc = sub.add_parser('income', help='local income experiments').add_subparsers(dest='income_action', required=True)
    p = inc.add_parser('new'); p.add_argument('name'); p.add_argument('--hypothesis', required=True); p.add_argument('--kill', required=True); p.add_argument('--min-days', type=int, required=True); p.add_argument('--budget-cap-cents', type=int, default=0)
    p = inc.add_parser('inventory'); p.add_argument('--file')
    inc.add_parser('offers'); inc.add_parser('status'); inc.add_parser('floors')
    p = inc.add_parser('score-job')
    p.add_argument('--min', dest='minimum', type=float); p.add_argument('--max', dest='maximum', type=float)
    p.add_argument('--unit', choices=('annual', 'hourly'), default='annual')
    p.add_argument('--type', dest='employment_type', choices=('part_time', 'full_time', 'unknown'), default='unknown')
    p.add_argument('--hours', type=float, help='posted hours per week for part-time hourly pay')
    p = inc.add_parser('source'); p.add_argument('exp', type=int); p.add_argument('url')
    p = inc.add_parser('claim'); p.add_argument('exp', type=int)
    group = p.add_mutually_exclusive_group(required=True); group.add_argument('--fact'); group.add_argument('--hypothesis'); group.add_argument('--estimate')
    for option in ('source','quote','test','kill','basis'): p.add_argument('--' + option, type=int if option == 'source' else str)
    for action in ('claims','prepare','report'):
        p = inc.add_parser(action); p.add_argument('exp', type=int)
    p = inc.add_parser('log'); p.add_argument('exp', type=int); p.add_argument('kind', choices=income.KINDS); p.add_argument('value', type=int); p.add_argument('--count', type=int); p.add_argument('--note', default='')
    p = inc.add_parser('publish'); p.add_argument('exp', type=int); p.add_argument('--platform', required=True); p.add_argument('--url', required=True)
    x = ap.parse_args()
    if x.cmd == 'links':
        import employer_link
        with db() as conn:
            result = employer_link.Finder(conn, json.loads(BOARDS.read_text()),
                                          json.loads(CONFIG.read_text()), dry=x.dry,
                                          budget=x.budget).run(limit=x.limit)
        print(f"Employer links: {result['found']} found, {result['missed']} missed, "
              f"{result['unavailable']} unavailable, "
              f"{result['duplicate']} duplicate, {result['skipped']} below floor; "
              f"{result['processed']} processed" + (' (time budget reached)' if result['budget_exhausted'] else ''))
        return
    if x.cmd == 'income':
        try:
            income_cmd(x)
        except (ValueError, OSError) as exc:
            ap.error(str(exc))
        return
    if x.cmd == 'setup':
        import setup_assistant
        with db() as conn:
            setup_assistant.run(conn, ROOT, non_interactive=x.non_interactive, answers_file=x.answers_file)
        return
    if x.cmd == 'resume-import':
        import resume_import
        with db() as conn:
            digest, counts = resume_import.import_resume(conn, x.path)
            if CONFIG.exists():
                profile = json.loads(CONFIG.read_text())
                for key, value in resume_import.approved_facts(conn):
                    if key in ('name','email','phone','location','city','state','zip','linkedin','github','website','timezone'):
                        profile[key] = value
                profile['resume'] = str(Path(x.path).expanduser().resolve())
                fd = os.open(CONFIG, os.O_WRONLY | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w') as f:
                    json.dump(profile, f, indent=2)
                    f.write('\n')
                os.chmod(CONFIG, 0o600)
        print(f'Resume {digest[:12]}: {counts}')
        return
    if x.cmd == 'facts':
        with db() as conn:
            for row in conn.execute('SELECT id,key,value,status,source FROM profile_facts ORDER BY id'):
                print(' | '.join(map(str, row)))
        return
    if x.cmd == 'tracks':
        import tracks_cmd
        with db() as conn:
            try:
                tracks_cmd.run(x.tracks_action, conn, ROOT, getattr(x, 'name', None),
                               getattr(x, 'source', None), getattr(x, 'force', False))
            except (ValueError, OSError) as exc:
                ap.error(str(exc))
        return
    if x.cmd == 'doctor':
        import doctor
        doctor.run(x.urls, x.from_db, x.fill)
        return
    with db() as conn:
        state.recover(conn)
    {"run": lambda: run(x.dry), "verify-sweep": verify_sweep, "scan": scan, "export": export, "report": report,
     "mark": lambda: mark(x.url, x.status), "apply": lambda: manual(x.url), "skills": skills_cmd, "signals": signals_cmd,
     "browse": browse_cmd, "blockers": blockers_cmd, "answer": lambda: answer_cmd(x.url, x.label, x.value),
     "resume": lambda: resume_cmd(x.url, x.visible), "interview": lambda: interview_cmd(x.url, x.storm, not x.no_voice),
     "answers": lambda: answers_cmd(x.answer_action, getattr(x,'ids',()), getattr(x,'category',None), getattr(x,'i_confirm_standing_authorization',False), getattr(x,'review_days',None)),
     "status": status_cmd, "review": review_cmd, "confirm": lambda: confirm_cmd(x.url, x.evidence),
     "response": lambda: response_cmd(x.url, x.kind, x.summary, x.file_path),
     "followups": followups_cmd, "followup-draft": lambda: followup_draft_cmd(x.url),
     "recruiter-email": lambda: recruiter_email_cmd(x.url), "page": page_cmd,
     "reconcile": lambda: reconcile_cmd(x.attempt_id, x.outcome, x.evidence)}[x.cmd]()


if __name__ == "__main__":
    main()
