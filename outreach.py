"""Nightly outreach drafts (never sent): a short note to the recruiter or hiring manager for strong fresh
applications, and a follow-up for applications that have gone quiet. Drafts land in followups/ and on the
dashboard's Outreach page; the user decides whether to send each one.

Usage: outreach.py [--recruiter N] [--followup N]
"""
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
DB = ROOT / "applications.sqlite3"
CONFIG = ROOT / "profile.json"


def table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS outreach (id INTEGER PRIMARY KEY, job_url TEXT, kind TEXT, path TEXT,
                    created TEXT, status TEXT DEFAULT 'draft', UNIQUE(job_url, kind))""")


def people_links(company, title):
    """LinkedIn people searches to find who to send it to (opened by the user, nothing is scraped)."""
    base = "https://www.linkedin.com/search/results/people/?keywords="
    role = "program" if "program" in (title or "").lower() else (title or "").split(",")[0]
    return {"recruiter": base + quote(f"{company} recruiter"),
            "hiring_manager": base + quote(f"{company} {role} manager")}


def recruiter_drafts(conn, profile, limit=5, min_fit=70, days=3):
    import smart
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows = conn.execute("""SELECT rowid, url, company, title, description, job_id FROM jobs WHERE status='applied' AND applied_at >= ?
                           AND coalesce(fit,0) >= ? AND url NOT IN (SELECT job_url FROM outreach WHERE kind='recruiter')
                           ORDER BY fit DESC LIMIT ?""", (since, min_fit, limit)).fetchall()
    made = 0
    resume = smart.grounded_resume_text(profile)
    for rowid, url, company, title, desc, job_id in rows:
        job = dict(url=url, company=company, title=title, description=desc or "")
        try:
            draft = smart.recruiter_email(profile, job)
        except Exception as exc:
            print(f"outreach: draft failed {company}: {str(exc)[:100]}")
            continue
        if not draft or not smart.check_grounding(draft, resume, profile.get("never_claim", []), profile=profile, job=job):
            conn.execute("INSERT OR IGNORE INTO outreach(job_url,kind,path,created,status) VALUES (?,?,?,?,?)",
                         (url, "recruiter", "", dt.date.today().isoformat(), "rejected_grounding"))
            continue
        path = ROOT / "followups" / f"recruiter_{''.join(c if c.isalnum() else '_' for c in company)[:40]}_{job_id or rowid}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"DRAFT - not sent\n\n{draft}\n", encoding="utf-8")
        conn.execute("INSERT OR IGNORE INTO outreach(job_url,kind,path,created) VALUES (?,?,?,?)",
                     (url, "recruiter", str(path), dt.date.today().isoformat()))
        made += 1
    conn.commit()
    return made


def followup_drafts(conn, profile, limit=10):
    import followup
    made = 0
    for _id, company, title, url, due, status in followup.due_followups(conn)[:limit]:
        if conn.execute("SELECT 1 FROM outreach WHERE job_url=? AND kind='followup'", (url,)).fetchone():
            continue
        if (conn.execute("SELECT status FROM jobs WHERE url=?", (url,)).fetchone() or ["?"])[0] != "applied":
            continue   # rejected / interviewing: no nudge
        try:
            path = followup.draft(conn, url, ROOT / "followups", profile)
        except Exception as exc:
            print(f"outreach: follow-up skipped {company}: {str(exc)[:100]}")
            continue
        conn.execute("INSERT OR IGNORE INTO outreach(job_url,kind,path,created) VALUES (?,?,?,?)",
                     (url, "followup", str(path), dt.date.today().isoformat()))
        made += 1
    conn.commit()
    return made


def items(conn, limit=60):
    """Drafts waiting on the user, for the dashboard."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outreach'").fetchone():
        return []
    out = []
    for oid, url, kind, path, created, status, company, title, fit, applied in conn.execute(
            """SELECT o.id, o.job_url, o.kind, o.path, o.created, o.status, j.company, j.title, j.fit, j.applied_at
               FROM outreach o LEFT JOIN jobs j ON j.url=o.job_url WHERE o.status='draft' ORDER BY o.created DESC, o.id DESC LIMIT ?""",
            (limit,)):
        try:
            text = Path(path).read_text(encoding="utf-8").replace("DRAFT - not sent\n\n", "", 1).strip()
        except OSError:
            continue
        out.append({"id": oid, "kind": kind, "company": company, "title": title, "fit": round(fit or 0),
                    "applied_at": applied, "created": created, "text": text, "job_url": url, **people_links(company, title)})
    return out


def set_status(conn, oid, status):
    if status not in ("sent", "skipped", "draft"):
        raise ValueError(status)
    table(conn)
    conn.execute("UPDATE outreach SET status=? WHERE id=?", (status, oid))
    conn.commit()


def main(argv):
    profile = json.loads(CONFIG.read_text())
    rec = int(argv[argv.index("--recruiter") + 1]) if "--recruiter" in argv else profile.get("outreach_recruiter_per_night", 5)
    fol = int(argv[argv.index("--followup") + 1]) if "--followup" in argv else profile.get("outreach_followup_per_night", 10)
    with sqlite3.connect(DB, timeout=60) as conn:
        table(conn)
        r = recruiter_drafts(conn, profile, rec) if rec else 0
        f = followup_drafts(conn, profile, fol) if fol else 0
    print(f"outreach: {r} recruiter drafts, {f} follow-up drafts (nothing sent)")


if __name__ == "__main__":
    main(sys.argv[1:])
