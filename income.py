"""Local, evidence-first income experiments. Only fetch_source accesses the web."""
import datetime as dt
import hashlib
import html
from html.parser import HTMLParser
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

import policy
import smart
import state

UA = 'RemoteApplyIncomeResearch/1.0'
MAX_BYTES = 2 * 1024 * 1024
_LAST_REQUEST = {}
KINDS = ('traffic', 'inquiry', 'purchase', 'expense', 'refund')
SALARY_FLOOR_KEYS = ('part_time_remote_annual', 'full_time_quick_hire_annual')
CATALOG = [
    ('digital template pack', ('writing','design','spreadsheet','template'), 0, 3, False, False),
    ('fixed-scope audit/consulting package', ('audit','consulting','analysis'), 0, 4, False, False),
    ('data cleanup/dashboard service', ('data','spreadsheet','dashboard','python'), 0, 5, False, False),
    ('workflow-automation setup', ('automation','python','workflow'), 0, 6, False, False),
    ('niche newsletter/report', ('writing','research','newsletter'), 0, 5, False, True),
    ('micro-tool/SaaS', ('coding','python','software'), 10, 10, True, True),
    ('print-on-demand design', ('design','illustration'), 0, 3, False, False),
]


def private_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(text)
    os.chmod(path, 0o600)
    return path


def experiment(conn, ident):
    row = conn.execute('SELECT * FROM income_experiments WHERE id=?', (ident,)).fetchone()
    if row is None:
        raise ValueError(f'Unknown experiment: {ident}')
    return row


def new(conn, name, hypothesis, kill, min_days, budget_cap_cents=0):
    if not all(str(x).strip() for x in (name, hypothesis, kill)) or min_days < 0 or budget_cap_cents < 0:
        raise ValueError('Name, hypothesis, kill criterion, and nonnegative days/budget are required')
    stamp = state.now()
    cur = conn.execute('INSERT INTO income_experiments(name,hypothesis,status,created,updated,budget_cap_cents,kill_criteria,min_exposure_days) VALUES (?,?,?,?,?,?,?,?)',
                       (name, hypothesis, 'draft', stamp, stamp, budget_cap_cents, kill, min_days))
    conn.commit()
    return cur.lastrowid


class Cleaner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0
        self.title = []
        self.in_title = False
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript', 'template'):
            self.hidden += 1
        if tag == 'title': self.in_title = True
        if tag in ('p', 'br', 'div', 'li', 'h1', 'h2', 'h3'): self.parts.append(' ')
    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'template') and self.hidden:
            self.hidden -= 1
        if tag == 'title': self.in_title = False
    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)
            if self.in_title: self.title.append(data)


def _allowed_url(url, allow_private=False):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Only public HTTP(S) URLs without credentials are allowed')
    hostname = parsed.hostname.lower()
    if hostname == 'localhost' or hostname.endswith('.localhost'):
        if not allow_private: raise ValueError('Private hosts are refused')
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError('Host could not be resolved') from exc
    if not allow_private and (not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses)):
        raise ValueError('Private hosts are refused')
    return parsed


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _get(url, allow_private=False):
    parsed = _allowed_url(url, allow_private)
    host = parsed.hostname
    elapsed = time.monotonic() - _LAST_REQUEST.get(host, -1e9)
    if elapsed < 1: time.sleep(1 - elapsed)
    _LAST_REQUEST[host] = time.monotonic()
    request = urllib.request.Request(url, headers={'User-Agent': UA}, method='GET')
    opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))
    try:
        response = opener.open(request, timeout=15)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        status = response.status
        if status in (301, 302, 303, 307, 308):
            return status, response.headers.get('Location'), b''
        content = response.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES: raise ValueError('Source exceeds 2 MB cap')
        return status, None, content


def fetch_source(url, allow_private=False):
    """The sole public network entry point; GET, robots, redirect and size guarded."""
    for redirects in range(4):
        parsed = _allowed_url(url, allow_private)
        robots_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, '/robots.txt', '', ''))
        robots_status, _, robots_bytes = _get(robots_url, allow_private)
        if robots_status == 200:
            rules = urllib.robotparser.RobotFileParser()
            rules.parse(robots_bytes.decode('utf-8', 'replace').splitlines())
            if not rules.can_fetch(UA, url): raise ValueError('robots.txt disallows this URL')
        elif robots_status not in (404, 410):
            raise ValueError('Could not verify robots.txt permission')
        status, location, body = _get(url, allow_private)
        if location is not None:
            if redirects == 3: raise ValueError('Too many redirects')
            url = urllib.parse.urljoin(url, location)
            continue
        if status != 200: raise ValueError(f'HTTP {status}')
        cleaner = Cleaner()
        cleaner.feed(body.decode('utf-8', 'replace'))
        cleaned = re.sub(r'\s+', ' ', html.unescape(' '.join(cleaner.parts))).strip()
        title = re.sub(r'\s+', ' ', ' '.join(cleaner.title)).strip()
        return url, title, status, cleaned
    raise ValueError('Too many redirects')


def source(conn, root, ident, url, allow_private=False):
    experiment(conn, ident)
    final_url, title, status, cleaned = fetch_source(url, allow_private)
    digest = hashlib.sha256(cleaned.encode()).hexdigest()
    path = private_write(Path(root) / 'income' / 'sources' / f'{digest}.txt', cleaned)
    cur = conn.execute('INSERT INTO income_sources(experiment_id,url,title,retrieved_at,http_status,text_sha256,text_path,excerpt) VALUES (?,?,?,?,?,?,?,?)',
                       (ident, final_url, title, state.now(), status, digest, str(path), cleaned[:500]))
    conn.execute("UPDATE income_experiments SET status='researching',updated=? WHERE id=? AND status='draft'", (state.now(), ident))
    conn.commit()
    return cur.lastrowid


def _normalized(value):
    return re.sub(r'\s+', ' ', value).strip().casefold()


def claim(conn, ident, text, kind, source_id=None, quote=None, test=None, kill=None, basis=None):
    experiment(conn, ident)
    if not text.strip(): raise ValueError('Claim text required')
    if kind not in ('fact', 'hypothesis', 'estimate'): raise ValueError('Invalid claim type')
    verified = False
    flag = None
    if kind == 'fact':
        row = conn.execute('SELECT text_path,text_sha256 FROM income_sources WHERE id=? AND experiment_id=?', (source_id, ident)).fetchone()
        if row and quote and _normalized(quote) and Path(row[0]).is_file():
            stored = Path(row[0]).read_text(encoding='utf-8')
            verified = hashlib.sha256(stored.encode()).hexdigest() == row[1] and _normalized(quote) in _normalized(stored)
        if not verified:
            kind, flag = 'hypothesis', 'Unverified fact: source quote missing or unmatched'
            test = test or 'Verify this claim against a source quote'
            kill = kill or 'Remove claim if source cannot be verified'
    elif kind == 'hypothesis':
        if not test or not kill: raise ValueError('Hypothesis needs a test and kill criterion')
    else:
        if not basis or not basis.strip(): raise ValueError('Estimate needs a stated basis')
    if kind == 'hypothesis' and (not test or not kill): raise ValueError('Hypothesis needs a test and kill criterion')
    if re.search(r'\b\d+(?:[.,]\d+)*\b|\$\s*\d', text) and not verified:
        if not basis or not basis.strip(): raise ValueError('Unverified numbers need an estimate basis')
        kind, flag = 'estimate', flag or 'Unverified number; treated as estimate'
    cur = conn.execute('INSERT INTO income_claims(experiment_id,text,type,source_id,quote,status,confidence,verified_at,basis,test,kill_criterion,flag) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (ident, text, kind, source_id if verified else None, quote if verified else None,
                        'verified' if verified else 'unverified', 1.0 if verified else None, state.now() if verified else None, basis, test, kill, flag))
    conn.commit()
    return cur.lastrowid


def claims(conn, ident):
    experiment(conn, ident)
    return conn.execute('SELECT id,type,status,text,flag FROM income_claims WHERE experiment_id=? ORDER BY id', (ident,)).fetchall()


def _validate_salary_floors(data):
    floors = data.get('salary_floors', {})
    if not isinstance(floors, dict):
        raise ValueError('salary_floors must be an object')
    for key in SALARY_FLOOR_KEYS:
        value = floors.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or value <= 0):
            raise ValueError(f'salary_floors.{key} must be a positive number or null')
    return {key: floors[key] for key in SALARY_FLOOR_KEYS if key in floors}


def _validate_inventory(data):
    if not isinstance(data, dict): raise ValueError('Inventory must be an object')
    required = ('skills', 'assets', 'equipment', 'weekly_hours', 'budget', 'gpu_constraint', 'platforms')
    if any(key not in data for key in required): raise ValueError('Inventory lacks required fields')
    for key in ('weekly_hours', 'budget'):
        value = data[key]
        if value is None and key == 'weekly_hours': continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f'{key} must be a nonnegative number' + (' or null' if key == 'weekly_hours' else ''))
    for key in ('skills', 'assets', 'equipment', 'platforms'):
        if not isinstance(data[key], list): raise ValueError(f'{key} must be a list')
    for asset in data['assets']:
        if not isinstance(asset, dict) or not asset.get('name') or not asset.get('description'):
            raise ValueError('Assets need name and one-line description')
    floors = _validate_salary_floors(data)
    return {**data, **({'salary_floors': floors} if 'salary_floors' in data else {})}


def save_inventory(root, data):
    data = _validate_inventory(data)
    return private_write(Path(root) / 'income' / 'inventory.json', json.dumps(data, indent=2) + '\n')


def inventory(root, read=input):
    existing = load_inventory(root)
    def items(prompt): return [x.strip() for x in read(prompt).split(',') if x.strip()]
    assets = []
    while True:
        name = read('Existing project/asset name (blank to finish): ').strip()
        if not name: break
        assets.append({'name': name, 'description': read('One line description: ').strip()})
    data = {'skills':items('Skills, comma separated: '), 'assets':assets,
        'equipment':items('Equipment, comma separated: '), 'weekly_hours':float(read('Weekly hours: ') or 0),
        'budget':float(read('Budget (default 0): ') or 0), 'gpu_constraint':read('GPU constraint? yes/no: ').strip().lower() == 'yes',
        'platforms':items('Platforms with accounts, comma separated: ')}
    if 'salary_floors' in existing: data['salary_floors'] = existing['salary_floors']
    return save_inventory(root, data)


def load_inventory(root):
    path = Path(root) / 'income' / 'inventory.json'
    if not path.exists():
        return {'skills':[], 'assets':[], 'equipment':[], 'weekly_hours':0, 'budget':0, 'gpu_constraint':False, 'platforms':[]}
    try:
        return _validate_inventory(json.loads(path.read_text(encoding='utf-8')))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError(f'Invalid inventory JSON: {exc}') from exc


def score_job(root, minimum=None, maximum=None, unit='annual', employment_type='unknown', hours_per_week=None):
    """Return a soft floor score; uncertain ranges and missing data score zero."""
    if unit not in ('annual', 'hourly'): raise ValueError('unit must be annual or hourly')
    if employment_type not in ('part_time', 'full_time', 'unknown'):
        raise ValueError('type must be part_time, full_time, or unknown')
    for name, value in (('min', minimum), ('max', maximum), ('hours', hours_per_week)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or value <= 0):
            raise ValueError(f'{name} must be a positive number')
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError('min cannot exceed max')
    if hours_per_week is not None and hours_per_week > 168:
        raise ValueError('hours cannot exceed 168 per week')
    floor_key = {'part_time': 'part_time_remote_annual', 'full_time': 'full_time_quick_hire_annual'}.get(employment_type)
    floor = load_inventory(root).get('salary_floors', {}).get(floor_key)
    factor = 1
    note = ''
    if unit == 'hourly':
        if employment_type == 'full_time': factor = 2080
        elif employment_type == 'part_time' and hours_per_week is not None: factor = hours_per_week * 52
        elif employment_type == 'part_time' and (minimum is not None or maximum is not None):
            note = 'Part-time annualized pay is unknown: hourly pay was posted without hours per week.'
        elif employment_type == 'unknown' and (minimum is not None or maximum is not None):
            note = 'Annualized pay is unknown because employment type is unspecified.'
    annual_min = minimum * factor if minimum is not None and not note else None
    annual_max = maximum * factor if maximum is not None and not note else None
    if floor is None or floor_key is None or (annual_min is None and annual_max is None):
        label = 'unknown_salary'
    elif annual_min is not None and annual_min >= floor:
        label = 'meets_floor'
    elif annual_max is not None and annual_max < floor:
        label = 'below_floor'
    else:
        label = 'unknown_salary'
        note = note or 'Posted salary does not establish whether the floor is met.'
    return {'label': label, 'score': {'meets_floor': 1, 'below_floor': -1, 'unknown_salary': 0}[label],
            'floor': floor, 'annual_min': annual_min, 'annual_max': annual_max, 'note': note}


def offers(root):
    inv = load_inventory(root)
    haystack = [(str(x), 'skill: ' + str(x)) for x in inv['skills']] + [(a['name'] + ' ' + a['description'], a['name']) for a in inv['assets']]
    results = []
    for name, terms, cost, hours, gpu, build in CATALOG:
        if cost > inv['budget'] or (gpu and inv['gpu_constraint']): continue
        matches = sorted({label for text, label in haystack if any(term in text.casefold() for term in terms)})
        reuse = [label for label in matches if not label.startswith('skill: ')]
        limited_hours = inv['weekly_hours'] is not None and hours > inv['weekly_hours']
        score = len(matches) * 3 + len(reuse) * 3 - (4 if build and not reuse else 0) - (5 if limited_hours else 0)
        why = ', '.join(matches) if matches else 'no inventory match'
        if reuse: why += '; reuse: ' + ', '.join(reuse)
        if limited_hours: why += f'; needs about {hours} hours/week'
        if build and not reuse: why += '; substantial new build'
        results.append((score, name, why))
    return sorted(results, key=lambda row: (-row[0], row[1]))


def prepare(conn, root, ident, profile=None):
    row = experiment(conn, ident)
    inv = load_inventory(root)
    profile = profile or {}
    name = row[1]
    # Templates contain only user-supplied facts or explicit placeholders.
    person = profile.get('name', '')
    approved = ' '.join(value for (value,) in conn.execute("SELECT value FROM profile_facts WHERE approved=1 AND status='approved'"))
    grounding = approved + ' ' + ' '.join(str(x) for x in inv['skills']) + ' ' + ' '.join(a['name'] + ' ' + a['description'] for a in inv['assets'])
    if person and (person.casefold() not in grounding.casefold() or not smart.check_grounding(person, grounding)):
        person = '[YOU: fill in]'
    elif not person: person = '[YOU: fill in]'
    safe_name = re.sub(r'[^a-zA-Z0-9 _-]', '', name).strip() or '[YOU: fill in]'
    templates = {
        'sample': f'Sample outline for {safe_name}\n1. What the buyer receives: [YOU: fill in]\n2. Example: [YOU: fill in]\n',
        'listing': f'{safe_name}\nBy {person}\nWho it helps: [YOU: fill in]\nDeliverable and scope: [YOU: fill in]\nProof and terms: [YOU: fill in]\n',
        'promo': f'Variant 1: {safe_name} — [YOU: fill in benefit and link]\n\nVariant 2: [YOU: fill in problem] — {safe_name}\n\nVariant 3: [YOU: fill in example and link]\n',
        'fulfillment': 'Checklist\n[ ] Confirm scope with buyer\n[ ] Deliver agreed files online\n[ ] Check quality\n[ ] Record feedback\n',
    }
    paths = []
    for kind, content in templates.items():
        path = private_write(Path(root) / 'income' / str(ident) / f'{kind}.txt', 'DRAFT - not published\n' + content)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        conn.execute('INSERT INTO income_assets(experiment_id,kind,path,sha256,status) VALUES (?,?,?,?,?)', (ident, kind, str(path), digest, 'draft'))
        paths.append(path)
    conn.execute("UPDATE income_experiments SET status='ready',updated=? WHERE id=? AND status IN ('draft','researching')", (state.now(), ident))
    conn.commit()
    return paths


def log_event(conn, ident, kind, value, count=None, note=''):
    row = experiment(conn, ident)
    if kind not in KINDS or value < 0 or (count is not None and count < 0): raise ValueError('Invalid event')
    amount = value if kind in ('purchase','expense','refund') else None
    qty = count if count is not None else (value if kind in ('traffic','inquiry') else 1)
    conn.execute('INSERT INTO income_events(experiment_id,ts,kind,amount_cents,count,source,note) VALUES (?,?,?,?,?,?,?)',
                 (ident, state.now(), kind, amount, qty, 'manual', note))
    if kind == 'expense':
        spent = conn.execute("SELECT coalesce(sum(amount_cents),0) FROM income_events WHERE experiment_id=? AND kind='expense'", (ident,)).fetchone()[0]
        if spent > row[6]: conn.execute('UPDATE income_experiments SET over_budget=1,updated=? WHERE id=?', (state.now(), ident))
    conn.commit()


def _rules(root):
    path = Path(root) / 'income' / 'rules.json'
    defaults = {'zero_inquiries_traffic':100, 'revise_traffic':100, 'low_inquiry_rate':0.01, 'continue_inquiries':3}
    if path.exists(): defaults.update(json.loads(path.read_text()))
    else: private_write(path, json.dumps(defaults, indent=2) + '\n')
    return defaults


def report(conn, root, ident):
    row = experiment(conn, ident)
    totals = {kind:{'count':0,'cents':0} for kind in KINDS}
    for kind, count, amount in conn.execute('SELECT kind,count,amount_cents FROM income_events WHERE experiment_id=?', (ident,)):
        totals[kind]['count'] += count or 0
        totals[kind]['cents'] += amount or 0
    traffic, inquiries = totals['traffic']['count'], totals['inquiry']['count']
    net = totals['purchase']['cents'] - totals['expense']['cents'] - totals['refund']['cents']
    published_at = row[12]
    days = max(0, (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(published_at)).days) if published_at else 0
    rules = _rules(root)
    if row[13]: verdict = f'Stop: expenses {totals["expense"]["cents"]} cents exceed cap {row[6]} cents.'
    elif days >= row[10] and traffic >= rules['zero_inquiries_traffic'] and inquiries == 0:
        verdict = f'Stop: {inquiries} inquiries from {traffic} visits after {days} exposed days (minimum {row[10]}).'
    elif traffic >= rules['revise_traffic'] and inquiries / traffic < rules['low_inquiry_rate']:
        verdict = f'Revise: {inquiries}/{traffic} inquiry rate is {inquiries / traffic:.2%}, below {rules["low_inquiry_rate"]:.2%}.'
    elif net > 0 or inquiries >= rules['continue_inquiries']:
        verdict = f'Continue: net {net} cents and {inquiries} inquiries.'
    else:
        verdict = f'Insufficient data: {traffic} visits, {inquiries} inquiries, {days} exposed days; need more traffic, inquiries, or net receipts.'
    rates = {'inquiry_rate': inquiries / traffic if traffic else None,
             'purchase_rate': totals['purchase']['count'] / traffic if traffic else None}
    evidence = {'totals':totals, 'net_cents':net, 'days_exposed':days, 'rates':rates, 'over_budget':bool(row[13])}
    conn.execute('INSERT INTO income_decisions(experiment_id,ts,verdict,evidence_json) VALUES (?,?,?,?)',
                 (ident, state.now(), verdict, json.dumps(evidence)))
    conn.commit()
    flags = [f'Claim {c[0]}: {c[4]}' for c in claims(conn, ident) if c[4]]
    return evidence, verdict + ' These are observations of one small test, not forecasts.', flags


def publish(conn, ident, platform, url, profile):
    experiment(conn, ident)
    allowed, reason = policy.check('publish', {'platform':platform, 'experiment':ident}, profile)
    if not allowed: return False, reason
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http','https') or not parsed.netloc: raise ValueError('Publication URL must be HTTP(S)')
    conn.execute("UPDATE income_experiments SET status='published',platform=?,published_url=?,published_at=?,updated=? WHERE id=?",
                 (platform, url, state.now(), state.now(), ident))
    conn.commit()
    return True, 'Manual publication recorded'


def status(conn):
    return conn.execute('SELECT id,name,status,over_budget FROM income_experiments ORDER BY id').fetchall()
