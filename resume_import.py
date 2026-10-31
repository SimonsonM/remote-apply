"""Deterministic resume extraction and explicit fact review."""
import datetime as dt
import hashlib
import re
from pathlib import Path

import state

EXP_SECTIONS = {'experience', 'work experience', 'professional experience', 'selected independent technical projects',
                'independent technical projects', 'independent projects', 'projects', 'additional experience'}
EDU_SECTIONS = {'education', 'education and professional development'}
HEADINGS = EXP_SECTIONS | EDU_SECTIONS | {'certifications', 'certificates', 'skills'}
CREDENTIAL = re.compile(r',?\s*\b(?:MBA|Ph\.?D\.?|M\.?S\.?|B\.?S\.?|CPA|PMP|CISSP)\.?\s*$', re.I)
EMAIL = re.compile(r'(?<![\w.-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}')
PHONE = re.compile(r'(?<!\w)(?:\+?\d{1,2}[ .-]?)?(?:\(?\d{3}\)?[ .-]?)\d{3}[ .-]?\d{4}(?!\w)')
URL = re.compile(r'https?://[^\s)]+|(?:linkedin\.com|github\.com)/[^\s)]+', re.I)
DATE = r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)[ .]+\d{4}|\d{4}'
ROLE_A = re.compile(rf'^(.+?),\s*(.+?),?\s+({DATE})\s*[-–—]\s*({DATE}|Present|Current)$', re.I)
ROLE_B = re.compile(r'^(.+?)\s+[-–—]\s+(.+?)\s*\((\d{4})\s*[-–—]\s*(\d{4}|Present|Current)\)$', re.I)
LOCATION = re.compile(r'(?<![\w,])([A-Za-z][A-Za-z .-]*),\s*([A-Z]{2}|[A-Za-z][A-Za-z ]+?)(?:,?\s+(\d{5}(?:-\d{4})?))?(?=\s*(?:\||$))')


def read_resume(path):
    path = Path(path).expanduser().resolve()
    suffix = path.suffix.lower()
    if suffix not in ('.docx', '.pdf', '.md', '.txt', '.rtf'):
        raise ValueError('Resume must be .docx, .pdf, .md, .txt, or .rtf')
    if suffix in ('.md', '.txt', '.rtf'):
        return path.read_text(encoding='utf-8')
    try:
        from markitdown import MarkItDown
        result = MarkItDown().convert(str(path)).text_content
        if result.strip():
            return result
    except Exception:
        pass
    if suffix == '.docx':
        from docx import Document
        return '\n'.join(p.text for p in Document(path).paragraphs)
    from pdfminer.high_level import extract_text
    return extract_text(str(path))


def parse_text(text):
    """Return fact candidates with stable line numbers; extraction never approves a fact."""
    facts, section, seen, current_role = [], '', set(), ''
    lines = text.splitlines()
    def add(key, value, line, confidence='medium'):
        value = re.sub(r'\s+', ' ', value).strip(' ,;')
        if value and (key, value) not in seen:
            facts.append({'key':key, 'value':value, 'line':line, 'confidence':confidence})
            seen.add((key,value))
    clean = [re.sub(r'^#+\s*', '', raw).replace('**', '').strip().strip('*').strip() for raw in lines]
    first = next(((i, line) for i,line in enumerate(clean,1) if line), None)
    if first and not EMAIL.search(first[1]) and not PHONE.search(first[1]):
        name = CREDENTIAL.sub('', first[1]).strip(' ,')
        add('name', name.title() if name.isupper() else name, first[0], 'medium')
    header_date = re.compile(rf'^({DATE})\s*[-–—]\s*({DATE}|Present|Current)\b', re.I)
    consumed = set()
    for i, raw in enumerate(lines,1):
        if i in consumed:
            continue
        line = clean[i-1]
        low = line.lower().rstrip(':')
        if low in HEADINGS:
            section = 'experience' if low in EXP_SECTIONS else 'education' if low in EDU_SECTIONS else low
            current_role = ''
            continue
        # Two-line role header: "Title | Company" followed by "Dates | Location".
        if section == 'experience' and ' | ' in line and not re.search(r'\b(?:19|20)\d{2}\b', line):
            j = next((k for k in range(i+1, len(lines)+1) if clean[k-1]), None)
            m = header_date.match(clean[j-1]) if j else None
            if m:
                title, company = (s.strip() for s in line.split('|', 1))
                current_role = f'{title} | {company} | {m[1]} - {m[2]}'
                add('role', current_role, i, 'medium')
                consumed.add(j)
                continue
        for m in EMAIL.finditer(line): add('email', m.group(), i, 'high')
        for m in PHONE.finditer(line): add('phone', m.group(), i, 'medium')
        for m in URL.finditer(line):
            url=m.group().rstrip('.,;')
            key='linkedin' if 'linkedin.com' in url.lower() else 'github' if 'github.com' in url.lower() else 'website'
            add(key, url if url.startswith('http') else 'https://'+url, i, 'high')
        if i <= 8 and not (first and i == first[0]):
            for part in line.split('|'):
                match = LOCATION.search(part.strip())
                if match:
                    city, region, postal = match.groups()
                    add('location', match.group().strip(), i, 'medium')
                    add('city', city, i, 'medium')
                    add('state', region, i, 'medium')
                    if postal: add('zip', postal, i, 'medium')
        if re.search(r'\b(?:location|based in)\s*:', line, re.I):
            add('location', line.split(':',1)[-1], i, 'medium')
        elif re.fullmatch(r'[A-Za-z .-]+,\s*[A-Z]{2}(?:\s+\d{5})?', line):
            add('location', line, i, 'low')
        a=ROLE_A.match(line)
        b=ROLE_B.match(line)
        if a:
            current_role=f'{a[1]} | {a[2]} | {a[3]} - {a[4]}'
            add('role', current_role, i, 'medium')
        elif b:
            current_role=f'{b[2]} | {b[1]} | {b[3]} - {b[4]}'
            add('role', current_role, i, 'medium')
        elif section == 'experience' and re.search(r'\b(?:19|20)\d{2}\b', line) and re.search(r'[-–—]', line):
            current_role=line
            add('role', line, i, 'low')
        elif section == 'experience' and current_role and line and (re.match(r'^[-•*]\s+', line) or len(line) > 40):
            add('role_bullet', f'{current_role}: {line.lstrip("-•* ")}', i, 'low')
        elif section == 'education' and line and not line.startswith(('-', '•')):
            add('education', line, i, 'low')
        elif section in ('certifications','certificates') and line:
            add('certification', line.lstrip('-• '), i, 'low')
        elif section == 'skills' and line:
            add('skill', line.lstrip('-• '), i, 'low')
        if re.search(r'\bSCADA\b', line, re.I):
            add('skill', 'SCADA experience', i, 'low')
    return facts


def approve_fact(conn, ident, value=None):
    row=conn.execute('SELECT value FROM profile_facts WHERE id=?', (ident,)).fetchone()
    if not row: raise ValueError('Unknown fact')
    now=dt.datetime.now(dt.timezone.utc)
    conn.execute("UPDATE profile_facts SET value=?,approved=1,status='approved',verified_at=?,review_by=? WHERE id=?",
                 (value if value is not None else row[0], now.isoformat(), (now+dt.timedelta(days=365)).isoformat(), ident))
    conn.commit()


def import_resume(conn, path, read=input, write=print, decisions=None):
    path=Path(path).expanduser().resolve()
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    candidates=parse_text(read_resume(path))
    existing=list(conn.execute("SELECT id,key,value,resume_sha256,status FROM profile_facts WHERE source LIKE 'resume:%'"))
    incoming={(f['key'],f['value']) for f in candidates}
    for ident,key,value,sha,status in existing:
        if sha != digest and (key,value) not in incoming and status == 'approved':
            conn.execute("UPDATE profile_facts SET status='stale',approved=0 WHERE id=?",(ident,))
    counts={'approved':0,'proposed':0,'dropped':0}
    bullets = {}
    for f in candidates:
        if f['key'] == 'role_bullet':
            bullets.setdefault(f['value'].split(': ', 1)[0], []).append(f)
    for f in candidates:
        key,value=f['key'],f['value']
        if key == 'role_bullet':
            continue
        notes_need_review = key == 'role' and value in bullets and any(
            not conn.execute('SELECT 1 FROM profile_facts WHERE key=? AND value=? AND resume_sha256=?',
                             ('role_bullet', bullet['value'], digest)).fetchone()
            for bullet in bullets[value])
        if any(k==key and v==value and s=='approved' for _,k,v,_,s in existing) and not notes_need_review:
            continue
        source=f'resume:{digest[:12]}:line {f["line"]}'
        prior=conn.execute('SELECT id,status FROM profile_facts WHERE key=? AND value=? AND resume_sha256=?',(key,value,digest)).fetchone()
        if prior and prior[1] in ('approved','dropped') and not notes_need_review:
            continue
        if decisions is None:
            write(f'{key}: {value} [{f["confidence"]}; {source}]')
            choice=''
            for attempt in range(3):
                choice=read('Confirm [y], edit [e], drop [n] (default n): ').strip().lower() or 'n'
                if choice in ('y','n','e'): break
                write('Choose y, n, or e.')
            else:
                write('Progress saved. Run ./ra and choose 1 to continue.')
                raise EOFError
            edited=read('Correct value: ').strip() if choice=='e' else ''
            if choice=='e' and edited:
                value=edited
                choice='y'
            elif choice=='e':
                choice='n'
        else:
            entry=decisions.get(f'{key}:{f["line"]}', decisions.get(key, 'n'))
            choice=entry if isinstance(entry,str) else entry.get('action','n')
            if isinstance(entry,dict) and choice=='e':
                value=entry.get('value','').strip()
                choice='y' if value else 'n'
        status='approved' if choice=='y' else 'dropped' if choice=='n' else 'proposed'
        if prior:
            ident=prior[0]
            conn.execute('UPDATE profile_facts SET value=?,source=?,status=?,approved=0 WHERE id=?',(value,source,status,ident))
        else:
            ident=conn.execute('INSERT INTO profile_facts(key,value,source,approved,resume_sha256,status) VALUES (?,?,?,0,?,?)',(key,value,source,digest,status)).lastrowid
        if status=='approved': approve_fact(conn,ident)
        conn.commit()
        counts[status]+=1
        if key == 'role' and status == 'approved' and f['value'] in bullets:
            grouped = bullets[f['value']]
            if decisions is None:
                write('Achievement notes:')
                for bullet in grouped: write(f"- {bullet['value'].split(': ', 1)[-1]}")
                for attempt in range(3):
                    keep=read('Keep these as achievement notes? [y/n/e] (default n): ').strip().lower() or 'n'
                    if keep in ('y','n','e'): break
                    write('Choose y, n, or e.')
                else:
                    raise EOFError
                edited_notes=read('Correct achievement notes: ').strip() if keep=='e' else ''
            else:
                entry=decisions.get('role_bullet','n')
                keep=entry if isinstance(entry,str) else entry.get('action','n')
                edited_notes=entry.get('value','') if isinstance(entry,dict) else ''
            for bullet in grouped:
                note=edited_notes if keep=='e' and edited_notes else bullet['value']
                note_status='approved' if keep=='y' or (keep=='e' and edited_notes) else 'dropped'
                prior_note=conn.execute('SELECT id FROM profile_facts WHERE key=? AND value=? AND resume_sha256=?',('role_bullet',bullet['value'],digest)).fetchone()
                if prior_note: continue
                note_id=conn.execute('INSERT INTO profile_facts(key,value,source,approved,resume_sha256,status) VALUES (?,?,?,0,?,?)',('role_bullet',note,f'resume:{digest[:12]}:line {bullet["line"]}',digest,note_status)).lastrowid
                if note_status=='approved': approve_fact(conn,note_id)
                conn.commit()
                counts[note_status]+=1
    conn.commit()
    return digest,counts


def approved_facts(conn):
    now=state.now()
    return [(k,v) for k,v in conn.execute("SELECT key,value FROM profile_facts WHERE approved=1 AND status='approved' AND (review_by IS NULL OR review_by>?) ORDER BY id",(now,))]
