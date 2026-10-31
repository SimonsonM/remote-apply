"""Local response ledger, follow-up drafts, and private status page. No delivery code."""
import datetime as dt
import hashlib
import html
import os
import re
import shlex
from pathlib import Path

import policy
import state

KINDS = ('acknowledged', 'interview_request', 'rejection', 'offer', 'info_request', 'other')
STATUS = {'interview_request': 'interview', 'rejection': 'rejected', 'offer': 'offer'}


def record_response(conn, url, kind, summary='', file_path=None):
    if kind not in KINDS:
        raise ValueError('unknown response kind')
    if len(summary) > 200:
        raise ValueError('summary must be at most 200 characters')
    row = conn.execute('SELECT url FROM jobs WHERE url=? OR apply_url=?', (url, url)).fetchone()
    if not row:
        raise ValueError('job URL not found')
    url = row[0]
    digest = hashlib.sha256(Path(file_path).read_bytes()).hexdigest() if file_path else None
    attempt = conn.execute('SELECT id FROM attempts WHERE job_url=? ORDER BY id DESC LIMIT 1', (url,)).fetchone()
    conn.execute('''INSERT INTO responses(job_url,attempt_id,kind,received_at,source,summary,text_sha256,ts)
                    VALUES (?,?,?,?,?,?,?,?)''',
                 (url, attempt[0] if attempt else None, kind, state.now(), 'file' if file_path else 'user', summary, digest, state.now()))
    conn.execute("UPDATE followups SET status='dismissed' WHERE job_url=? AND status IN ('due','drafted')", (url,))
    conn.commit()
    return url


def due_followups(conn, today=None):
    today = today or dt.date.today().isoformat()
    return conn.execute('''SELECT f.id,j.company,j.title,f.job_url,f.due,f.status FROM followups f
        LEFT JOIN jobs j ON j.url=f.job_url WHERE f.status IN ('due','drafted') AND f.due<=?
        ORDER BY f.due,f.id''', (today,)).fetchall()


def draft(conn, url, folder, profile=None):
    row = conn.execute('SELECT url,company,title,applied_at FROM jobs WHERE url=? OR apply_url=?', (url, url)).fetchone()
    if not row:
        raise ValueError('job URL not found')
    url, company, title, applied = row
    item = conn.execute("SELECT id FROM followups WHERE job_url=? AND status IN ('due','drafted') ORDER BY id DESC LIMIT 1", (url,)).fetchone()
    if not item:
        raise ValueError('no active follow-up for this job')
    # This permission check is kept at the message boundary. A local draft never sends.
    policy.check('message', {'company': company, 'title': title}, profile or {})
    applied = applied or conn.execute("SELECT substr(updated,1,10) FROM attempts WHERE job_url=? AND state='confirmed' ORDER BY id DESC LIMIT 1", (url,)).fetchone()[0]
    body = (f"DRAFT - not sent\n\nSubject: Following up on {title}\n\n"
            f"Hello {company} team,\n\nI applied for the {title} role on {applied}. "
            "I remain interested in the opportunity and would appreciate any update you can share.\n\n"
            "Thank you for your time.\n")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r'[^A-Za-z0-9_-]+', '_', company).strip('_')[:60] or 'company'
    path = folder / f'{slug}_{dt.date.today().isoformat()}.txt'
    if path.exists():
        raise FileExistsError(f'draft already exists: {path}')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        out.write(body)
    conn.execute("UPDATE followups SET status='drafted',draft_path=? WHERE id=?", (str(path), item[0]))
    conn.commit()
    return path


def render_page(conn):
    e = lambda value: html.escape(str(value if value is not None else ''), quote=True)
    q = shlex.quote
    def section(title, rows):
        return f'<section><h2>{e(title)}</h2>' + (''.join(rows) or '<p>None</p>') + '</section>'
    jobs = {r[0]: (r[1], r[2]) for r in conn.execute('SELECT url,company,title FROM jobs')}
    grouped = {}
    for _, url, label, _, kind in state.open_blockers(conn):
        grouped.setdefault(url, []).append((label, kind))
    needs = []
    if conn.execute("SELECT 1 FROM answer_store WHERE status='pending' LIMIT 1").fetchone():
        needs.append('<p><code>.venv/bin/python apply.py answers wizard</code></p>')
    for url, items in grouped.items():
        company, title = jobs.get(url, ('', ''))
        inner = ''.join(f'<li>{e(label)} · {e(kind)}<code>{e("python apply.py answer " + q(url) + " " + q(label) + " YOUR_ANSWER")}</code></li>' for label, kind in items)
        needs.append(f'<article><h3>{e(company)} — {e(title)}</h3><ul>{inner}</ul></article>')
    ready, uncertain, confirmed = [], [], []
    for ident, url, status, updated in conn.execute('SELECT id,job_url,state,updated FROM attempts ORDER BY id DESC'):
        company, title = jobs.get(url, ('', ''))
        head = f'{e(company)} — {e(title)}'
        if status == 'ready_for_review':
            ready.append(f'<p>{head} <code>{e("python apply.py review")}</code></p>')
        elif status in ('submitting', 'uncertain'):
            uncertain.append(f'<p>{head} ({e(status)}) <code>{e("python apply.py reconcile " + str(ident) + " confirmed --evidence YOUR_EVIDENCE")}</code></p>')
        elif status == 'confirmed':
            evidence = conn.execute('SELECT 1 FROM evidence WHERE attempt_id=? LIMIT 1', (ident,)).fetchone()
            confirmed.append(f'<p>{head} — {e(updated[:10])} — evidence: {"yes" if evidence else "no"}</p>')
    responses = []
    for url, kind, received, source, summary in conn.execute('SELECT job_url,kind,received_at,source,summary FROM responses ORDER BY id DESC'):
        company, title = jobs.get(url, ('', ''))
        responses.append(f'<p>{e(company)} — {e(title)} · {e(kind)} · {e(received[:10])} · {e(source)} · {e(summary)}</p>')
    follows = []
    for _, company, title, url, due, status in due_followups(conn):
        follows.append(f'<p>{e(company)} — {e(title)} · due {e(due)} · {e(status)} <code>{e("python apply.py followup-draft " + q(url))}</code></p>')
    income_rows = []
    for ident, name, status, over_budget in conn.execute('SELECT id,name,status,over_budget FROM income_experiments ORDER BY id DESC'):
        income_rows.append(f'<p>{e(ident)} · {e(name)} · {e(status)}' + (' · over budget' if over_budget else '') + '</p>')
    content = ''.join((section('Needs you now', needs), section('Ready to review', ready),
                       section('Submitting/Uncertain', uncertain), section('Confirmed', confirmed),
                       section('Responses', responses), section('Follow-ups due', follows), section('Income experiments', income_rows)))
    return ("<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Application status</title><style>:root{color-scheme:light dark}body{font:16px system-ui,sans-serif;"
            "max-width:72rem;margin:auto;padding:1.5rem;background:#fff;color:#161616}section{border-top:2px solid #555;padding:1rem 0}"
            "code{display:block;overflow-wrap:anywhere;padding:.5rem;background:#eee;color:#111}"
            "@media(prefers-color-scheme:dark){body{background:#10151b;color:#f2f5f8}code{background:#25303c;color:#fff}section{border-color:#9db6d0}}"
            "</style><h1>Application status</h1>" + content + '</html>')


def write_page(conn, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        out.write(render_page(conn))
    os.chmod(path, 0o600)
    return path
