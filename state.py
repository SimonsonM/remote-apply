"""Durable application attempts and field observations (stdlib only)."""
import datetime as dt
import hashlib
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TABLES = ('attempts', 'field_results', 'blockers', 'answer_store', 'standing_authorizations', 'evidence', 'responses', 'followups', 'profile_facts', 'income_experiments', 'income_sources', 'income_claims', 'income_assets', 'income_events', 'income_decisions', 'workday_accounts', 'workday_tenants')
SENSITIVE = re.compile(r'gender|race|ethnic|hispanic|veteran|disabilit|orientation|pronoun|salary|compensation|pay|ssn|social security|date of birth|visa|sponsor|authori[sz]ed|consent|privacy|agree|acknowledge|personal data|conflict|non-?compete|restrictive|government official|sanction|clearance|18 years|relocat', re.I)
TRANSITIONS = {
    'new': {'blocked', 'ready_for_review'},
    'blocked': {'ready_for_review', 'abandoned'},
    'ready_for_review': {'submitting', 'blocked', 'abandoned'},
    'submitting': {'confirmed', 'uncertain', 'abandoned'},
    'uncertain': set(), 'confirmed': set(), 'abandoned': set(),
}

class DuplicateSubmission(RuntimeError):
    pass

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def backup_db(path):
    path = Path(path)
    folder = path.parent / 'backups'
    folder.mkdir(exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    out = folder / f'applications.{stamp}.sqlite3'
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    with sqlite3.connect(path) as src, sqlite3.connect(out) as dest:
        src.backup(dest)
    os.chmod(out, 0o600)
    return out

def migrate(conn):
    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    columns = {r[1] for r in conn.execute('PRAGMA table_info(answer_store)')} if 'answer_store' in existing else set()
    field_columns = {r[1] for r in conn.execute('PRAGMA table_info(field_results)')} if 'field_results' in existing else set()
    if set(TABLES) <= existing and {'category', 'status'} <= columns and 'review_status' in field_columns:
        return
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    if path:
        os.chmod(path, 0o600)
        backup_db(path)
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY, job_url TEXT NOT NULL, state TEXT NOT NULL,
      created TEXT NOT NULL, updated TEXT NOT NULL, profile_sha256 TEXT, resume_sha256 TEXT, note TEXT);
    CREATE TABLE IF NOT EXISTS field_results (id INTEGER PRIMARY KEY, attempt_id INTEGER NOT NULL, field_key TEXT,
      label TEXT, required INTEGER, kind TEXT, status TEXT NOT NULL CHECK(status IN ('filled','failed','unanswered','skipped')),
      error TEXT, sensitive INTEGER, value_sha256 TEXT, value_preview TEXT, source TEXT, ts TEXT NOT NULL, review_status TEXT);
    CREATE TABLE IF NOT EXISTS blockers (id INTEGER PRIMARY KEY, attempt_id INTEGER NOT NULL, label TEXT,
      field_key TEXT, kind TEXT, status TEXT NOT NULL, resolved_by INTEGER, ts TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS answer_store (id INTEGER PRIMARY KEY, match_regex TEXT, question_text TEXT,
      answer TEXT, sensitive INTEGER, source TEXT, approved INTEGER, verified_at TEXT, review_by TEXT, scope TEXT, ts TEXT NOT NULL,
      category TEXT DEFAULT 'other', status TEXT DEFAULT 'pending');
    CREATE TABLE IF NOT EXISTS standing_authorizations (id INTEGER PRIMARY KEY, category TEXT, scope TEXT, approved_at TEXT, expires TEXT);
    CREATE TABLE IF NOT EXISTS evidence (id INTEGER PRIMARY KEY, attempt_id INTEGER, kind TEXT, path TEXT, sha256 TEXT,
      url TEXT, page_text_sha256 TEXT, doc_hashes TEXT, ts TEXT);
    CREATE TABLE IF NOT EXISTS responses (id INTEGER PRIMARY KEY, job_url TEXT NOT NULL, attempt_id INTEGER,
      kind TEXT NOT NULL CHECK(kind IN ('acknowledged','interview_request','rejection','offer','info_request','other')),
      received_at TEXT NOT NULL, source TEXT NOT NULL, summary TEXT NOT NULL, text_sha256 TEXT, ts TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS followups (id INTEGER PRIMARY KEY, job_url TEXT NOT NULL, attempt_id INTEGER,
      due TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('due','drafted','done','dismissed')),
      draft_path TEXT, ts TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS profile_facts (id INTEGER PRIMARY KEY, key TEXT NOT NULL, value TEXT NOT NULL,
      source TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0, verified_at TEXT, review_by TEXT,
      resume_sha256 TEXT, status TEXT NOT NULL DEFAULT 'proposed');
    CREATE INDEX IF NOT EXISTS profile_facts_key ON profile_facts(key,status);
    CREATE UNIQUE INDEX IF NOT EXISTS followups_attempt_kind ON followups(attempt_id,kind);
    CREATE TABLE IF NOT EXISTS income_experiments (id INTEGER PRIMARY KEY, name TEXT NOT NULL, hypothesis TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('draft','researching','ready','published','measuring','stopped','continued')),
      created TEXT NOT NULL, updated TEXT NOT NULL, budget_cap_cents INTEGER NOT NULL DEFAULT 0,
      platform TEXT, offer_json TEXT, kill_criteria TEXT NOT NULL, min_exposure_days INTEGER NOT NULL DEFAULT 7,
      published_url TEXT, published_at TEXT, over_budget INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS income_sources (id INTEGER PRIMARY KEY, experiment_id INTEGER NOT NULL REFERENCES income_experiments(id),
      url TEXT NOT NULL, title TEXT, retrieved_at TEXT NOT NULL, http_status INTEGER NOT NULL,
      text_sha256 TEXT NOT NULL, text_path TEXT NOT NULL, excerpt TEXT NOT NULL CHECK(length(excerpt)<=500));
    CREATE TABLE IF NOT EXISTS income_claims (id INTEGER PRIMARY KEY, experiment_id INTEGER NOT NULL REFERENCES income_experiments(id),
      text TEXT NOT NULL, type TEXT NOT NULL CHECK(type IN ('fact','hypothesis','estimate')),
      source_id INTEGER REFERENCES income_sources(id), quote TEXT,
      status TEXT NOT NULL CHECK(status IN ('unverified','verified','refuted')),
      confidence REAL, verified_at TEXT, basis TEXT, test TEXT, kill_criterion TEXT, flag TEXT);
    CREATE TABLE IF NOT EXISTS income_assets (id INTEGER PRIMARY KEY, experiment_id INTEGER NOT NULL REFERENCES income_experiments(id),
      kind TEXT NOT NULL CHECK(kind IN ('sample','listing','promo','fulfillment','inventory')),
      path TEXT NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS income_events (id INTEGER PRIMARY KEY, experiment_id INTEGER NOT NULL REFERENCES income_experiments(id),
      ts TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('traffic','inquiry','purchase','expense','refund')),
      amount_cents INTEGER, count INTEGER, source TEXT, note TEXT);
    CREATE TABLE IF NOT EXISTS income_decisions (id INTEGER PRIMARY KEY, experiment_id INTEGER NOT NULL REFERENCES income_experiments(id),
      ts TEXT NOT NULL, verdict TEXT NOT NULL, evidence_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS workday_accounts (tenant TEXT PRIMARY KEY, created_at TEXT, verified_at TEXT, status TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS workday_tenants (company TEXT PRIMARY KEY, host TEXT, tenant TEXT, site TEXT, checked_at TEXT NOT NULL);
    ''')
    for column in ('category', 'status'):
        if column not in columns and 'answer_store' in existing:
            conn.execute(f"ALTER TABLE answer_store ADD COLUMN {column} TEXT DEFAULT '{'other' if column == 'category' else 'pending'}'")
    if 'field_results' in existing and 'review_status' not in {r[1] for r in conn.execute('PRAGMA table_info(field_results)')}:
        conn.execute('ALTER TABLE field_results ADD COLUMN review_status TEXT')
    conn.commit()

def create_attempt(conn, job_url, note='', profile_sha256=None, resume_sha256=None):
    t = now()
    cur = conn.execute('INSERT INTO attempts(job_url,state,created,updated,profile_sha256,resume_sha256,note) VALUES (?,?,?,?,?,?,?)',
                       (job_url, 'new', t, t, profile_sha256, resume_sha256, note))
    conn.commit()
    return cur.lastrowid

def schedule_followup(conn, attempt_id, days=10):
    if not isinstance(days, int) or days < 0:
        raise ValueError('followup_days must be a nonnegative integer')
    row = conn.execute('SELECT job_url,updated FROM attempts WHERE id=? AND state=\'confirmed\'', (attempt_id,)).fetchone()
    if not row or conn.execute('SELECT 1 FROM responses WHERE job_url=? LIMIT 1', (row[0],)).fetchone():
        return
    if conn.execute('SELECT count(*) FROM followups WHERE job_url=?', (row[0],)).fetchone()[0] >= 2:
        return
    due = (dt.datetime.fromisoformat(row[1]).date() + dt.timedelta(days=days)).isoformat()
    conn.execute('INSERT OR IGNORE INTO followups(job_url,attempt_id,due,kind,status,ts) VALUES (?,?,?,?,?,?)',
                 (row[0], attempt_id, due, 'application', 'due', now()))
    conn.commit()

def set_state(conn, attempt_id, new, followup_days=10):
    row = conn.execute('SELECT state FROM attempts WHERE id=?', (attempt_id,)).fetchone()
    if not row or new not in TRANSITIONS[row[0]]:
        raise ValueError(f'illegal attempt transition: {row[0] if row else None} -> {new}')
    conn.execute('UPDATE attempts SET state=?, updated=? WHERE id=?', (new, now(), attempt_id))
    conn.commit()
    if new == 'confirmed':
        schedule_followup(conn, attempt_id, followup_days)

def reconcile(conn, attempt_id, confirmed, followup_days=10):
    row = conn.execute('SELECT state FROM attempts WHERE id=?', (attempt_id,)).fetchone()
    if not row or row[0] != 'uncertain':
        raise ValueError('only uncertain attempts may be reconciled')
    conn.execute('UPDATE attempts SET state=?, updated=? WHERE id=?', ('confirmed' if confirmed else 'abandoned', now(), attempt_id))
    conn.commit()
    if confirmed:
        schedule_followup(conn, attempt_id, followup_days)

# --- duplicate-application identity -------------------------------------
# The same role reaches us under different URLs (job boards vs careers pages,
# tracking parameters, reposts). begin_submit refuses a submission when any of
# these identities already has a submitted/uncertain application.
SUBMITTED_STATES = ('submitting', 'confirmed', 'uncertain')
SUBMITTED_JOB_STATUSES = ('applied', 'unconfirmed')
DUPLICATE_WINDOW_DAYS = 90
_TRACKING = re.compile(r'^(utm_.*|gh_src|src|source|ref|referrer|refid|trk|gclid|fbclid|mc_cid|mc_eid|lever-source|lever-origin)$', re.I)
_COMPANY_SUFFIX = re.compile(r'\b(the|inc|llc|ltd|limited|gmbh|corp|corporation|co|company|plc|ag|sa|bv|labs?)\b')
_TITLE_NOISE = re.compile(r'\b(remote|hybrid|onsite|on site)\b')

def normalize_url(url):
    """Canonical URL: lowercase scheme/host, no fragment, no tracking params, sorted query, no trailing slash."""
    parts = urlsplit((url or '').strip())
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip('/'), urlencode(query), ''))

def _words(text):
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', (text or '').lower()).split())

def normalize_company(name):
    return ' '.join(_COMPANY_SUFFIX.sub(' ', _words(name)).split())

def normalize_title(title):
    return ' '.join(_TITLE_NOISE.sub(' ', _words(title)).split())

def _job_columns(conn):
    try:
        return {r[1] for r in conn.execute('PRAGMA table_info(jobs)')}
    except sqlite3.Error:
        return set()

def find_duplicate(conn, job_url, today=None):
    """Return (other_url, reason) if this job was already submitted under another identity, else None."""
    today = today or dt.date.today()
    cutoff = (today - dt.timedelta(days=DUPLICATE_WINDOW_DAYS)).isoformat()
    target = normalize_url(job_url)
    marks = ','.join('?' * len(SUBMITTED_STATES))
    submitted = {}
    for url, updated in conn.execute(f'SELECT job_url, max(updated) FROM attempts WHERE state IN ({marks}) GROUP BY job_url', SUBMITTED_STATES):
        submitted[url] = (updated or '')[:10]
    cols = _job_columns(conn)
    if 'status' in cols:
        when = 'applied_at' if 'applied_at' in cols else "''"
        jmarks = ','.join('?' * len(SUBMITTED_JOB_STATUSES))
        for url, applied in conn.execute(f'SELECT url, {when} FROM jobs WHERE status IN ({jmarks})', SUBMITTED_JOB_STATUSES):
            submitted.setdefault(url, (applied or '')[:10])
    submitted.pop(job_url, None)
    for url in submitted:
        if normalize_url(url) == target:
            return url, 'same posting under a different URL'
    if not {'company', 'title'} <= cols or not submitted:
        return None
    has_id = {'job_id', 'source'} <= cols
    fields = 'company, title' + (', job_id, source' if has_id else '')
    me = conn.execute(f'SELECT {fields} FROM jobs WHERE url=?', (job_url,)).fetchone()
    if not me:
        return None
    company, title = normalize_company(me[0]), normalize_title(me[1])
    my_id = (me[2] or '').strip() if has_id else ''
    my_source = (me[3] or '').strip().lower() if has_id else ''
    marks = ','.join('?' * len(submitted))
    for row in conn.execute(f'SELECT url, {fields} FROM jobs WHERE url IN ({marks})', list(submitted)):
        url = row[0]
        if has_id and my_id and (row[3] or '').strip() == my_id and (row[4] or '').strip().lower() == my_source:
            return url, f'same {my_source or "ATS"} job id {my_id}'
        recent = not submitted[url] or submitted[url] >= cutoff
        if recent and company and title and normalize_company(row[1]) == company and normalize_title(row[2]) == title:
            return url, f'same company and title within {DUPLICATE_WINDOW_DAYS} days'
    return None

def begin_submit(conn, job_url):
    conn.execute('BEGIN IMMEDIATE')
    try:
        if conn.execute("SELECT 1 FROM attempts WHERE job_url=? AND state IN ('submitting','confirmed','uncertain') LIMIT 1", (job_url,)).fetchone():
            raise DuplicateSubmission(job_url)
        duplicate = find_duplicate(conn, job_url)
        if duplicate:
            raise DuplicateSubmission(f'{job_url} already applied as {duplicate[0]} ({duplicate[1]})')
        row = conn.execute("SELECT id FROM attempts WHERE job_url=? AND state='ready_for_review' ORDER BY id DESC LIMIT 1", (job_url,)).fetchone()
        if not row:
            raise ValueError('no ready attempt')
        conn.execute("UPDATE attempts SET state='submitting', updated=? WHERE id=?", (now(), row[0]))
        conn.commit()
        return row[0]
    except Exception:
        conn.rollback()
        raise

def recover(conn):
    conn.execute("UPDATE attempts SET state='uncertain', note='crash/restart during submit', updated=? WHERE state='submitting'", (now(),))
    conn.commit()

def record_field(conn, attempt_id, key, label, required, kind, status, value='', error='', source=''):
    sensitive = bool(SENSITIVE.search(label or ''))
    if sensitive and error:
        error = 'field fill failed'
    value = str(value or '')
    digest = hashlib.sha256(value.encode()).hexdigest() if value else None
    preview = value if value and not sensitive and len(value) <= 80 else None
    conn.execute('''INSERT INTO field_results(attempt_id,field_key,label,required,kind,status,error,sensitive,value_sha256,value_preview,source,ts,review_status)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                 (attempt_id, str(key), label, int(bool(required)), kind, status, error or None, int(sensitive), digest, preview, source, now(),
                  'needs_review' if source in ('draft','cover_letter') and status == 'filled' else None))
    if required and status in ('failed', 'unanswered'):
        conn.execute('INSERT INTO blockers(attempt_id,label,field_key,kind,status,resolved_by,ts) VALUES (?,?,?,?,?,?,?)',
                     (attempt_id, label, str(key), kind, 'open', None, now()))
    conn.commit()

def open_blockers(conn, job_url=None):
    sql = "SELECT b.id,a.job_url,b.label,b.field_key,b.kind FROM blockers b JOIN attempts a ON a.id=b.attempt_id WHERE b.status='open'"
    return conn.execute(sql + (' AND a.job_url=?' if job_url else ''), (job_url,) if job_url else ()).fetchall()

