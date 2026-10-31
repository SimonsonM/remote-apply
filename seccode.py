"""Fetch Greenhouse application security codes from the local Proton Bridge IMAP (read-only).
Scope: only messages from Greenhouse's no-reply sender, subject naming the company, received after the submit."""
import email, imaplib, os, re, ssl, time
import datetime as dt
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path

SENDER = 'no-reply@us.greenhouse-mail.io'
CODE_RE = re.compile(r'security code field on your application:\s*([A-Za-z0-9]{6,12})\b')


def _env():
    env = dict(os.environ)
    f = Path(__file__).with_name('.env')
    if f.exists():
        for line in f.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k, v = line.split('=', 1)
                env.setdefault(k.strip(), v.strip())
    return env


def configured():
    e = _env()
    return all(e.get(k) for k in ('RA_IMAP_HOST', 'RA_IMAP_PORT', 'RA_IMAP_USER', 'RA_IMAP_PASS'))


def _connect():
    e = _env()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False  # Bridge uses a self-signed cert on localhost only
    ctx.verify_mode = ssl.CERT_NONE
    m = imaplib.IMAP4(e['RA_IMAP_HOST'], int(e['RA_IMAP_PORT']))
    m.starttls(ssl_context=ctx)
    m.login(e['RA_IMAP_USER'], e['RA_IMAP_PASS'])
    m.select('INBOX', readonly=True)
    return m


def _subject(msg):
    return ''.join(t.decode(c or 'utf8', 'ignore') if isinstance(t, bytes) else t for t, c in decode_header(msg['Subject'] or ''))


def _text(msg):
    out = ''
    for part in msg.walk():
        if part.get_content_type() in ('text/plain', 'text/html'):
            out += (part.get_payload(decode=True) or b'').decode(part.get_content_charset() or 'utf8', 'ignore')
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', out))


def _norm(s):
    return re.sub(r'[^a-z0-9]', '', s.lower())


def find_code(company, since, exclude=()):
    """Newest code for `company` received at/after `since` (aware datetime), or None."""
    m = _connect()
    try:
        day = (since - dt.timedelta(days=1)).strftime('%d-%b-%Y')
        typ, data = m.search(None, 'SINCE', day, 'FROM', f'"{SENDER}"')
        best = None
        for ident in reversed(data[0].split()[-30:]):
            typ, raw = m.fetch(ident, '(BODY.PEEK[])')
            msg = email.message_from_bytes(raw[0][1])
            if SENDER not in (msg['From'] or '').lower():
                continue
            subj = _subject(msg)
            if 'security code' not in subj.lower() or _norm(company) not in _norm(subj):
                continue
            try:
                when = parsedate_to_datetime(msg['Date'])
            except Exception:
                continue
            if when < since - dt.timedelta(seconds=20):
                continue
            hit = CODE_RE.search(_text(msg))
            if hit and hit.group(1) not in exclude and (best is None or when > best[0]):
                best = (when, hit.group(1))
        return best[1] if best else None
    finally:
        try:
            m.logout()
        except Exception:
            pass


def wait_for_code(company, since, timeout=180, poll=6, exclude=()):
    end = time.time() + timeout
    while time.time() < end:
        try:
            code = find_code(company, since, exclude)
            if code:
                return code
        except Exception:
            pass
        time.sleep(poll)
    return None
