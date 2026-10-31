"""Verified answers and time-bounded standing authorization."""
import datetime as dt
import json
import re
import state

RESTRICTED = {'eeo', 'consent', 'attestation', 'salary'}
PATTERNS = (
    ('eeo', r'gender|race|ethnic|hispanic|veteran|disabilit|orientation|pronoun'),
    ('attestation', r'authori[sz](?:ed|ation)|eligible|right to work|permitted to work|permission to work|legally|sponsor|visa|non[ -]?compete|employment agreement|restrictive covenant|non[ -]?solicit|work in (?:canada|the uk|[a-z]+)|relocat|clearance|18\+|18 years|18 or older|\bage\b|sanction|government official|conflict of interest|criminal|convict|background[ -]?check|previously (?:employed|worked)|related to|family member employed'),
    ('consent', r'consent|privacy|agree|acknowledge|personal data'),
    ('salary', r'salary|compensation|pay'),
)

def category(label):
    for name, pattern in PATTERNS:
        if re.search(pattern, label, re.I):
            return name
    return 'profile_fact' if re.search(r'name|email|phone|address|location|city|website|linkedin|company|employer|title', label, re.I) else 'other'

def reclassify(conn):
    """Revisit saved questions; return IDs whose old approval needs review."""
    pending_ids = []
    for ident, question, pattern, old, approved, status in conn.execute(
            'SELECT id,question_text,match_regex,category,approved,status FROM answer_store'):
        new = category(question or pattern or '')
        if new == old:
            continue
        stricter = new in RESTRICTED
        if stricter and (approved or status == 'approved'):
            conn.execute("UPDATE answer_store SET category=?,approved=0,status='pending' WHERE id=?", (new, ident))
            pending_ids.append(ident)
        else:
            conn.execute('UPDATE answer_store SET category=? WHERE id=?', (new, ident))
    conn.commit()
    return pending_ids

def import_bank(conn, path):
    count = 0
    for item in json.loads(path.read_text()).get('answers', []):
        pattern, answer = item.get('match'), item.get('answer')
        if not pattern or not answer or conn.execute('SELECT 1 FROM answer_store WHERE match_regex=? AND answer=?', (pattern, answer)).fetchone():
            continue
        conn.execute('''INSERT INTO answer_store(match_regex,question_text,answer,sensitive,source,approved,scope,ts,category,status)
                        VALUES (?,?,?,?,?,0,?,?,?,'pending')''',
                     (pattern, pattern, answer, int(bool(state.SENSITIVE.search(pattern))), 'answers.json', '*', state.now(), category(pattern)))
        count += 1
    conn.commit()
    return count

def grant_standing(conn, selected_category):
    if selected_category not in RESTRICTED:
        raise ValueError('standing authorization requires a restricted category')
    now = dt.datetime.now(dt.timezone.utc)
    conn.execute('INSERT INTO standing_authorizations(category,scope,approved_at,expires) VALUES (?,?,?,?)',
                 (selected_category, json.dumps({'category': selected_category, 'approved_at': now.isoformat()}),
                  now.isoformat(), (now + dt.timedelta(days=180)).isoformat()))
    conn.commit()


def approve(conn, ids=(), selected_category=None, confirm=False, review_days=None):
    if selected_category in RESTRICTED and not confirm:
        raise ValueError('restricted category requires --i-confirm-standing-authorization or explicit IDs')
    if selected_category and confirm and selected_category in RESTRICTED:
        grant_standing(conn, selected_category)
    if selected_category:
        rows = conn.execute("SELECT id,category FROM answer_store WHERE category=? AND status='pending'", (selected_category,)).fetchall()
    else:
        rows = conn.execute(f"SELECT id,category FROM answer_store WHERE id IN ({','.join('?' for _ in ids)}) AND status!='rejected'", tuple(ids)).fetchall() if ids else []
    for ident, cat in rows:
        days = review_days if review_days is not None else (90 if cat == 'salary' else 365)
        now = dt.datetime.now(dt.timezone.utc)
        conn.execute("UPDATE answer_store SET approved=1,status='approved',verified_at=?,review_by=? WHERE id=?",
                     (now.isoformat(), (now + dt.timedelta(days=days)).isoformat(), ident))
        if cat in RESTRICTED and not selected_category:
            conn.execute("UPDATE answer_store SET scope=? WHERE id=?", (json.dumps({'explicit_id': True}), ident))
    conn.commit()
    return len(rows)

def resolve(conn, label, company):
    """Return (value, source, blocker_kind, row_id); never consult a raw answer bank.
    Rows in the question's own category win; a restricted question never borrows another category's answer."""
    want = category(label)
    hits = []
    for row in conn.execute(
            'SELECT id,match_regex,answer,scope,approved,status,review_by,category FROM answer_store ORDER BY id DESC'):
        try:
            if re.search(row[1], label, re.I):
                hits.append(row)
        except (re.error, TypeError):
            continue
    same = [r for r in hits if r[7] == want]
    hits = same if (same or want in RESTRICTED) else hits
    live = [r for r in hits if r[5] != 'rejected']
    hits = live or hits  # a rejected row must not shadow an older approved one
    for ident, pattern, answer, scope, approved, status, review_by, cat in hits:
        if scope and scope not in ('*', company) and not scope.startswith('{'):
            continue
        stale = status == 'expired' or (review_by and review_by <= state.now())
        if stale:
            return '', '', 'stale_answer', ident
        standing = conn.execute('SELECT 1 FROM standing_authorizations WHERE category=? AND expires>? LIMIT 1', (cat, state.now())).fetchone()
        explicit = scope and scope.startswith('{') and json.loads(scope).get('explicit_id')
        if not approved or status != 'approved' or (cat in RESTRICTED and not (explicit or standing)):
            return '', '', 'unapproved_answer', ident
        return answer.replace('{company}', company), f'answer_store:{ident}', '', ident
    return '', '', '', None
