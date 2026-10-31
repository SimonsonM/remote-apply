"""iCIMS public discovery and conservative, account-gated application flow."""
import datetime as dt
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

import apply
import policy
import state
import workday

JOB_PATH = re.compile(r'^/jobs/(\d+)/[^/?#]+/job/?$', re.I)
CONFIRM = re.compile(r'thank you for applying|application has been submitted|application submitted', re.I)
CAPTCHA = re.compile(r'captcha challenge|verify you are human|i am human|i.m not a robot', re.I)
NEXT = re.compile(r'^(?:next|continue|save and continue|review application|review)$', re.I)
SUBMIT = re.compile(r'^(?:submit|submit application|apply now)$', re.I)
LAST_REQUEST = {}


def _host(cfg):
    host = cfg['host'].lower().strip()
    if not re.fullmatch(r'[a-z0-9-]+\.icims\.com', host):
        raise ValueError('unrecognized iCIMS host')
    return host


def _get(host, path):
    elapsed = time.monotonic() - LAST_REQUEST.get(host, 0)
    if elapsed < 1:
        time.sleep(1 - elapsed)
    url = f'https://{host}{path}'
    for attempt in range(4):
        LAST_REQUEST[host] = time.monotonic()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=apply.UA), timeout=25) as response:
                return response.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 3:
                raise
            delay = exc.headers.get('Retry-After', '')
            try:
                pause = min(60, max(1, int(delay)))
            except ValueError:
                pause = min(60, 2 ** (attempt + 1))
            time.sleep(pause)
    raise RuntimeError('iCIMS request retries exhausted')


def parse_search(html):
    soup = BeautifulSoup(html, 'html.parser')
    found = {}
    for a in soup.select('a[href]'):
        path = urlparse(a['href']).path
        match = JOB_PATH.fullmatch(path)
        if match:
            found[match.group(1)] = path
    return found


def parse_job(html):
    soup = BeautifulSoup(html, 'html.parser')
    def first(selectors):
        for selector in selectors:
            node = soup.select_one(selector)
            if node:
                value = node.get_text(' ', strip=True)
                if value:
                    return value
        return ''
    title = first(('.iCIMS_JobHeaderTitle', 'h1[itemprop="title"]', 'h1'))
    location = first(('.iCIMS_JobHeaderField.iCIMS_JobHeaderField-Location',
                      '[itemprop="jobLocation"]', '.iCIMS_JobHeaderField-Location',
                      '[data-field="location"]', '.header.left span:not(.sr-only)'))
    description = first(('.iCIMS_JobContent', '[itemprop="description"]',
                         '#job-description', '.job-description'))
    return title, location, description


def fetch_icims(name, cfg, max_pages=10):
    host = _host(cfg)
    seen = set()
    for page_no in range(1, min(max_pages, 10) + 1):
        links = parse_search(_get(host, f'/jobs/search?ss=1&in_iframe=1&pr={page_no}'))
        new = [(job_id, path) for job_id, path in links.items() if job_id not in seen]
        if not new:
            break
        for job_id, path in new:
            seen.add(job_id)
            try:
                title, location, description = parse_job(_get(host, path + '?in_iframe=1'))
            except (OSError, urllib.error.HTTPError):
                continue
            if not title:
                continue
            base = f'https://{host}/jobs/{job_id}'
            yield dict(company=name, title=title, location=location, description=description,
                       url=base + '/job', source='icims', token=host, job_id=job_id,
                       apply_url=base + '/login', salary=apply.salary_from(description), posted_at='')
        if len(links) == 0:
            break


def _frame(page):
    iframe = page.locator('#icims_content_iframe')
    return page.frame_locator('#icims_content_iframe') if iframe.count() else page


def _body(scope):
    try:
        return scope.locator('body').inner_text(timeout=5000)
    except Exception:
        return ''


def _visible(loc):
    try:
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:
        return False


def _captcha(scope):
    return (_visible(scope.locator('iframe[src*="/bframe"], iframe[title*="challenge" i], iframe[title*="recaptcha" i], '
                                   '.h-captcha iframe[title*="checkbox" i], .g-recaptcha iframe[title*="checkbox" i]'))
            or bool(CAPTCHA.search(_body(scope)[:3000])))


def _click(scope, pattern):
    for role in ('button', 'link'):
        loc = scope.get_by_role(role, name=pattern)
        if _visible(loc):
            loc.first.click(timeout=5000)
            return True
    return False


def _page_errors(scope):
    errors = []
    for el in scope.locator('[role="alert"]:visible, .error:visible, .iCIMS_ErrorMessage:visible').all()[:10]:
        message = re.sub(r'\s+', ' ', el.inner_text()).strip()
        if message:
            errors.append(message[:180])
    return errors


def _confirmed(scope, job):
    body = _body(scope)
    if re.search(r'not submitted|failed to submit|submission error', body, re.I):
        return False
    if CONFIRM.search(body):
        return True
    return bool(re.search(r'candidate dashboard|my applications', body, re.I)
                and re.search(r'\bsubmitted\b', body, re.I)
                and job.get('title', '').casefold() in body.casefold())


def _settle(page):
    try:
        page.wait_for_load_state('networkidle', timeout=12000)
    except Exception:
        pass
    page.wait_for_timeout(700)


def _auth(page, job, prof, keys):
    email = keys.get('ICIMS_EMAIL') or keys.get('WORKDAY_EMAIL') or prof.get('email')
    password = keys.get('ICIMS_PASSWORD')
    if not email or not password:
        return ['iCIMS email or password missing from keys.env/profile']
    for _ in range(5):
        scope = _frame(page)
        if _captcha(page) or _captcha(scope):
            return ['CAPTCHA challenge visible']
        body = _body(scope)
        if re.search(r'profile|resume|application questions|voluntary self.identification', body, re.I) and not re.search(r'sign in|create account', body[:500], re.I):
            return []
        email_field = scope.locator('input[type="email"], input[name*="email" i]')
        if _visible(email_field):
            email_field.first.fill(email)
            _click(scope, re.compile(r'^(continue|next)$', re.I))
            _settle(page)
            if _captcha(page) or _captcha(_frame(page)):
                return ['CAPTCHA challenge visible']
        scope = _frame(page)
        password_field = scope.locator('input[type="password"]')
        if not _visible(password_field):
            if _click(scope, re.compile(r'create account|register|sign in|log in', re.I)):
                _settle(page)
                continue
            return ['iCIMS account form unavailable']
        for field in password_field.all():
            field.fill(password)
        for field in scope.locator('input[type="email"], input[name*="email" i]').all():
            field.fill(email)
        for box in scope.locator('input[type="checkbox"]').all():
            label = box.evaluate('e => e.closest("label,fieldset,.form-group")?.innerText || e.parentElement?.innerText || ""')
            if apply.restricted(label):
                return [apply.strip_label(label)]
            if re.search(r'terms (?:of use|and conditions)|privacy (?:notice|policy)', label, re.I):
                box.check()
            else:
                return [apply.strip_label(label)]
        if not _click(scope, re.compile(r'^(sign in|log in|create account|register|continue)$', re.I)):
            return ['iCIMS account submit selector unavailable']
        _settle(page)
        scope = _frame(page)
        body = _body(scope)
        if re.search(r'verif(?:y|ication).{0,50}email|check your (?:email|inbox)|activation link', body, re.I):
            host = urlparse(job['apply_url']).hostname
            allowed = re.compile(r'^(?:' + re.escape(host) + r'|login\.icims\.com|icims\.com)$', re.I)
            link = workday.verification_link(keys, sender='icims', allowed_host=allowed)
            if not link:
                return ['Email verification required']
            page.goto(link, wait_until='domcontentloaded')
            _settle(page)
            page.goto(job['apply_url'], wait_until='domcontentloaded')
            _settle(page)
            continue
        if re.search(r'invalid password|incorrect password|unable to sign in', body, re.I):
            return ['iCIMS sign-in failed']
        if not _visible(scope.locator('input[type="password"]')):
            return []
    return ['iCIMS account did not advance']


def _resolve(label, prof, bank, company):
    if apply.restricted(label):
        return ''
    if re.search(r'disabilit|gender|race|ethnic|hispanic|veteran|self.identif', label, re.I):
        return apply.resolve(label, prof, bank, company) or '__decline__'
    return apply.resolve(label, prof, bank, company)


def _fill_page(scope, job, prof, bank):
    fields = scope.evaluate(apply.FIELDS_JS)
    missing = []
    for field in fields:
        context = ' '.join(str(field.get(key) or '') for key in ('question', 'heading', 'label'))
        if apply.restricted(context):
            missing.append(apply.strip_label(field.get('question') or field.get('label') or context))
    if missing:
        return [], fields, missing
    # A previously attached resume must not be silently duplicated.
    files = [f for f in fields if f['type'] == 'file']
    resume_name = Path(prof['resume']).name
    if len(files) > 1:
        return [], fields, ['Multiple upload controls need review']
    if files and resume_name in _body(scope):
        fields = [f for f in fields if f['type'] != 'file']
    actions, unknown = apply.generic_plan(fields, prof, bank, job['company'],
                                          resolver=_resolve)
    missing.extend(unknown)
    if missing:
        return [], fields, missing
    results = apply.generic_fill(scope, actions, tolerant=True)
    for result in results:
        if result['status'] == 'failed':
            field = next((f for f in fields if f['i'] == result['key']), {})
            missing.append(f"{field.get('question') or field.get('label') or result['key']}: {result['error']}")
    return results, fields, missing


def _proof(page, job):
    apply.PROOF.mkdir(exist_ok=True)
    slug = re.sub(r'[^a-z0-9]+', '_', f"{job.get('company', '')}_{job.get('job_id', '')}", flags=re.I).strip('_')[:90]
    path = apply.PROOF / f'icims_{slug}_{dt.datetime.now():%Y%m%d_%H%M%S}.png'
    masks = [page.locator('input[type="password"], input[type="email"]')]
    scope = _frame(page)
    if scope is not page:
        masks.append(scope.locator('input[type="password"], input[type="email"]'))
    page.screenshot(path=str(path), full_page=True, mask=masks, mask_color='#000000')
    return path


def apply_job(page, job, prof, bank, dry=False):
    """Return (status, note); dry fills through review and never submits."""
    apply.US_CITIZEN = bool(prof.get('us_citizen'))
    apply._set_current_job(job)
    results, fields, missing, visited = [], [], [], []
    with apply.db() as conn:
        attempt = apply._new_attempt(conn, job, prof)
        try:
            page.goto(job['apply_url'], wait_until='domcontentloaded', timeout=45000)
            _settle(page)
            missing = _auth(page, job, prof, apply.load_keys())
            if missing:
                if 'CAPTCHA' in missing[0]:
                    state.set_state(conn, attempt, 'blocked')
                    return 'captcha', f'{missing[0]} [proof: {_proof(page, job)}]'
                raise StopIteration
            for step in range(12):
                scope = _frame(page)
                if _captcha(page) or _captcha(scope):
                    state.set_state(conn, attempt, 'blocked')
                    return 'captcha', f'CAPTCHA challenge visible [proof: {_proof(page, job)}]'
                body = _body(scope)
                if _confirmed(scope, job) and not scope.locator('form').count():
                    state.set_state(conn, attempt, 'confirmed', apply.followup_days())
                    return 'applied', f'[proof: {_proof(page, job)}]'
                heading = scope.locator('h1, h2, .iCIMS_Header')
                visited.append(heading.first.inner_text()[:80] if heading.count() else f'page {step + 1}')
                page_results, page_fields, blockers = _fill_page(scope, job, prof, bank)
                results.extend(page_results)
                fields.extend(page_fields)
                missing.extend(blockers)
                if missing:
                    break
                if _visible(scope.get_by_role('button', name=SUBMIT)) or _visible(scope.get_by_role('link', name=SUBMIT)):
                    outcome, note = apply._record_attempt(conn, attempt, job, results, [], fields, prof=prof, bank=bank)
                    note += f' [pages: {", ".join(visited)}] [proof: {_proof(page, job)}]'
                    if dry:
                        return 'ready', note
                    allowed, reason = policy.check('submit', {**job, 'submitted_today': prof.get('_submitted_today', 0)}, prof)
                    if not allowed:
                        state.set_state(conn, attempt, 'blocked')
                        return 'needs_you', reason
                    state.begin_submit(conn, job['url'])
                    if not _click(scope, SUBMIT):
                        state.set_state(conn, attempt, 'uncertain')
                        return 'unconfirmed', 'Submit selector unavailable'
                    _settle(page)
                    if _confirmed(_frame(page), job):
                        state.set_state(conn, attempt, 'confirmed', apply.followup_days())
                        return 'applied', note
                    state.set_state(conn, attempt, 'uncertain')
                    return 'unconfirmed', note
                if not _click(scope, NEXT):
                    missing.append('Next page selector unavailable')
                    break
                _settle(page)
                errors = _page_errors(_frame(page))
                if errors:
                    missing.extend(errors)
                    break
            else:
                missing.append('Too many iCIMS pages without reaching review')
        except StopIteration:
            pass
        except Exception as exc:
            missing.append('Automation error: ' + workday._safe_error(exc))
        outcome, note = apply._record_attempt(conn, attempt, job, results, missing, fields, prof=prof, bank=bank)
        note += f' [pages: {", ".join(visited)}]'
        try:
            note += f' [proof: {_proof(page, job)}]'
        except Exception:
            pass
        return 'needs_you', note
