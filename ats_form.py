"""Conservative form handling shared by the Workable and SmartRecruiters adapters."""
import hashlib
import os
import re

import apply
import policy
import state

NEXT = re.compile(r'^(?:next|continue|save and continue|review application|review)$', re.I)
SUBMIT = re.compile(r'^(?:submit|submit application|send application|apply now)$', re.I)
CONSENT = re.compile(r'\b(?:consent|agree|accept|authorize|authorise|waive|acknowledge)\b', re.I)
FIELD_NAMES = {'firstname': 'First name', 'first_name': 'First name',
               'lastname': 'Last name', 'last_name': 'Last name'}


def _label(field):
    return apply.strip_label(field.get('question') or field.get('label') or field.get('heading') or '')


def plan(fields, job, prof, bank):
    """Return generic actions and human blockers without inventing form answers."""
    blockers = []
    for field in fields:
        context = ' '.join(str(field.get(key) or '') for key in ('heading', 'question', 'label'))
        if apply.restricted(context) or (field.get('type') == 'checkbox' and CONSENT.search(context)):
            blockers.append(_label(field) or 'Consent question requires review')
    if blockers:
        return [], list(dict.fromkeys(blockers))
    def answer(label, profile, answers, company):
        normalized = FIELD_NAMES.get(re.sub(r'[^a-z_]', '', label.lower()), label)
        return apply.resolve(normalized, profile, answers, company)
    return apply.generic_plan(fields, prof, bank, job['company'], resolver=answer)


def fill_page(page, job, prof, bank):
    fields = page.evaluate(apply.FIELDS_JS)
    actions, missing = plan(fields, job, prof, bank)
    if missing:
        return [], fields, missing
    results = apply.generic_fill(page, actions, tolerant=True)
    for result in results:
        if result['status'] == 'failed':
            field = next((f for f in fields if f['i'] == result['key']), {})
            missing.append(f"{_label(field) or result['key']}: {result['error']}")
    return results, fields, missing


def _visible(locator):
    try:
        return locator.count() > 0 and locator.first.is_visible()
    except Exception:
        return False


def _errors(page):
    messages = []
    for item in page.locator('[role="alert"]:visible, [aria-invalid="true"]:visible, .error:visible').all()[:10]:
        value = re.sub(r'\s+', ' ', item.inner_text()).strip()
        if value:
            messages.append(value[:180])
    return messages


def challenge_present(page):
    return any('captcha-delivery.com' in frame.url for frame in page.frames) or 'captcha-delivery.com' in page.content()


def apply_job(page, job, prof, bank, dry=False):
    apply.US_CITIZEN = bool(prof.get('us_citizen'))
    apply._set_current_job(job)
    results, fields, missing = [], [], []
    with apply.db() as conn:
        attempt = apply._new_attempt(conn, job, prof)
        try:
            page.goto(job['apply_url'], wait_until='domcontentloaded', timeout=45000)
            try:
                page.wait_for_selector('form input, form textarea, form select, iframe[title*="captcha" i]', timeout=20000)
            except Exception:
                pass
            for step in range(8):
                page.wait_for_timeout(500)
                if challenge_present(page):
                    missing.append('CAPTCHA challenge visible')
                    break
                if _visible(page.locator('iframe[title*="captcha" i], iframe[src*="/bframe"], .h-captcha iframe[title*="checkbox" i]')):
                    missing.append('CAPTCHA challenge visible')
                    break
                if not page.locator('form').count():
                    start = page.get_by_role('button', name=re.compile(r'^(?:apply|apply now|start application|i.m interested)$', re.I))
                    if _visible(start):
                        start.first.click()
                        continue
                    missing.append('Application form unavailable')
                    break
                page_results, page_fields, blockers = fill_page(page, job, prof, bank)
                results.extend(page_results)
                fields.extend(page_fields)
                missing.extend(blockers)
                if missing:
                    break
                # Unmapped custom controls need a human; FIELDS_JS only plans native controls.
                if page.locator('[role="combobox"]:visible, [contenteditable="true"]:visible').count():
                    missing.append('Custom application control needs review')
                    break
                submit = page.get_by_role('button', name=SUBMIT)
                if _visible(submit):
                    outcome, note = apply._record_attempt(conn, attempt, job, results, [], fields, prof=prof, bank=bank)
                    if dry or os.environ.get('PYTEST_CURRENT_TEST'):
                        return 'ready', note
                    allowed, reason = policy.check('submit', {**job, 'submitted_today': prof.get('_submitted_today', 0)}, prof)
                    if not allowed:
                        state.set_state(conn, attempt, 'blocked')
                        return 'needs_you', reason
                    before_url = page.url
                    before_text = page.inner_text('body')
                    before_hash = hashlib.sha256(before_text.encode()).hexdigest()
                    state.begin_submit(conn, job['url'])
                    submit.first.click()
                    if apply.confirm_submission(page, before_url, before_hash, before_text):
                        state.set_state(conn, attempt, 'confirmed', apply.followup_days())
                        return 'applied', note
                    state.set_state(conn, attempt, 'uncertain')
                    return 'unconfirmed', 'Submission clicked; confirmation not observed'
                next_button = page.get_by_role('button', name=NEXT)
                if not _visible(next_button):
                    missing.append('Application submit or next control unavailable')
                    break
                next_button.first.click()
                page.wait_for_timeout(500)
                errors = _errors(page)
                if errors:
                    missing.extend(errors)
                    break
            else:
                missing.append('Too many application pages without a review step')
        except Exception as exc:
            missing.append('Automation error: ' + str(exc).splitlines()[0][:180])
        _, note = apply._record_attempt(conn, attempt, job, results, missing, fields, prof=prof, bank=bank)
        return 'needs_you', note
