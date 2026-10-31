"""Disposable, shareable inspection of application forms."""
import datetime as dt
import json
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import answer_store
import apply
import smart
import state

BOARD_HOSTS = ('greenhouse.io', 'lever.co', 'ashbyhq.com')


def public_url(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, '', ''))


def safe_text(value, prof):
    value = str(value or '')
    for key in ('name', 'first_name', 'last_name', 'email', 'phone', 'location', 'current_company', 'current_title', 'website', 'linkedin'):
        secret = str(prof.get(key) or '')
        if len(secret) >= 4:
            value = value.replace(secret, '[redacted]')
    value = re.sub(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', '[email]', value, flags=re.I)
    value = re.sub(r'(?<!\w)(?:\+?\d[\d .()-]{7,}\d)(?!\w)', '[phone]', value)
    return value


def board_host(host):
    return any(host == domain or host.endswith('.' + domain) for domain in BOARD_HOSTS)


def page_kind(fields):
    names = ' '.join(' '.join(str(f.get(k, '')) for k in ('name', 'id', 'label')) for f in fields)
    has_file = any(f.get('type') == 'file' for f in fields)
    has_contact = bool(re.search(r'\bemail\b', names, re.I)) and bool(re.search(r'\b(?:first.?name|last.?name|full.?name|name)\b', names, re.I))
    return 'application_form' if has_file and has_contact else 'listing_or_other'


def outcome(profile_key, answer, source, blocker_kind, answer_id, draftable=False):
    """Map resolution facts to a safe, value-free label."""
    if profile_key:
        return 'profile'
    if blocker_kind == 'stale_answer':
        return f'stale_answer:{answer_id}'
    if blocker_kind == 'unapproved_answer':
        return f'pending_answer:{answer_id}'
    if source.startswith('answer_store:'):
        return source
    return 'draftable' if draftable else 'none'


def read_conn():
    if apply.DB.exists():
        conn = sqlite3.connect(f'file:{apply.DB}?mode=ro', uri=True)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {'answer_store', 'standing_authorizations'} <= tables:
            return conn
        copy = sqlite3.connect(':memory:')
        conn.backup(copy)
        conn.close()
        state.migrate(copy)
        return copy
    conn = sqlite3.connect(':memory:')
    state.migrate(conn)
    return conn


def candidates(conn, count):
    if count < 1:
        raise ValueError('--from-db must be positive')
    try:
        rows = conn.execute("SELECT url,apply_url,company,title,source,token,job_id,status FROM jobs WHERE status IN ('new','needs_you') AND source IN ('greenhouse','lever','ashby') ORDER BY source,status,url").fetchall()
    except sqlite3.OperationalError:
        return []
    # Round robin gives each board and status a chance before repeating a bucket.
    buckets = {(source, status): [] for source in ('greenhouse', 'lever', 'ashby') for status in ('new', 'needs_you')}
    for row in rows:
        buckets[(row[4], row[7])].append(row)
    chosen = []
    while len(chosen) < count and any(buckets.values()):
        for key in buckets:
            if buckets[key] and len(chosen) < count:
                chosen.append(buckets[key].pop(0))
    return [dict(zip(('url','apply_url','company','title','source','token','job_id','status'), row)) for row in chosen]


def profile_key(label, prof):
    label = apply.strip_label(label)
    if len(label) < 60:
        for pattern, key in apply.PROFILE_FIELDS:
            if re.search(pattern, label, re.I) and prof.get(key):
                return key
    return None


def describe(label, required, kind, options_count, prof, conn, company):
    label = apply.strip_label(label)
    key = profile_key(label, prof)
    value, source, blocker, ident = answer_store.resolve(conn, label, company)
    if kind == 'file' and re.search(r'resume|\bcv\b', label, re.I) and Path(prof.get('resume') or '').is_file():
        key = 'resume'
    can_draft = bool(kind in ('text', 'textarea') and prof.get('draft_answers', True) and smart.draftable(label, kind, required))
    result = outcome(key, value, source, blocker, ident, can_draft)
    cat = answer_store.category(label)
    if kind == 'file':
        cat = 'file'
    elif cat in ('other', 'profile_fact'):
        cat = 'free_text' if kind == 'textarea' or can_draft else 'other'
    return {'label': safe_text(label, prof)[:120], 'required': bool(required), 'kind': kind,
            'options_count': options_count, 'resolver_outcome': result,
            'profile_key': key if result == 'profile' and key not in ('email', 'phone') else None,
            'category': cat, 'blocker': bool(required and (result == 'none' or result.startswith(('pending_answer:', 'stale_answer:'))))}


def field_rows(fields, prof, conn, company):
    rows = []
    groups = {}
    resume_ids, ambiguous = apply.resume_file_indices(fields)
    for f in fields:
        if f['type'] in ('radio', 'checkbox'):
            groups.setdefault(f['name'] or f"field_{f['i']}", []).append(f)
        else:
            label = f.get('label') or f.get('name') or ''
            kind = 'file' if f['type'] == 'file' else 'select' if f['tag'] == 'select' else 'textarea' if f['tag'] == 'textarea' else 'text'
            row = describe(label, f['required'], kind, len(f.get('options', [])), prof, conn, company)
            if kind == 'file':
                if f['i'] in resume_ids and Path(prof.get('resume') or '').is_file():
                    row.update(resolver_outcome='profile', profile_key='resume', blocker=False)
                elif ambiguous and f['required'] and not apply.is_cover_file(f):
                    row.update(blocker=True, blocker_kind='ambiguous_file')
            if kind == 'select' and f['required'] and row['resolver_outcome'] not in ('none', 'draftable'):
                value = prof.get(profile_key(label, prof)) if profile_key(label, prof) else answer_store.resolve(conn, apply.strip_label(label), company)[0]
                if not value or not apply.pick_option(value, f.get('options', [])):
                    row['blocker'] = True
            rows.append((f['i'], row))
    for group in groups.values():
        first = group[0]
        label = first.get('question') or first.get('label') or first.get('name') or ''
        row = describe(label, any(f['required'] for f in group), first['type'], len(group), prof, conn, company)
        if row['required'] and row['resolver_outcome'] not in ('none', 'draftable'):
            value = prof.get(profile_key(label, prof)) if profile_key(label, prof) else answer_store.resolve(conn, apply.strip_label(label), company)[0]
            if not value or not apply.pick_option(value, [f['label'] for f in group]):
                row['blocker'] = True
        rows.append((first['i'], row))
    return rows


def inspect(job, prof, conn, browser, fill=False):
    url = job.get('apply_url') or job['url']
    host = urlsplit(url).hostname or ''
    report = {'url': safe_text(public_url(job['url']), prof), 'source': job.get('source') or 'unknown',
              'fields': [], 'platform_notes': [], 'aborted_requests': 0,
              'host_note': 'custom_career_site' if not board_host(host) else '',
              'listing_page': False, 'page_kind': 'listing_or_other'}
    ctx = browser.new_context(service_workers='block')
    page = ctx.new_page()
    def route_handler(route):
        if route.request.method.upper() != 'GET':
            report['aborted_requests'] += 1
            route.abort()
        else:
            route.continue_()
    ctx.route('**/*', route_handler)
    try:
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=45000)
        except Exception as exc:
            report['platform_notes'].append('timeout' if 'Timeout' in type(exc).__name__ else 'navigation_error')
            return report
        final_host = urlsplit(page.url).hostname or ''
        if not board_host(final_host):
            report['host_note'] = 'custom_career_site'
        if final_host != host and not board_host(final_host):
            report['platform_notes'].append('custom_career_redirect')
        text = page.locator('body').inner_text(timeout=5000)[:10000].lower()
        if re.search(r'access denied|request blocked|bot detection|unusual traffic', text):
            report['platform_notes'].append('access_denied')
        if page.locator('input[type=password]').count() or (re.search(r'log in|sign in|create an account', text) and not page.locator('input,textarea,select').count()):
            report['platform_notes'].append('login_wall')
        frames = page.locator('iframe')
        for i in range(frames.count()):
            src = (frames.nth(i).get_attribute('src') or '').lower()
            if re.search(r'captcha|recaptcha|hcaptcha|turnstile', src):
                report['platform_notes'].append('captcha_iframe')
        target = page
        fields = page.evaluate(apply.FIELDS_JS)
        if not fields:
            for frame in page.frames[1:]:
                try:
                    embedded = frame.evaluate(apply.FIELDS_JS)
                    if embedded:
                        target, fields = frame, embedded
                        report['platform_notes'].append('iframe_form')
                        break
                except Exception:
                    continue
        if not fields:
            report['platform_notes'].append('zero_fields')
            report['listing_page'] = True
            return report
        report['page_kind'] = page_kind(fields)
        report['listing_page'] = report['page_kind'] == 'listing_or_other'
        def resolver(label, _prof, _bank, company):
            key = profile_key(label, prof)
            if key:
                return prof[key]
            return answer_store.resolve(conn, apply.strip_label(label), company)[0]
        gh_actions = None
        if job.get('source') == 'greenhouse' and job.get('token') and job.get('job_id'):
            try:
                questions = apply.get_json(f"https://boards-api.greenhouse.io/v1/boards/{job['token']}/jobs/{job['job_id']}?questions=true")
                gh_actions, _ = apply.gh_plan(job['token'], job['job_id'], prof, [], job.get('company') or '',
                                               resolver=resolver, questions=questions)
            except Exception:
                report['platform_notes'].append('greenhouse_questions_unavailable')
        rows = field_rows(fields, prof, conn, job.get('company') or '')
        report['fields'] = [row for _, row in rows]
        if fill:
            if gh_actions is not None:
                results = apply.gh_fill(target, gh_actions, tolerant=True)
                by_key = {str(r['key']): r['status'] for r in results}
                for f, row in zip(fields, report['fields']):
                    row['fill_status'] = by_key.get(str(f.get('name')), 'not_attempted')
            else:
                actions, _ = apply.generic_plan(fields, prof, [], job.get('company') or '', resolver=resolver)
                results = apply.generic_fill(target, actions, tolerant=True)
                by_key = {str(r['key']): r['status'] for r in results}
                for key, row in rows:
                    row['fill_status'] = by_key.get(str(key), 'not_attempted')
            page.wait_for_timeout(300)
    except Exception as exc:
        report['platform_notes'].append('timeout' if 'Timeout' in type(exc).__name__ else 'inspection_error')
    finally:
        ctx.close()
    report['platform_notes'] = list(dict.fromkeys(report['platform_notes']))
    return report


def run(urls, from_db=None, fill=False, browser=None, output_dir=None):
    if not urls and from_db is None:
        raise SystemExit('doctor requires URL(s) or --from-db N')
    profile = json.loads(apply.CONFIG.read_text()) if apply.CONFIG.exists() else {}
    profile.setdefault('name', '')
    profile.setdefault('resume', '')
    prof = apply.prepare_profile(profile)
    own_browser = browser is None
    if own_browser:
        from playwright.sync_api import sync_playwright
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
    try:
        with read_conn() as conn:
            selected = candidates(conn, from_db) if from_db is not None else []
            jobs = selected + []
            for url in urls:
                row = conn.execute('SELECT url,apply_url,company,title,source,token,job_id,status FROM jobs WHERE url=? OR apply_url=?', (url, url)).fetchone() if apply.DB.exists() else None
                jobs.append(dict(zip(('url','apply_url','company','title','source','token','job_id','status'), row)) if row else {'url': url, 'apply_url': url, 'company': '', 'source': 'unknown'})
            reports = [inspect(job, prof, conn, browser, fill) for job in jobs]
    finally:
        if own_browser:
            browser.close()
            playwright.stop()
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder = Path(output_dir) if output_dir else apply.ROOT / 'reports'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'doctor_{stamp}.json'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'generated_at_utc': stamp, 'fill': fill, 'jobs': reports}, stream, indent=2)
    for report in reports:
        print(f"{report['url']}: {len(report['fields'])} fields; notes={','.join(report['platform_notes']) or 'none'}; aborted={report['aborted_requests']}")
        for field in report['fields']:
            print(f"  {field['label']} | required={field['required']} | {field['kind']} | options={field['options_count']} | {field['resolver_outcome']}" + (f"({field['profile_key']})" if field['profile_key'] else '') + f" | {field['category']} | blocker={field['blocker']}" + (f" | fill={field['fill_status']}" if fill else ''))
    print(f'Report: {path}')
    return path, reports
