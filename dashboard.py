"""Local, read-only Remote Apply dashboard. Run: python dashboard.py"""

import argparse
import json
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


def snapshot(db_path=DB):
    """Build one consistent read-only view from the current jobs database."""
    today = date.today()
    days = [(today - timedelta(days=13 - i)).isoformat() for i in range(14)]
    empty = {"database_found": False, "as_of": today.isoformat(), "status_counts": {},
             "source_counts": [], "activity": [{"date": day, "count": 0} for day in days],
             "jobs": [], "totals": {"tracked": 0, "action": 0, "applied": 0,
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
                "jobs": jobs,
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
