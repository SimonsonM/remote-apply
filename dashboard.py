"""Local, read-only Remote Apply dashboard. Run: python dashboard.py"""

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
DB = ROOT / "applications.sqlite3"
ASSETS = ROOT / "dashboard"
TITLES = {"mr", "mrs", "ms", "dr", "prof"}


def first_name():
    """First name from the imported resume (resume_facts.json), else profile.json; '' if neither has one."""
    for path in (ROOT / "resume_facts.json", ROOT / "profile.json"):
        try:
            full = json.loads(path.read_text()).get("name") or ""
        except Exception:
            continue
        words = [w.strip(".,") for w in full.split(",")[0].split()]
        words = [w for w in words if w and w.lower() not in TITLES]
        if words:
            return words[0].capitalize() if words[0].isupper() or words[0].islower() else words[0]
    return ""


ACTION_STATUSES = ("staged", "captcha", "needs_you", "unconfirmed", "manual")


def safe_url(value):
    """Only send browser links to ordinary web URLs."""
    value = (value or "").strip()
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    return value if parts.scheme in ("http", "https") and parts.netloc else ""


def action_detail(status, notes, blockers):
    note = (notes or "").lower()
    if status == "staged":
        return "Review & submit", "Review the prepared form, then submit it yourself."
    if status == "captcha":
        return "Solve CAPTCHA", "Complete the verification and submit the application."
    if status == "unconfirmed":
        return "Verify submission", "Check the employer site or confirmation email before recording it as applied."
    if status == "manual":
        return "Apply manually", "Open the employer listing and complete the application."
    if blockers:
        return "Answer required", "An open question or blocker needs your input."
    if "arbitration" in note or "waiver" in note:
        return "Review terms", "Decide whether to accept the employer's terms."
    if "cover" in note or "draft" in note:
        return "Review writing", "Review or complete the written answer."
    if "sign-in" in note or "password" in note or "verification" in note:
        return "Sign in", "Complete the account or verification step."
    return "Finish application", "Open the application and complete the remaining steps."


def outcomes(counts, conn=None, today=None):
    """Response statistics over everything actually submitted (applied, rejected, interview, offer)."""
    applied, rejected = counts.get("applied", 0), counts.get("rejected", 0)
    talking = counts.get("interview", 0) + counts.get("offer", 0)
    submitted = applied + rejected + talking
    pct = lambda n: round(100 * n / submitted, 1) if submitted else 0.0
    ghosted = rejected_week = 0
    if conn is not None:
        today = today or date.today()
        ghosted = conn.execute("SELECT count(*) FROM jobs WHERE status='applied' AND applied_at != '' "
                               "AND substr(applied_at,1,10) < ?", ((today - timedelta(days=30)).isoformat(),)).fetchone()[0]
        rejected_week = conn.execute("SELECT count(*) FROM jobs WHERE status='rejected' AND substr(updated,1,10) >= ?",
                                     ((today - timedelta(days=7)).isoformat(),)).fetchone()[0]
    return {"submitted": submitted, "rejected": rejected, "rejected_pct": pct(rejected),
            "rejected_week": rejected_week, "interviews": talking, "interview_pct": pct(talking),
            "responded": rejected + talking, "response_pct": pct(rejected + talking),
            "awaiting": applied, "awaiting_pct": pct(applied), "no_reply_30d": ghosted}


SUBMITTED = ("applied", "rejected", "interview", "offer")
LEVELS = (("Director / VP", r"\b(director|head of|vp|vice president|chief)\b"),
          ("Staff / Principal / Lead", r"\b(staff|principal|lead)\b"),
          ("Senior", r"\b(senior|sr\.?)\b"),
          ("Manager", r"\bmanager\b"))
REMOTE_WORDS = re.compile(r"remote|anywhere|distributed|work from home", re.I)


def level_of(title):
    for name, pattern in LEVELS:
        if re.search(pattern, title or "", re.I):
            return name
    return "Individual contributor"


def _rate_rows(groups):
    rows = []
    for label, items in groups.items():
        sent = len(items)
        heard = sum(1 for st in items if st in ("rejected", "interview", "offer"))
        rows.append({"label": label, "applied": sent, "rejected": sum(1 for st in items if st == "rejected"),
                     "interviews": sum(1 for st in items if st in ("interview", "offer")),
                     "response_pct": round(100 * heard / sent, 1) if sent else 0.0})
    return sorted(rows, key=lambda r: -r["applied"])


LEVEL_WORDS = re.compile(r"\b(senior|sr|staff|principal|lead|junior|jr|associate|ii|iii|iv|i|1|2|3)\b\.?", re.I)


def title_family(title):
    """'Senior Program Manager, Risk Ops (Remote)' -> 'Program Manager'."""
    parts = re.split(r"\s[-–—|]\s|,|\(|/", title or "")
    clean = lambda t: re.sub(r"\s+", " ", LEVEL_WORDS.sub(" ", t or "")).strip(" .-:")
    core = clean(parts[0])
    if core.lower() in ("manager", "director", "head", "lead", "vp", "head of") and len(parts) > 1 and clean(parts[1]):
        core = f"{clean(parts[1])} {core}"   # 'Manager, Implementation' -> 'Implementation Manager'
    core = re.sub(r"\bof of\b", "of", core)
    return core.title() if core else (title or "Untitled")


SYSTEM_NAMES = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "workday": "Workday", "workable": "Workable",
                "smartrecruiters": "SmartRecruiters", "icims": "iCIMS", "eightfold": "Eightfold", "microsoft": "Microsoft Careers",
                "adzuna": "Adzuna", "remotive": "Remotive", "himalayas": "Himalayas", "usajobs": "USAJOBS",
                "roberthalf": "Robert Half", "michaelpage": "Michael Page", "adecco": "Adecco"}


def _with_all_systems(rows):
    """Every system Remote Apply can apply through or find jobs on, with zeros where nothing has gone out yet."""
    seen = {r["label"] for r in rows}
    return rows + [{"label": name, "applied": 0, "rejected": 0, "interviews": 0, "response_pct": 0.0}
                   for name in SYSTEM_NAMES.values() if name not in seen]


STOP = set("""a an and are as at be by for from has have in is it its of on or our that the their this to we will with you your
who what when where which while all any can may not more most must other such than then these they those through up
us via was were would also about across into within per new work team role job company including include etc able strong
experience years year looking join help using use well based skills ability best great own both each how why one two""".split())


BOILER = set("""equal opportunity employer salary range base compensation pay bonus equity benefits benefit insurance medical dental vision
401k 401 pto leave parental vacation holidays holiday time off sexual orientation gender identity expression race color religion
national origin veteran status disability disabilities protected qualified applicants applicant accommodation accommodations
age marital pregnancy genetic citizenship ancestry criminal background check privacy notice policy candidates candidate eeo
affirmative action law laws regulations perks wellness stipend reasonable hiring location locations remote hybrid office
offices california colorado york washington states united regard note nbsp amp interview interviews recruiting recruiter
consideration employment please receive process apply applying application email emails fraud scam statement visit learn
click website www com http https team members culture mission values diverse diversity inclusion inclusive belonging
preferred qualifications qualification every day employee stock purchase san francisco quality""".split())


def resume_text():
    parts = []
    for name in ("resume.md", "resume_facts.json"):
        try:
            parts.append((ROOT / name).read_text())
        except OSError:
            pass
    return re.sub(r"[^a-z0-9+#/ ]", " ", " ".join(parts).lower())


def keyword_gaps(rows, limit=14):
    """Two-word phrases that recur across postings you applied to but appear nowhere in your resume text.
    Add only the ones that are true for you."""
    have = resume_text()
    if not have.strip():
        return []
    have_words = set(have.split())
    seen = Counter()
    for desc in rows:
        words = [w for w in re.sub(r"[^a-z0-9+#/ ]", " ", (desc or "").lower()).split()]
        grams = {f"{a} {b}" for a, b in zip(words, words[1:]) if a not in STOP and b not in STOP and len(a) > 2 and len(b) > 2
                 and a not in BOILER and b not in BOILER
                 and not a.isdigit() and not b.isdigit()}
        seen.update(grams)
    n = max(1, len(rows))
    out = []
    for gram, count in seen.most_common(1500):
        if count < max(3, n * 0.06) or gram in have:
            continue
        if all(w in have_words for w in gram.split()) and count < n * 0.15:
            continue   # both words already there: likely phrased differently, only flag if very common
        out.append({"phrase": gram, "pct": round(100 * count / n, 1)})
        if len(out) >= limit:
            break
    return out


def insights(conn, today=None):
    """Market read-outs from what was submitted and what came back."""
    today = today or date.today()
    cols = {c[1] for c in conn.execute("PRAGMA table_info(jobs)")}
    variant_col = "resume_variant" if "resume_variant" in cols else "''"
    posted_col = "coalesce(posted_at,'')" if "posted_at" in cols else "''"
    rows = conn.execute(f"SELECT company, title, location, source, fit, status, applied_at, updated, {posted_col}, {variant_col} FROM jobs "
                        f"WHERE status IN ({','.join('?' * len(SUBMITTED))}) AND applied_at != ''", SUBMITTED).fetchall()
    by = lambda key: _rate_rows({k: [r[5] for r in rows if key(r) == k] for k in {key(r) for r in rows}})
    band = lambda r: "70+" if (r[4] or 0) >= 70 else "65–69" if (r[4] or 0) >= 65 else "60–64" if (r[4] or 0) >= 60 else "under 60"
    waits = sorted(max(0, (date.fromisoformat(r[7][:10]) - date.fromisoformat(r[6][:10])).days)
                   for r in rows if r[5] == "rejected" and r[7] and r[6])
    silent = sum(1 for r in rows if r[5] == "applied" and (today - date.fromisoformat(r[6][:10])).days >= 14)
    applied_open = sum(1 for r in rows if r[5] == "applied")
    lags = sorted(max(0, (date.fromisoformat(r[6][:10]) - date.fromisoformat(r[8][:10])).days)
                  for r in rows if r[8] and r[6] and re.match(r"\d{4}-\d{2}-\d{2}", r[8]))
    pretty = {"programmgmt": "Program management", "aiops": "AI operations", "itsecurity": "IT / security", "marketing": "Marketing"}
    variants = by(lambda r: pretty.get((r[9] or "").lower(), (r[9] or "Base resume").replace("_", " ").title()))
    weekdays = Counter(date.fromisoformat(r[6][:10]).strftime("%a") for r in rows)
    rejecters = Counter(r[0] for r in rows if r[5] == "rejected").most_common(6)
    discovered = conn.execute("SELECT count(*) FROM jobs").fetchone()[0]
    qualified = conn.execute("SELECT count(*) FROM jobs WHERE coalesce(fit,0) >= 58").fetchone()[0]
    attempted = conn.execute("SELECT count(*) FROM jobs WHERE status IN ('applied','rejected','interview','offer','unconfirmed')").fetchone()[0]
    heard = sum(1 for r in rows if r[5] in ("rejected", "interview", "offer"))
    talks = sum(1 for r in rows if r[5] in ("interview", "offer"))
    return {
        "by_level": by(lambda r: level_of(r[1])),
        "by_source": _with_all_systems(by(lambda r: SYSTEM_NAMES.get(r[3] or "", (r[3] or "Other").title()))),
        "by_fit": sorted(by(band), key=lambda r: r["label"], reverse=True),
        "by_mode": by(lambda r: "Remote" if REMOTE_WORDS.search(r[2] or "") else "Local / hybrid"),
        "funnel": [{"label": "Discovered", "count": discovered}, {"label": "Good fit", "count": qualified},
                   {"label": "Submitted", "count": attempted}, {"label": "Heard back", "count": heard},
                   {"label": "Interview", "count": talks}],
        "median_days_to_rejection": waits[len(waits) // 2] if waits else None,
        "fastest_rejection_days": waits[0] if waits else None,
        "silent_14d": silent, "silent_14d_pct": round(100 * silent / applied_open, 1) if applied_open else 0.0,
        "weekdays": [{"day": d, "count": weekdays.get(d, 0)} for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")],
        "top_rejecters": [{"company": c, "count": n} for c, n in rejecters],
        "median_days_posted_to_applied": lags[len(lags) // 2] if lags else None,
        "applied_within_2_days_pct": round(100 * sum(1 for x in lags if x <= 2) / len(lags), 1) if lags else None,
        "by_resume": variants,
        "keyword_gaps": keyword_gaps([r[0] for r in conn.execute(
            "SELECT description FROM jobs WHERE status IN ('applied','rejected','interview','offer','unconfirmed') AND coalesce(fit,0) >= 62")])
            if "description" in cols else [],
        "top_titles": sorted(by(lambda r: title_family(r[1])), key=lambda r: (-r["applied"], r["label"]))[:8],
    }


def threats(conn, days=60):
    """Scam scanner results (threat_scan.py); empty if it has never run."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mail_threats'").fetchone():
        return {"scanned": 0, "suspicious": 0, "high": 0, "items": [], "countries": []}
    since = (date.today() - timedelta(days=days)).isoformat()
    counts = dict(conn.execute("SELECT level, count(*) FROM mail_threats GROUP BY level"))
    items = []
    for row in conn.execute("SELECT received_at, from_addr, from_domain, reply_to, subject, ip, ip_host, ip_country, ip_org, "
                            "spf, dkim, dmarc, domain_age_days, score, level, reasons, mailbox FROM mail_threats "
                            "WHERE level != 'clean' ORDER BY score DESC, received_at DESC LIMIT 40"):
        keys = ("received_at", "from_addr", "from_domain", "reply_to", "subject", "ip", "ip_host", "ip_country", "ip_org",
                "spf", "dkim", "dmarc", "domain_age_days", "score", "level", "reasons", "mailbox")
        item = dict(zip(keys, row))
        item["reasons"] = json.loads(item["reasons"] or "[]")
        items.append(item)
    countries = [{"country": c or "?", "count": n} for c, n in conn.execute(
        "SELECT ip_country, count(*) FROM mail_threats WHERE ip != '' GROUP BY ip_country ORDER BY count(*) DESC LIMIT 8")]
    auth = dict(conn.execute("SELECT CASE WHEN spf='pass' AND dkim='pass' THEN 'pass' ELSE 'weak' END, count(*) FROM mail_threats GROUP BY 1"))
    return {"scanned": sum(counts.values()), "suspicious": counts.get("suspicious", 0), "high": counts.get("high", 0),
            "auth_pass": auth.get("pass", 0), "auth_weak": auth.get("weak", 0),
            "recent_flags": conn.execute("SELECT count(*) FROM mail_threats WHERE level != 'clean' AND received_at >= ?", (since,)).fetchone()[0],
            "items": items, "countries": countries}


def _outreach(conn):
    try:
        import outreach
        return outreach.items(conn)
    except Exception:
        return []


def snapshot(db_path=DB):
    """Build one consistent read-only view from the current jobs database."""
    today = date.today()
    days = [(today - timedelta(days=13 - i)).isoformat() for i in range(14)]
    empty = {"database_found": False, "as_of": today.isoformat(), "status_counts": {},
             "source_counts": [], "activity": [{"date": day, "count": 0} for day in days],
             "jobs": [], "outcomes": outcomes({}), "insights": None, "threats": None, "outreach": [],
             "totals": {"tracked": 0, "action": 0, "applied": 0,
                        "interviews": 0, "offers": 0, "open_blockers": 0}}
    if not Path(db_path).is_file():
        return empty

    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
            return empty
        counts = dict(conn.execute("SELECT status, count(*) FROM jobs GROUP BY status"))
        source_counts = [{"source": source or "Other", "count": count} for source, count in
                         conn.execute("SELECT source, count(*) FROM jobs WHERE status IN ('applied','interview','offer') "
                                      "GROUP BY source ORDER BY count(*) DESC LIMIT 6")]
        activity_counts = dict(conn.execute(
            "SELECT substr(applied_at,1,10), count(*) FROM jobs "
            "WHERE applied_at >= ? AND applied_at != '' GROUP BY substr(applied_at,1,10)", (days[0],)))
        jobs = [dict(row) for row in conn.execute(
            "SELECT url, apply_url, company, title, location, salary, fit, status, notes, source, "
            "updated, found_at FROM jobs WHERE status IN (?,?,?,?,?) "
            "ORDER BY CASE status WHEN 'staged' THEN 0 WHEN 'captcha' THEN 1 "
            "WHEN 'needs_you' THEN 2 WHEN 'unconfirmed' THEN 3 ELSE 4 END, "
            "fit DESC, found_at DESC", ACTION_STATUSES)]
        blockers = Counter()
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='blockers'").fetchone():
            for job_url, count in conn.execute(
                "SELECT a.job_url, count(*) FROM blockers b JOIN attempts a ON a.id=b.attempt_id "
                "WHERE b.status='open' AND a.id=(SELECT max(id) FROM attempts WHERE job_url=a.job_url) "
                "GROUP BY a.job_url"):
                blockers[job_url] = count
        for job in jobs:
            count = blockers[job["url"]]
            label, guidance = action_detail(job["status"], job["notes"], count)
            job["blockers"] = count
            job["action_label"] = label
            job["guidance"] = guidance
            job["link"] = safe_url(job["apply_url"]) or safe_url(job["url"])
            job["fit"] = round(job["fit"] or 0)
            job.pop("apply_url")
            job.pop("url")
        return {"database_found": True, "as_of": today.isoformat(), "status_counts": counts,
                "source_counts": source_counts,
                "activity": [{"date": day, "count": activity_counts.get(day, 0)} for day in days],
                "jobs": jobs, "outcomes": outcomes(counts, conn), "insights": insights(conn), "threats": threats(conn), "outreach": _outreach(conn),
                "totals": {"tracked": sum(counts.values()), "action": len(jobs),
                           "applied": counts.get("applied", 0),
                           "interviews": counts.get("interview", 0), "offers": counts.get("offer", 0),
                           "open_blockers": sum(blockers.values())}}
    finally:
        conn.close()


CACHE = ROOT / ".dashboard_cache.json"


def _save_cache(data):
    try:
        CACHE.write_text(json.dumps({"saved_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "data": data}))
    except OSError:
        pass


def _load_cache():
    """Last successful snapshot, marked stale; None if there has never been one."""
    try:
        cached = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return None
    return {**cached["data"], "stale_since": cached["saved_at"]}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        """The one write: mark an outreach draft sent or skipped (/api/outreach/<id>/<status>), same-origin only."""
        m = re.fullmatch(r"/api/outreach/(\d+)/(sent|skipped|draft)", self.path)
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        if not m or (origin and origin != f"http://{host}"):
            self.send_error(403)
            return
        import outreach
        with sqlite3.connect(DB, timeout=10) as conn:
            outreach.set_status(conn, int(m.group(1)), m.group(2))
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/dashboard":
            try:
                data = {**snapshot(), "first_name": first_name()}
                _save_cache(data)
            except sqlite3.Error as exc:
                # An application run holds the write lock for long stretches; show the last good data instead of nothing.
                data = _load_cache()
                if data is None:
                    self.send_error(503, f"Database unavailable: {exc}")
                    return
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        else:
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
                      "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8")}
            if path not in assets:
                self.send_error(404)
                return
            filename, content_type = assets[path]
            body = (ASSETS / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the local Remote Apply dashboard")
    parser.add_argument("--port", type=int, default=8675)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Remote Apply dashboard: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
