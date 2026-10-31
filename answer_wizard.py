"""Terminal review of saved answers. No browser or network work."""
import json
import re
import sys
from pathlib import Path

import answer_store

ORDER = ('profile_fact', 'other', 'free_text', 'attestation', 'consent', 'eeo', 'salary')
WHY = {
    'profile_fact': 'This is a basic fact that will be sent on applications.',
    'other': 'This answer will be sent when a form asks a matching question.',
    'free_text': 'This written answer will be sent to employers.',
    'attestation': 'This is a legal statement to the employer.',
    'consent': 'This gives or declines permission requested by the employer.',
    'eeo': 'This is optional self-identification information.',
    'salary': 'This states your compensation answer to the employer.',
}
SENTINELS = {'__decline__': 'Decline to self-identify', '__agree__': 'Agree'}


def description(question, pattern, example=''):
    raw = (question or '').strip()
    if not raw or raw == (pattern or '').strip():
        raw = re.sub(r'\\[bBsSwWdD]', ' ', pattern or '')
        raw = re.sub(r'[\^$|?*+()\[\]{}]', ' ', raw)
        raw = re.sub(r'\\.', ' ', raw)
        raw = re.sub(r'\s+', ' ', raw).strip()
    raw = raw[:100] or 'this question'
    if example and example.casefold() != raw.casefold():
        return f'{raw} (for example: {example[:120]})'
    return raw


def shown_answer(category, answer):
    if category == 'eeo':
        return SENTINELS.get(answer, 'EEO answer selected for this question')
    return answer


def ask_choice(read=input, write=print):
    while True:
        choice = read('[y] approve  [e] edit  [n] reject  [s] skip  [q] quit: ').strip().lower()
        if choice in ('y', 'e', 'n', 's', 'q'):
            return choice
        write('Choose y, e, n, s, or q. Nothing is approved by default.')


def ask_approval(category, read=input, write=print):
    if category not in answer_store.RESTRICTED:
        return True
    return read('Type yes to approve this answer: ').strip().lower() == 'yes'


def ask_standing(read=input):
    return read('Apply this decision to all future questions in this category for 180 days? [y/N] ').strip().lower() == 'y'


def history(conn, reports_dir):
    """Map labels to distinct real forms; include the newest doctor report."""
    labels = []
    for url, label in conn.execute('''SELECT DISTINCT a.job_url,f.label FROM field_results f
                                      JOIN attempts a ON a.id=f.attempt_id WHERE f.label IS NOT NULL'''):
        labels.append((url, label))
    paths = sorted(Path(reports_dir).glob('doctor_*.json')) if Path(reports_dir).exists() else []
    report = None
    if paths:
        try:
            report = json.loads(paths[-1].read_text())
            for job in report.get('jobs', []):
                for field in job.get('fields', []):
                    if field.get('label'):
                        labels.append((job.get('url', ''), field['label']))
        except (OSError, ValueError, TypeError):
            report = None
    return labels, report


def usage(pattern, labels):
    try:
        matches = [(url, label) for url, label in labels if re.search(pattern, label, re.I)]
    except (re.error, TypeError):
        return 0, ''
    return len({url for url, _ in matches if url}), matches[0][1] if matches else ''


def still_blocked(conn, report):
    if not report:
        return []
    remaining = []
    for job in report.get('jobs', []):
        company = job.get('company') or ''
        for field in job.get('fields', []):
            if not field.get('blocker'):
                continue
            outcome = field.get('resolver_outcome') or ''
            if outcome.startswith(('pending_answer:', 'stale_answer:')):
                value, _, blocker, _ = answer_store.resolve(conn, field.get('label') or '', company)
                if value and not blocker:
                    continue
            remaining.append((company, job.get('url') or 'unknown form'))
            break
    return list(dict.fromkeys(remaining))


def reopen(conn, ids):
    if not ids:
        return 0
    rows = conn.execute(f"SELECT id FROM answer_store WHERE id IN ({','.join('?' * len(ids))}) AND status IN ('approved','rejected')", tuple(ids)).fetchall()
    for (ident,) in rows:
        conn.execute("UPDATE answer_store SET status='pending',approved=0,verified_at=NULL,review_by=NULL,scope=CASE WHEN scope LIKE '{%}' THEN '*' ELSE scope END WHERE id=?", (ident,))
    conn.commit()
    return len(rows)


def run(conn, category=None, reports_dir='reports', read=input, write=print, interactive=None):
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        write('answers wizard needs an interactive terminal; run it from a terminal.')
        return 1
    query = "SELECT id,category,question_text,match_regex,answer FROM answer_store WHERE status='pending'"
    params = ()
    if category:
        query += ' AND category=?'
        params = (category,)
    rows = conn.execute(query, params).fetchall()
    catalog_path = Path(__file__).with_name('answer_catalog.json')
    catalog_help = {item['match']: item['help'] for item in json.loads(catalog_path.read_text()).get('questions', [])} if catalog_path.exists() else {}
    rows.sort(key=lambda row: (ORDER.index(row[1]) if row[1] in ORDER else len(ORDER), row[0]))
    labels, report = history(conn, reports_dir)
    counts = {'approved': 0, 'rejected': 0, 'skipped': 0}
    total = len(rows)
    for number, (ident, cat, question, pattern, answer) in enumerate(rows, 1):
        used, example = usage(pattern, labels)
        def display(value):
            write(f'\n{number} of {total} | answer {ident} | {cat}')
            write(f'When an employer asks: {description(question, pattern, example)}')
            write(f'Answer sent: {shown_answer(cat, value)}')
            if labels:
                write(f'Real forms asking this: {used}')
            write(f'Why it matters: {catalog_help.get(pattern, WHY.get(cat, WHY["other"]))}')
        display(answer)
        try:
            while True:
                choice = ask_choice(read, write)
                if choice == 'q':
                    break
                if choice == 's':
                    counts['skipped'] += 1
                    break
                if choice == 'n':
                    conn.execute("UPDATE answer_store SET approved=0,status='rejected',verified_at=NULL,review_by=NULL WHERE id=?", (ident,))
                    conn.commit()
                    counts['rejected'] += 1
                    break
                proposed = answer
                if choice == 'e':
                    proposed = read('New answer text: ')
                    if not proposed.strip():
                        write('Answer cannot be empty; no change saved.')
                        continue
                    display(proposed)
                    if read('Approve this edited answer? [y/N] ').strip().lower() != 'y':
                        write('Edit discarded.')
                        continue
                if not proposed.strip():
                    write('Enter an answer with [e] before approving.')
                    continue
                if not ask_approval(cat, read, write):
                    write('Approval not given; answer remains pending.')
                    continue
                if proposed != answer:
                    conn.execute('UPDATE answer_store SET answer=? WHERE id=?', (proposed, ident))
                answer_store.approve(conn, [ident])
                counts['approved'] += 1
                if cat in answer_store.RESTRICTED and ask_standing(read):
                    answer_store.grant_standing(conn, cat)
                break
            if choice == 'q':
                write('Progress saved.')
                break
        except EOFError:
            write('Input ended. Progress saved; remaining answers are pending.')
            break
    write(f"Summary: {counts['approved']} approved, {counts['rejected']} rejected, {counts['skipped']} skipped")
    blocked = still_blocked(conn, report)
    write('Forms in the last doctor report still blocked:')
    for company, url in blocked:
        write(f'  {company} | {url}')
    if not blocked:
        write('  none')
    return 0
