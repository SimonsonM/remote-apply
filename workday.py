"""Conservative Workday application flow. No final action without policy approval."""
import datetime as dt
import email, email.policy
import imaplib
import os
import re
import ssl
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import apply
import policy
import smart
import state

DECLINE = re.compile(r'gender|race|ethnic|hispanic|disabilit|veteran|self.identif', re.I)
VERIFY = re.compile(r'verif|activat', re.I)
ALLOWED_VERIFY_HOST = re.compile(r'(^|\.)(myworkdayjobs|myworkday)\.com$', re.I)
STEPS = ('My Information', 'My Experience', 'Application Questions', 'Voluntary Disclosures', 'Self Identify', 'Review')


def _tenant(job):
    host = urlparse(job.get('url') or job.get('apply_url', '')).hostname or ''
    if not re.fullmatch(r'[a-z0-9-]+\.wd(?:1|3|5|12)\.myworkdayjobs\.com', host, re.I):
        raise ValueError('unrecognized Workday tenant host')
    return host.split('.')[0]


def _account(conn, tenant, status):
    now = state.now()
    conn.execute('INSERT INTO workday_accounts(tenant,created_at,verified_at,status) VALUES (?,?,?,?) '
                 'ON CONFLICT(tenant) DO UPDATE SET status=excluded.status, verified_at=coalesce(excluded.verified_at,workday_accounts.verified_at)',
                 (tenant, now, now if status == 'verified' else None, status))
    conn.commit()


def _safe_error(error):
    result = str(error).splitlines()[0][:200]
    for secret in apply.load_keys().values():
        if secret and len(secret) > 3:
            result = result.replace(secret, '[redacted]')
    return result


def _visible(loc):
    try:
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:
        return False


def _click(page, selector):
    """Workday overlays many buttons with a transparent div[data-automation-id=click_filter]
    that intercepts pointer events, so a plain click times out. Try normal, then the
    overlay that sits on the button, then a forced/JS click."""
    loc = page.locator(selector)
    if not _visible(loc):
        return False
    btn = loc.first
    try:
        btn.click(timeout=4000)
        return True
    except Exception:
        pass
    try:
        overlay = btn.locator('xpath=ancestor-or-self::*[1]/following-sibling::*[@data-automation-id="click_filter"] | '
                              'ancestor::*[1]/*[@data-automation-id="click_filter"]')
        if overlay.count():
            overlay.first.click(timeout=4000)
            return True
    except Exception:
        pass
    try:
        btn.click(force=True, timeout=4000)
        return True
    except Exception:
        btn.evaluate("e => e.click()")
        return True


def _button(page, text):
    loc = page.get_by_role('button', name=re.compile(text, re.I))
    if _visible(loc):
        loc.first.click()
        return True
    return False


def _field_label(el):
    return (el.evaluate("e => {const a=e.getAttribute('aria-labelledby'); if(a) return a.split(' ').map(x=>document.getElementById(x)?.innerText||'').join(' '); const l=e.labels?.[0]; return l?.innerText || e.closest('[data-automation-id=\"formField\"],fieldset,[data-automation-id=\"questionSet\"]')?.innerText?.split('\\n')[0] || e.getAttribute('aria-label') || e.getAttribute('placeholder') || ''}") or '').strip()[:300]


def _record(results, fields, key, label, kind, status, value='', error='', source=''):
    fields.append(dict(i=key, label=label, question=label, required=True, type=kind))
    results.append(dict(key=key, kind=kind, status=status, value=value, error=error, source=source))


def _resolve(label, prof, bank, job):
    if re.search(r'how many years of project management experience|years of (?:formal )?project management experience', label, re.I):
        return '2 years'
    if re.search(r'\bextension\b|\bext\.?$', apply.strip_label(label), re.I):
        return ''
    if apply.restricted(label):
        return ''
    if DECLINE.search(label):
        return apply.resolve(label, prof, bank, job['company']) or '__decline__'
    if re.search(r'how did you hear|source of (your )?application', label, re.I):
        known = apply.resolve(label, prof, bank, job['company'])
        return (known + '|' if known else '') + f"{job['company']} Careers Website|Careers Website|Browsing Career Site|Company Career Site|Career Site|Company Website|Corporate Website|Job Board|Adzuna|Internet|Other"
    if re.search(r'phone device type|device type', label, re.I):
        return 'Mobile|Cell|Mobile Phone'
    if re.fullmatch(r'city', apply.strip_label(label), re.I) and prof.get('location'):
        return apply.resolve('Mailing city', {}, bank, job['company']) or prof['location'].split(',')[0].strip()
    if re.fullmatch(r'state|state/province|region', apply.strip_label(label), re.I) and ',' in prof.get('location', ''):
        state = prof['location'].split(',')[1].strip()
        return apply.resolve('Mailing state', {}, bank, job['company']) or f'{state}|{apply.US_STATES.get(state.upper(), state)}'
    if re.fullmatch(r'country|country/region', apply.strip_label(label), re.I) and 'United States' in prof.get('location', ''):
        return 'United States|United States of America|USA|US'
    if re.search(r'previous(ly)? (a |an )?(employee|worker)|worked (for|at) (us|this company)', label, re.I):
        if any(apply.ckey(name) == apply.ckey(job['company']) or apply.ckey(name) in apply.ckey(label) for name in apply.KNOWN_EMPLOYERS):
            return ''
        return apply.resolve(label, prof, bank, job['company']) or str(prof.get('previous_worker', 'No'))
    return apply.resolve(label, prof, bank, job['company'])


WD_SKILLS = re.compile(r'\b(?:excel|ms project|microsoft project|powerpoint|leading projects)\b', re.I)


def checklist_choice(label, own):
    """Only the four owner-confirmed project tools/activities in a skills checklist."""
    own = (own or '').strip()
    return bool(re.search(r'excel|ms project|smart sheets|powerpoint|leading projects|none of the above', label, re.I)
                and '\n' not in own and WD_SKILLS.search(own)
                and not re.search(r'none of the above|smart sheets', own, re.I))


def date_part_answer(label, step, prof):
    if not re.search(r'\bstart date\b|\bstart month\b|\bstart year\b', label, re.I):
        return ''
    if re.search(r'when can you start|availability|earliest', step + ' ' + label, re.I):
        date = (dt.date.today().replace(day=1) + dt.timedelta(days=35)).replace(day=1)
        return str(date.year) if 'year' in label.lower() else date.strftime('%B')
    employment = prof.get('employment') or {}
    if re.search(r'my experience|work experience|employment', step, re.I) and employment.get('start_month') and employment.get('start_year'):
        return str(employment['start_year']) if 'year' in label.lower() else str(employment['start_month'])
    return ''


def _select(page, el, answer):
    if el.evaluate('e => e.tagName') == 'SELECT':
        options = [o.inner_text().strip() for o in el.locator('option').all()]
        choice = apply.pick_option(answer, options)
        if not choice:
            return False
        el.select_option(label=choice)
        return True
    el.click()
    choices = page.locator('[role=option]:visible, [data-automation-id=promptOption]:visible')
    labels = [x.inner_text().strip() for x in choices.all()]
    choice = apply.pick_option(answer, labels)
    if not choice:
        return False
    choices.nth(labels.index(choice)).click()
    return True


def _wd_options(page):
    opts = page.locator('[role=option]:visible, [data-automation-id=promptOption]:visible, [data-automation-id=menuItem]:visible')
    labels = []
    for i in range(opts.count()):
        try:
            labels.append(opts.nth(i).inner_text().strip())
        except Exception:
            labels.append('')
    return opts, labels


def _fill_listboxes(page, job, prof, bank, results, fields):
    """Workday single-selects are <button aria-haspopup=listbox>; multiselect prompts
    (e.g. How Did You Hear About Us?) are prompt inputs with nested promptOption menus."""
    missing = []
    tried = set()
    if os.environ.get('RA_WD_DEBUG'):
        import sys as _s
        _s.stderr.write('LISTBOX SCAN %s: %s\n' % (_current_step(page).replace('\n', ' '), page.evaluate(
            '''() => [...document.querySelectorAll('button')].filter(e=>/Select One/.test(e.innerText)).map(e=>[e.id, e.getAttribute('aria-haspopup'), e.offsetWidth>0])''')))
    for _round in range(30):  # re-scan after every pick: Workday re-renders the form
        btn = None
        for cand in page.locator('button[aria-haspopup=listbox]:visible').all():
            try:
                cur = (cand.inner_text(timeout=3000) or '').strip()
                key = cand.get_attribute('id', timeout=3000) or cand.get_attribute('aria-label', timeout=3000) or _field_label(cand)
            except Exception:
                continue
            if key in ('languageSelectorButton', 'settingsSelectorButton') or not key:
                continue
            if key not in tried:  # blank OR prefilled: prefilled values from saved drafts must be re-verified
                btn = cand
                tried.add(key)
                break
        if btn is None:
            break
        current = (btn.inner_text() or '').strip()
        blank = not current or re.fullmatch(r'select one', current, re.I)
        label = _field_label(btn) or btn.get_attribute('aria-label', timeout=3000) or btn.get_attribute('data-automation-id', timeout=3000) or 'listbox'
        label = re.sub(r'\s*Select One\s*$', '', label).strip()
        if not blank:
            ans = _resolve(label, prof, bank, job)
            norm = lambda x: re.sub(r'\s+', ' ', x).strip().casefold()
            exact = ans and any(norm(a) == norm(current) or (a.startswith('__') and apply.pick_option(a, [current]))
                                for a in ans.split('|'))
            if exact:
                _record(results, fields, label, label, 'select', 'filled', current, source='approved')
                continue
            if not ans and re.fullmatch(r'country\*?|language\*?', label, re.I):
                continue
            # prefilled value is not backed by an approved answer: re-pick below (or block)
        required = label.rstrip().endswith('*') or btn.get_attribute('aria-required', timeout=3000) == 'true'
        if apply.restricted(label):
            _record(results, fields, label, label, 'select', 'failed', error='restricted attestation')
            continue
        answer = date_part_answer(label, _current_step(page), prof) or _resolve(label, prof, bank, job)
        if not answer:
            if required:
                missing.append(label)
            continue
        try:
            _, stale = _wd_options(page)  # options already in the DOM from other widgets
            btn.click(timeout=5000)
            page.wait_for_timeout(600)
            opts, labels = _wd_options(page)
            fresh = [l for i, l in enumerate(labels) if i >= len(stale) or l != stale[i]]
            choice = apply.pick_option(answer, [l for l in fresh if l]) or None
            if not choice:
                page.keyboard.press('Escape')
                missing.append(f'{label} (no matching option; options: {" / ".join(fresh[:15])})')
                continue
            idx = max(i for i, l in enumerate(labels) if l == choice)  # newest occurrence = the open popup
            opts.nth(idx).click()
            page.wait_for_timeout(400)
            ok = choice.casefold()[:25] in (btn.inner_text() or '').casefold()
            if os.environ.get('RA_WD_DEBUG'):
                import sys as _s
                _s.stderr.write(f'PICK {label[:50]!r} ans={answer!r} n={len(labels)} labels={labels[:6]!r} choice={choice!r} idx={labels.index(choice)} after={(btn.inner_text() or "")[:30]!r} ok={ok}\n')
            _record(results, fields, label, label, 'select', 'filled' if ok else 'failed', choice if ok else '',
                    error='' if ok else 'readback mismatch', source='demographics' if answer == '__decline__' else 'approved')
        except Exception as exc:
            _record(results, fields, label, label, 'select', 'failed', error=_safe_error(exc))
    for box in page.locator('[data-automation-id=multiselectInputContainer]:visible, [data-uxi-widget-type=multiselect]:visible').all():
        container = box.locator('xpath=ancestor::*[@data-automation-id="formField" or starts-with(@data-automation-id,"formField")][1]')
        label = ((container.first.inner_text() if container.count() else '') or _field_label(box)).split('\n')[0].strip()
        if box.locator('[data-automation-id=selectedItem], [data-automation-id=selectedItemList] li').count():
            continue
        answer = _resolve(label, prof, bank, job)
        required = label.rstrip().endswith('*')
        if not answer:
            if required:
                missing.append(label)
            continue
        try:
            box.click(timeout=5000)
            page.wait_for_timeout(800)
            chosen = None
            for _ in range(3):  # nested menus: category -> item
                opts, labels = _wd_options(page)
                if not labels:
                    break
                choice = apply.pick_option(answer, labels) or next((l for l in labels if re.search(r'^other|job board|website|internet', l, re.I)), None)
                if not choice:
                    break
                opts.nth(labels.index(choice)).click()
                chosen = choice
                page.wait_for_timeout(700)
                if box.locator('[data-automation-id=selectedItem], [data-automation-id=selectedItemList] li').count():
                    break
            page.keyboard.press('Escape')
            ok = bool(box.locator('[data-automation-id=selectedItem], [data-automation-id=selectedItemList] li').count())
            _record(results, fields, label, label, 'select', 'filled' if ok else 'failed', chosen or '',
                    error='' if ok else 'no selection', source='approved')
            if not ok and required:
                missing.append(label)
        except Exception as exc:
            _record(results, fields, label, label, 'select', 'failed', error=_safe_error(exc))
    return missing


def _page_errors(page):
    """Workday's error banner after Save and Continue."""
    banner = page.locator('[data-automation-id=errorBanner]:visible, [data-automation-id=errorMessage]:visible')
    msgs = []
    for i in range(min(banner.count(), 10)):
        try:
            msgs.append(re.sub(r'\s+', ' ', banner.nth(i).inner_text()).strip()[:160])
        except Exception:
            pass
    if not msgs and re.search(r'Errors? Found', page.inner_text('body')[:5000]):
        msgs.append('Workday reported errors on this page')
    return msgs


def _fill_page(page, job, prof, bank, results, fields, step=''):
    """Visit each visible form control once, with a source for every filled value."""
    missing = []
    _stable(page)
    # Listboxes/prompts first: choosing them re-renders the form (new fields appear).
    missing += _fill_listboxes(page, job, prof, bank, results, fields)
    page.wait_for_timeout(500)
    sel = 'input:visible, textarea:visible, select:visible, [role=combobox]:visible'
    # Workday re-renders while we type, so nth()-based handles drift onto other fields.
    # Address every control by its id; fall back to a temporary data attribute.
    ids = page.evaluate('''(sel) => [...document.querySelectorAll(sel.replaceAll(':visible',''))]
        .filter(e => e.offsetWidth || e.offsetHeight || e.getClientRects().length)
        .map((e, i) => { if (!e.id) e.setAttribute('data-ra-wd', 'f' + i); return e.id ? '[id="' + e.id + '"]' : '[data-ra-wd="f' + i + '"]'; })''', sel)
    seen = set()
    grounded_cache = None
    checkbox_groups = {}
    for css in ids:
        el = page.locator(css).first
        if not el.count():
            continue
        aid = el.get_attribute('data-automation-id', timeout=3000) or ''
        typ = (el.get_attribute('type', timeout=3000) or '').lower()
        if typ in ('hidden', 'submit', 'button', 'radio') or aid in ('file-upload-input-ref',):
            continue
        key = el.get_attribute('id', timeout=3000) or el.get_attribute('name', timeout=3000) or aid or f'wd_{len(seen)}'
        if key in seen:
            continue
        seen.add(key)
        label = _field_label(el) or key
        if typ == 'file':
            continue
        if typ == 'checkbox':
            # Full surrounding text matters: the short label can omit waiver wording.
            label = (el.evaluate("e => e.closest('fieldset,[data-automation-id=\"questionSet\"],[data-automation-id=\"formField\"]')?.innerText || e.parentElement?.parentElement?.parentElement?.innerText || ''") or label).strip()[:1000]
            if apply.restricted(label):
                _record(results, fields, key, label, 'checkbox', 'failed', error='restricted attestation')
                continue
            own = _field_label(el)
            if checklist_choice(label, own):
                _tick(page, el)
                _record(results, fields, key, label, 'checkbox', 'filled' if el.is_checked() else 'failed', own, source='profile')
                continue
            group_answer = _resolve(label, prof, bank, job) if own and own not in ('', key) and own.strip() != label.strip() else ''
            if own and own not in ('', key) and (DECLINE.search(label) or (group_answer and not group_answer.startswith('__agree__'))):
                # Self-ID "check one of the boxes": tick only the box whose own text is the approved answer.
                answer = group_answer or _resolve(label, prof, bank, job) or '__decline__'
                group = re.sub(r'\s+', ' ', label)[:120]
                if apply.pick_option(answer, [own]):
                    _tick(page, el)
                    _record(results, fields, key, group, 'checkbox', 'filled' if el.is_checked() else 'failed', own, source='demographics')
                    checkbox_groups[group] = True
                else:
                    checkbox_groups.setdefault(group, False)
                continue
            answer = _resolve(label, prof, bank, job)
            generic_terms = bool(re.search(r'privacy (notice|policy)|terms (of use|& conditions|and conditions)|candidate account|read and (understand|acknowledge)', label, re.I))
            if answer == '__agree__' or generic_terms:
                el.check()
                _record(results, fields, key, label, 'checkbox', 'filled' if el.is_checked() else 'failed', 'checked', source='approved' if answer else 'generic_terms')
            elif el.get_attribute('required', timeout=3000) is not None or el.get_attribute('aria-required', timeout=3000) == 'true' or label.rstrip().endswith('*'):
                missing.append(label)
            continue
        if apply.restricted(label):
            _record(results, fields, key, label, typ or 'text', 'failed', error='restricted attestation')
            continue
        if aid.startswith('dateSection') and re.search(r'self.identif|voluntary|disclosure', step, re.I) \
                and re.fullmatch(r'date\*?', apply.strip_label(label).strip(), re.I):
            # Signature date on the voluntary self-identification form: today.
            today = dt.date.today()
            part = {'dateSectionMonth-input': f'{today.month:02d}', 'dateSectionDay-input': f'{today.day:02d}',
                    'dateSectionYear-input': str(today.year)}.get(aid)
            if aid == 'dateSectionMonth-input':
                try:
                    el.click(force=True, timeout=4000)
                except Exception:
                    el.focus()
                page.keyboard.type(today.strftime('%m%d%Y'), delay=80)
                page.wait_for_timeout(300)
                _record(results, fields, key, 'Self-identification date', 'text', 'filled', today.isoformat(), source='today')
            continue
        prefilled = el.input_value().strip() if el.evaluate('e => e.tagName') in ('INPUT', 'TEXTAREA', 'SELECT') else ''
        if prefilled and re.search(r'\bextension\b', apply.strip_label(label), re.I):
            el.fill('')  # never carry a phone number into the extension field
            continue
        if prefilled:
            if re.search(r'skills?', label, re.I) and re.search(r'My Experience', step, re.I):
                _record(results, fields, key, label, 'text', 'filled', prefilled, source='resume')
                continue
            expected = _resolve(label, prof, bank, job)
            choices = expected.split('|') if expected else []
            grounded = ''
            if re.search(r'My Experience', step, re.I):
                if grounded_cache is None:
                    grounded_cache = smart.grounded_resume_text(prof).lower()
                    for block in (prof.get('employment'), prof.get('education')):
                        if isinstance(block, dict):
                            grounded_cache += ' ' + ' '.join(str(v).lower() for v in block.values())
                grounded = grounded_cache
            verified = any(prefilled.casefold() == str(v).casefold() for v in choices) or (
                bool(grounded) and len(prefilled) >= 3 and prefilled.lower() in grounded)
            _record(results, fields, key, label, 'text', 'filled' if verified else 'failed', prefilled if verified else '',
                    error='' if verified else 'parsed value not verified', source='resume' if grounded else 'approved')
            continue
        answer = date_part_answer(label, step, prof) or _resolve(label, prof, bank, job)
        drafted = False
        required = el.get_attribute('required', timeout=3000) is not None or el.get_attribute('aria-required', timeout=3000) == 'true' or label.rstrip().endswith('*')
        if not answer and required and smart.draftable(label, typ or 'text', True) and prof.get('draft_answers', True):
            try:
                draft = smart.draft(prof, job, label)
                problems = smart.grounding_problems(draft, smart.grounded_resume_text(prof), prof.get('never_claim', []), profile=prof, job=job)
                if not problems:
                    answer = draft
                    drafted = True
            except Exception:
                pass
        if not answer:
            if required:
                missing.append(label)
            continue
        try:
            if el.evaluate('e => e.tagName') == 'SELECT' or el.get_attribute('role', timeout=3000) == 'combobox':
                if not _select(page, el, answer):
                    missing.append(label)
                    continue
                value = answer
                kind = 'select'
            else:
                value = apply.text_answer(answer)
                if not value:
                    missing.append(label)
                    continue
                el.fill(value)
                if not apply._read_back(el, 'text', value):
                    if not required:
                        el.fill('')  # e.g. numeric-only box mangles a range: leave optional fields blank
                        continue
                    raise ValueError('readback mismatch')
                kind = 'text'
            _record(results, fields, key, label, kind, 'filled', value,
                    source='draft' if drafted else 'demographics' if answer == '__decline__' else 'approved')
        except Exception as exc:
            _record(results, fields, key, label, typ or 'text', 'failed', error=_safe_error(exc))
    missing += [f'{g} (no matching box)' for g, done in checkbox_groups.items() if not done]
    groups = {}
    for radio in page.locator('input[type=radio]:visible').all():
        name = radio.get_attribute('name', timeout=3000) or radio.get_attribute('data-automation-id', timeout=3000) or 'radio'
        groups.setdefault(name, []).append(radio)
    for name, radios in groups.items():
        if any(r.is_checked() for r in radios):
            continue
        context = radios[0].evaluate("e => e.closest('fieldset,[data-automation-id=\"questionSet\"],[data-automation-id=\"formField\"]')?.innerText || ''")
        label = apply.clean_field_label(context) or _field_label(radios[0]) or name
        if apply.restricted(context):
            _record(results, fields, name, context or label, 'radio', 'failed', error='restricted attestation')
            continue
        answer = _resolve(label, prof, bank, job)
        options = [_field_label(r) for r in radios]
        choice = apply.pick_option(answer, options) if answer else None
        if not choice:
            missing.append(label)
            continue
        selected = radios[options.index(choice)]
        selected.check()
        _record(results, fields, name, label, 'radio', 'filled', choice,
                source='demographics' if answer == '__decline__' else 'approved')
    return missing


def _verify_message(message, since, sender='workday', allowed_host=ALLOWED_VERIFY_HOST):
    from_address = str(message.get('From', '')).lower()
    subject = str(email.header.make_header(email.header.decode_header(message.get('Subject', '')))).lower()
    if not (sender and sender in from_address and VERIFY.search(subject)):
        return None
    try:
        date = parsedate_to_datetime(message['Date'])
        if date.tzinfo is None or date.astimezone(dt.timezone.utc) < since:
            return None
    except Exception:
        return None
    body = message.get_body(preferencelist=('html', 'plain')) if message.is_multipart() else message
    if not body:
        return None
    text = body.get_content()
    import html as html_lib
    for raw in re.findall(r'https?://[^\s<>"\']+', html_lib.unescape(text)):
        url = raw.rstrip(').,;')
        if allowed_host.search(urlparse(url).hostname or ''):
            return url
    return None


def _bridge_client(keys, imap_factory=imaplib.IMAP4):
    """Logged-in Proton Bridge IMAP client, trusting only the Bridge's own loopback certificate."""
    probe = imap_factory('127.0.0.1', 1143)
    try:
        probe_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        probe_ctx.check_hostname = False
        probe_ctx.verify_mode = ssl.CERT_NONE
        probe.starttls(probe_ctx)
        certificate = ssl.DER_cert_to_PEM_cert(probe.sock.getpeercert(binary_form=True))
    finally:
        try:
            probe.logout()
        except Exception:
            pass
    client = imap_factory('127.0.0.1', 1143)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.load_verify_locations(cadata=certificate)
    client.starttls(ctx)
    client.login(keys['BRIDGE_USER'], keys['BRIDGE_PASS'])
    return client


VERIFY_FOLDERS = ('INBOX', '"Folders/Job Applications"')
PENDING_VERIFY = ('created', 'creating', 'email_verification')


def pending_verification_links(keys, tenants, hours=24, imap_factory=imaplib.IMAP4):
    """{tenant: link} for still-valid Workday verification mails (read or unread, inbox or the sorted folder)."""
    if not tenants or not keys.get('BRIDGE_USER') or not keys.get('BRIDGE_PASS'):
        return {}
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    criteria = ('SINCE', (since - dt.timedelta(days=1)).strftime('%d-%b-%Y'), 'FROM', 'workday',
                'OR', 'SUBJECT', 'verif', 'SUBJECT', 'activat')
    found = {}
    client = _bridge_client(keys, imap_factory)
    try:
        for folder in VERIFY_FOLDERS:
            try:
                if client.select(folder, readonly=True)[0] != 'OK':
                    continue
                typ, data = client.search(None, *criteria)
            except Exception:
                continue
            for uid in (data[0] or b'').split() if typ == 'OK' else []:
                typ, payload = client.fetch(uid, '(BODY.PEEK[])')
                raw = next((p[1] for p in payload if isinstance(p, tuple)), None) if typ == 'OK' else None
                if not raw:
                    continue
                link = _verify_message(email.message_from_bytes(raw, policy=email.policy.default), since)
                host = urlparse(link or '').hostname or ''
                tenant = host.split('.')[0].lower() if re.fullmatch(r'[a-z0-9-]+\.wd(?:1|3|5|12)\.myworkdayjobs\.com', host, re.I) else ''
                if tenant in tenants and '/activate/' in (link or ''):
                    found[tenant] = link  # newest wins: SEARCH returns ascending order
    finally:
        try:
            client.logout()
        except Exception:
            pass
    return found


def sweep_verifications(conn, keys, open_page, log=print):
    """Visit pending verification links before they expire; mark those tenants verified. Returns count."""
    tenants = {r[0] for r in conn.execute(
        f"SELECT tenant FROM workday_accounts WHERE status IN ({','.join('?' * len(PENDING_VERIFY))})", PENDING_VERIFY)}
    try:
        links = pending_verification_links(keys, tenants)
    except Exception as exc:
        log(f'verify sweep: mail unavailable ({_safe_error(exc)})')
        return 0
    done = 0
    for tenant, link in links.items():
        page = open_page()
        try:
            page.goto(link, wait_until='domcontentloaded', timeout=45000)
            page.wait_for_timeout(4000)
            body = page.inner_text('body')[:3000]
            if re.search(r'expired|invalid|no longer valid|something went wrong', body, re.I):
                log(f'verify sweep: {tenant} link rejected ({re.sub(chr(10), " ", body)[:80]})')
                continue
            _account(conn, tenant, 'verified')
            done += 1
            log(f'verify sweep: {tenant} verified')
        except Exception as exc:
            log(f'verify sweep: {tenant} failed ({_safe_error(exc)})')
        finally:
            try:
                page.close()
            except Exception:
                pass
    return done


def verification_link(keys, timeout=300, imap_factory=imaplib.IMAP4, sender='workday', allowed_host=ALLOWED_VERIFY_HOST):
    """Server-side SEARCH limits FETCH to verification mail only."""
    if not keys.get('BRIDGE_USER') or not keys.get('BRIDGE_PASS'):
        return None
    deadline = time.monotonic() + timeout
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)
    criteria = ('UNSEEN', 'SINCE', (since - dt.timedelta(days=1)).strftime('%d-%b-%Y'),  # IMAP SINCE is date-only, server-local
                'OR', 'FROM', 'myworkday' if sender == 'workday' else sender, 'FROM', sender,
                'OR', 'SUBJECT', 'verify', 'OR', 'SUBJECT', 'verification', 'SUBJECT', 'activate')
    while time.monotonic() < deadline:
        # Obtain the Bridge certificate from loopback, then authenticate only on
        # a second connection whose trust store contains that certificate alone.
        probe = imap_factory('127.0.0.1', 1143)
        try:
            probe_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            probe_ctx.check_hostname = False
            probe_ctx.verify_mode = ssl.CERT_NONE
            probe.starttls(probe_ctx)
            certificate = ssl.DER_cert_to_PEM_cert(probe.sock.getpeercert(binary_form=True))
        finally:
            try:
                probe.logout()
            except Exception:
                pass
        client = imap_factory('127.0.0.1', 1143)
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False  # Bridge certs identify its local endpoint
            ctx.load_verify_locations(cadata=certificate)
            client.starttls(ctx)
            client.login(keys['BRIDGE_USER'], keys['BRIDGE_PASS'])
            client.select('INBOX', readonly=True)
            typ, data = client.search(None, *criteria)
            if typ == 'OK':
                for uid in (data[0] or b'').split():
                    typ, payload = client.fetch(uid, '(BODY.PEEK[])')
                    if typ != 'OK':
                        continue
                    raw = next((part[1] for part in payload if isinstance(part, tuple)), None)
                    if raw:
                        link = _verify_message(email.message_from_bytes(raw, policy=email.policy.default), since, sender, allowed_host)
                        if link:
                            return link
        finally:
            try:
                client.logout()
            except Exception:
                pass
        time.sleep(min(10, max(0, deadline - time.monotonic())))
    return None


def _fill_credentials(page, keys, creating, results, fields):
    """Fill email/password (+verify and account terms when creating). Returns a blocker list or None."""
    for aid, val in [('email', keys['WORKDAY_EMAIL']), ('password', keys['WORKDAY_PASSWORD']),
                     ('verifyPassword', keys['WORKDAY_PASSWORD'])]:
        if aid == 'verifyPassword' and not creating:
            continue
        el = page.locator(f'[data-automation-id="{aid}"]')
        if _visible(el):
            el.first.fill(val)
            _record(results, fields, aid, aid, 'credential', 'filled', source='keys.env')
    if not creating:
        return None
    boxes = page.locator('[data-automation-id="createAccountCheckbox"], input[type=checkbox]')
    for i in range(boxes.count()):
        box = boxes.nth(i)
        label = (box.evaluate('e => (e.closest("form") || e.parentElement.parentElement.parentElement).innerText || ""') or '').strip()[:2000]
        if apply.restricted(label):
            _record(results, fields, 'account_terms', label, 'checkbox', 'failed', error='restricted attestation')
            return []
        if not re.search(r'terms (of use|& conditions|and conditions)|privacy (notice|policy)|i agree|may contact me about (the )?(roles?|jobs?|positions?|opportunities)', label, re.I):
            _record(results, fields, 'account_terms', label, 'checkbox', 'failed', error='unknown account consent')
            return []
        try:
            if not box.is_checked():
                box.check(force=True, timeout=4000)
        except Exception:
            box.evaluate('e => e.click()')
        _record(results, fields, 'account_terms', label[:300], 'checkbox', 'filled', 'checked', source='generic_terms')
        break
    return None


def _auth(page, job, prof, keys, conn, results, fields):
    tenant = _tenant(job)
    if not keys.get('WORKDAY_EMAIL') or not keys.get('WORKDAY_PASSWORD'):
        return ['Workday credentials missing from keys.env']
    # Already signed in on this tenant (second job in the same run): the flow opens straight to My Information.
    for _ in range(16):
        step = _current_step(page)
        if step and not re.search(r'Create Account|Sign In', step, re.I):
            return []
        if _visible(page.locator('[data-automation-id="SignInWithEmailButton"], [data-automation-id="email"], [data-automation-id="applyManually"], [data-automation-id="createAccountLink"]')):
            break
        page.wait_for_timeout(500)
    if not _visible(page.locator('[data-automation-id="SignInWithEmailButton"]')):
        page.locator('[data-automation-id="applyManually"]').wait_for(timeout=15000)
    if _click(page, '[data-automation-id="applyManually"]'):
        page.wait_for_load_state('domcontentloaded')
    for _ in range(40):  # signed-in sessions skip straight to the application
        step = _current_step(page)
        if step and not re.search(r'Create Account|Sign In', step, re.I):
            return []
        if _visible(page.locator('[data-automation-id="SignInWithEmailButton"], [data-automation-id="email"]')):
            break
        page.wait_for_timeout(500)
    if not _click(page, '[data-automation-id="SignInWithEmailButton"]') and not _visible(page.locator('[data-automation-id="email"]')):
        return ['Email sign-in selector unavailable']
    row = conn.execute('SELECT status FROM workday_accounts WHERE tenant=?', (tenant,)).fetchone()
    known = bool(row and row[0] in ('created', 'verified'))
    page.wait_for_timeout(1000)
    create_form = _visible(page.locator('[data-automation-id="verifyPassword"]'))
    if known and create_form:
        _click(page, '[data-automation-id="signInLink"]')
        page.wait_for_timeout(1000)
    elif not known and not create_form:
        _click(page, '[data-automation-id="createAccountLink"]')
        page.wait_for_timeout(1000)
    creating = _visible(page.locator('[data-automation-id="verifyPassword"]'))
    if creating:
        _account(conn, tenant, 'creating')
    err = _fill_credentials(page, keys, creating, results, fields)
    if err is not None:
        return err
    submit_selector = '[data-automation-id="createAccountSubmitButton"]' if creating else '[data-automation-id="signInSubmitButton"]'
    if not _click(page, submit_selector):
        return ['Account submit selector unavailable']
    page.wait_for_timeout(2500)
    body = page.inner_text('body')[:3000]
    if creating and re.search(r'already (exists|in use|registered|been registered)|account (with this email|for this email) (already )?exists', body, re.I):
        _click(page, '[data-automation-id="signInLink"]')
        page.wait_for_timeout(1000)
        creating = False
        err = _fill_credentials(page, keys, False, results, fields)
        if err is not None:
            return err
        _click(page, '[data-automation-id="signInSubmitButton"]')
        page.wait_for_timeout(2500)
        body = page.inner_text('body')[:3000]
    if not creating and re.search(r'(account|email)[^.]{0,60}(not (yet )?(been )?verified|not (yet )?activated|needs? (to be )?verif)|verify your (account|email) (before|to)', body, re.I):
        # Unverified account whose original link expired: ask Workday for a new one and follow it.
        if _frame_button(page, re.compile(r'resend', re.I)):
            page.wait_for_timeout(2000)
            link = verification_link(keys)
            if link:
                page.goto(link, wait_until='domcontentloaded')
                _account(conn, tenant, 'verified')
                return [('Workday account just verified - retry next run', 'email_verification')]
        _account(conn, tenant, 'email_verification')
        return [('Email verification required', 'email_verification')]
    if re.search(r'incorrect password|invalid (username|email|password)|unable to sign', body, re.I):
        return ['Workday sign-in failed']
    if _visible(page.locator(submit_selector)) and not re.search(r'verif(?:y|ication)[^.]{0,40}(?:email|account)|check your (?:email|inbox)|sent (?:you )?an email', body, re.I):
        return ['Account form validation failed']
    if re.search(r'verif(?:y|ication)[^.]{0,40}(?:email|account)|check your (?:email|inbox)|activation (?:email|link)|sent (?:you )?an email', body, re.I):
        link = verification_link(keys)
        if not link:
            _account(conn, tenant, 'email_verification')
            return [('Email verification required', 'email_verification')]
        page.goto(link, wait_until='domcontentloaded')
        _account(conn, tenant, 'verified')
    elif creating:
        if re.search(r'Create Account/Sign In', _current_step(page), re.I):
            return ['Account did not advance']
        _account(conn, tenant, 'created')
    return []


def _upload(page, prof, results, fields):
    resume = Path(prof['resume'])
    apply.log(f"upload file: {apply.attachment_details(resume)}")
    if not resume.is_file():
        return [f'Resume path is not a file: {resume}']
    try:
        page.locator('[data-automation-id="file-upload-input-ref"], [data-automation-id="file-upload-item"]').first.wait_for(state='attached', timeout=10000)
    except Exception:
        pass
    items = page.locator('[data-automation-id="file-upload-item"]')
    if items.count() or resume.name in page.inner_text('body'):
        # Drafts from earlier attempts can hold several copies: keep exactly one.
        for _ in range(10):
            if items.count() <= 1:
                break
            btn = items.last.locator('[data-automation-id="delete-file"], button[aria-label*="Delete" i], button[aria-label*="Remove" i]')
            if not btn.count():
                return [f'{items.count()} resume copies attached and the extras cannot be removed']
            btn.first.click()
            page.wait_for_timeout(1200)
        _record(results, fields, 'resume', 'Resume', 'file', 'filled', str(resume), source='profile')
        return []  # already attached on this candidate account
    slot = page.locator('[data-automation-id="file-upload-input-ref"]')
    if not slot.count():
        return ['Resume upload selector unavailable']
    slot.first.set_input_files(str(resume))
    page.wait_for_timeout(2000)
    _record(results, fields, 'resume', 'Resume', 'file', 'filled', str(resume), source='profile')
    return []


def _experience_blockers(page, prof):
    """Fail closed when Workday's parsed entry cards cannot be checked."""
    cards = page.locator('[data-automation-id="workExperienceItem"], [data-automation-id="educationItem"]')
    if not cards.count():
        return []  # nothing parsed; resume is attached. Required sections surface as page errors on Save.
    evidence = smart.grounded_resume_text(prof).lower()
    for block in (prof.get('employment'), prof.get('education')):
        if isinstance(block, dict):
            evidence += ' ' + ' '.join(str(v).lower() for v in block.values())
    blockers = []
    for card in cards.all():
        entry = card.inner_text().lower()
        values = [v.strip() for v in re.split(r'\n+', entry) if len(v.strip()) >= 4
                  and not re.fullmatch(r'edit|delete|remove|work experience|education', v.strip())]
        if not values or any(v not in evidence for v in values):
            blockers.append('Parsed Work Experience or Education entry is not verified')
    return blockers


def _screenshot(page, job):
    apply.PROOF.mkdir(exist_ok=True)
    slug = re.sub(r'[^a-z0-9]+', '_', f"{job.get('company','')}_{job.get('job_id','')}", flags=re.I).strip('_')[:90]
    path = apply.PROOF / f'workday_{slug}_{dt.datetime.now():%Y%m%d_%H%M%S}.png'
    mask = [page.locator(f'[data-automation-id="{aid}"]') for aid in ('email', 'password', 'verifyPassword')]
    mask.append(page.locator('input[type="email"]'))
    wd_email = apply.load_keys().get('WORKDAY_EMAIL')
    if wd_email:
        mask.append(page.get_by_text(wd_email, exact=True))
    page.screenshot(path=str(path), full_page=True, mask=mask, mask_color='#000000')
    return path


def _tick(page, el):
    """Workday checkboxes ignore synthetic checks; click the visible label, then fall back."""
    for how in ('label', 'click', 'force', 'js'):
        try:
            if el.is_checked():
                return True
            if how == 'label':
                ident = el.get_attribute('id', timeout=2000)
                lab = page.locator(f'label[for="{ident}"]') if ident else page.locator('label').filter(has=el)
                if lab.count():
                    lab.first.click(timeout=3000)
            elif how == 'click':
                el.click(timeout=3000)
            elif how == 'force':
                el.check(force=True, timeout=3000)
            else:
                el.evaluate('e => e.click()')
            page.wait_for_timeout(300)
        except Exception:
            continue
    try:
        return el.is_checked()
    except Exception:
        return False


def _stable(page, timeout=12000):
    """Wait until the count of form controls stops changing (questionnaires render async)."""
    js = "() => document.querySelectorAll('input,textarea,select,button[aria-haspopup]').length"
    last, same, waited = -1, 0, 0
    while waited < timeout:
        n = page.evaluate(js)
        same = same + 1 if n == last else 0
        if same >= 3 and n > 0:
            return
        last = n
        page.wait_for_timeout(500)
        waited += 500


def _settle(page, timeout=30000):
    """Wait for Workday's SPA to finish a transition (spinner gone, a step rendered)."""
    try:
        page.wait_for_load_state('networkidle', timeout=timeout)
    except Exception:
        pass
    try:
        page.locator('[data-automation-id="progressBarActiveStep"], [data-automation-id="applyFlowPage"]').first.wait_for(timeout=timeout)
    except Exception:
        pass
    page.wait_for_timeout(800)


def _current_step(page):
    active = page.locator('[data-automation-id="progressBarActiveStep"]')
    return active.first.inner_text().strip() if active.count() else ''


def _review_missing(page, prof):
    body = re.sub(r'\s+', ' ', page.inner_text('body')).lower()
    return [f'Review read-back: {key}' for key in ('first_name', 'last_name', 'email')
            if prof.get(key) and str(prof[key]).lower() not in body]


WOTC = re.compile(r'work opportunity tax credit|tax credit (screening|questionnaire|programs?)', re.I)
OPT_OUT = re.compile(r'opt[ -]?out|do not wish to (participate|continue|answer)|decline to (participate|answer)|skip (this )?(questionnaire|survey)|no,? thank', re.I)


def _all_text(page):
    out = []
    for fr in page.frames:
        try:
            out.append(fr.locator('body').inner_text(timeout=3000))
        except Exception:
            pass
    return '\n'.join(out)


def _frame_button(page, pattern):
    """Click the first visible button/link matching pattern in the page or any frame."""
    for fr in page.frames:
        loc = fr.get_by_role('button', name=pattern).or_(fr.get_by_role('link', name=pattern))
        try:
            for i in range(loc.count()):
                if loc.nth(i).is_visible():
                    loc.nth(i).click()
                    return True
        except Exception:
            continue
    return False


def _wotc_opt_out(page, results, fields):
    """Optional WOTC tax-credit questionnaire (e.g. Centene's 'Take Assessment' step): open it and opt out.
    Opting out is the documented choice and does not affect the application. Returns a blocker list."""
    shots = apply.ROOT / 'proof'
    stamp = time.strftime('%Y%m%d_%H%M%S')
    try:
        try:
            with page.context.expect_page(timeout=12000) as info:
                _frame_button(page, re.compile(r'^Take Assessment$', re.I))
            win = info.value
        except Exception:
            win = page
        win.wait_for_load_state('domcontentloaded', timeout=30000)
        for attempt in range(20):
            win.wait_for_timeout(1500)
            frames = [win] + [f for f in win.frames if f != win.main_frame]
            hit = None
            for fr in frames:
                loc = fr.get_by_role('button', name=OPT_OUT).or_(fr.get_by_role('link', name=OPT_OUT)).or_(fr.locator('input[type=button], input[type=submit]').filter(has_text=OPT_OUT))
                try:
                    if loc.count() and loc.first.is_visible():
                        hit = loc.first
                        break
                except Exception:
                    continue
            if hit is not None:
                hit.click()
                win.wait_for_timeout(2000)
                # some vendors confirm the opt-out
                for fr in [win] + [f for f in win.frames if f != win.main_frame]:
                    try:
                        conf = fr.get_by_role('button', name=re.compile(r'^(yes|confirm|ok|continue|submit)\b', re.I))
                        if conf.count() and conf.first.is_visible():
                            conf.first.click(); win.wait_for_timeout(1500); break
                    except Exception:
                        pass
                break
        try:
            win.screenshot(path=str(shots / f'wotc_{stamp}.png'), full_page=True)
        except Exception:
            pass
        if win is not page:
            try:
                if not win.is_closed():
                    win.wait_for_timeout(1500)
                    if not win.is_closed():
                        win.close()
            except Exception:
                pass
        page.bring_to_front()
        _settle(page)
        if hit is None:
            return ['WOTC questionnaire: opt-out control not found']
        _record(results, fields, 'wotc_opt_out', 'Work Opportunity Tax Credit questionnaire (optional)', 'click', 'filled', 'opted out', source='wotc_optional')
        return []
    except Exception as exc:
        return [f'WOTC questionnaire: {_safe_error(exc)}']


def apply_job(page_or_ctx, job, prof, bank, submit=False):
    """Return (status, note) for a context or (page, status, note) for staging."""
    staging = not hasattr(page_or_ctx, 'goto')
    # apply.py may run as __main__, so the 'apply' module this file imported has its own globals.
    apply.US_CITIZEN = bool(prof.get('us_citizen'))
    apply._set_current_job(job)
    page = page_or_ctx.new_page() if staging else page_or_ctx
    with apply.db() as conn:
        attempt = apply._new_attempt(conn, job, prof)
        results, fields, missing, visited = [], [], [], []
        try:
            page.goto(job['url'], wait_until='domcontentloaded', timeout=45000)
            page.wait_for_selector('[data-automation-id="jobPostingPage"], [data-automation-id="applyFlowPage"]', timeout=30000)
            if not _visible(page.locator('[data-automation-id="applyFlowPage"]')):
                page.get_by_role('button', name='Apply', exact=True).wait_for(timeout=30000)
                _button(page, r'^Apply$')
            page.wait_for_timeout(500)
            missing += _auth(page, job, prof, apply.load_keys(), conn, results, fields)
            if not missing:
                _settle(page)
                if re.search(r'Create Account|Sign In', _current_step(page), re.I):
                    body = page.inner_text('body')[:1500]
                    missing.append('Still on sign-in after auth: ' + re.sub(r'\s+', ' ', body)[:200])
            if missing or any(r['status'] == 'failed' for r in results):
                raise StopIteration
            retried = {}
            for index in range(12):
                if re.search(r'Something went wrong', page.inner_text('body')[:3000]):
                    page.reload(wait_until='domcontentloaded')
                    _settle(page)
                    if re.search(r'Something went wrong', page.inner_text('body')[:3000]):
                        missing.append('Workday server error (retry next run)')
                        break
                step = _current_step(page)
                if step and step not in visited:
                    visited.append(step)
                if re.search(r'Application Submitted', page.inner_text('body')[:600], re.I):
                    break
                if re.search(r'My Experience', step, re.I) and not any(r['key'] == 'resume' for r in results):
                    missing += _upload(page, prof, results, fields)
                    if not missing:
                        missing += _experience_blockers(page, prof)
                if re.search(r'Assessment', step, re.I) and not any(r['key'] == 'wotc_opt_out' for r in results):
                    for _ in range(16):  # the questionnaire blurb renders after the step header
                        if WOTC.search(_all_text(page)):
                            break
                        page.wait_for_timeout(500)
                    if WOTC.search(_all_text(page)):
                        missing += _wotc_opt_out(page, results, fields)
                missing += _fill_page(page, job, prof, bank, results, fields, step)
                if missing or any(r['status'] == 'failed' for r in results):
                    break
                # The Review page is the only point at which Submit may be visible.
                if _visible(page.get_by_role('button', name=re.compile(r'^Submit$', re.I))):
                    if not re.search(r'Review', step, re.I):
                        missing.append('Review step not verified')
                    else:
                        missing += _review_missing(page, prof)
                    break
                nxt = '[data-automation-id="pageFooterNextButton"], [data-automation-id="bottom-navigation-next-button"]'
                try:
                    page.locator(nxt).first.wait_for(state='visible', timeout=15000)
                    page.wait_for_function('s => { const b=document.querySelector(s); return b && !b.disabled && b.getAttribute("aria-disabled")!=="true"; }', arg=nxt, timeout=15000)
                except Exception:
                    pass
                if not (_click(page, nxt)
                        or _button(page, r'^Save and Continue$|^Next$|^Continue$')):
                    missing.append('Next page selector unavailable')
                    break
                _settle(page)
                for click_try in range(2):
                    for _ in range(90):  # Save and Continue can take a while to commit
                        if _current_step(page) != step or _page_errors(page):
                            break
                        page.wait_for_timeout(500)
                    if _current_step(page) != step or _page_errors(page) or click_try:
                        break
                    _click(page, nxt)  # one retry: the first click is sometimes swallowed
                _settle(page)
                if re.search(r'Something went wrong', page.inner_text('body')[:3000]) and not retried.get(step):
                    retried[step] = True
                    page.reload(wait_until='domcontentloaded')
                    _settle(page)
                    continue
                errs = _page_errors(page)
                if errs or _current_step(page) == step:
                    missing.extend(errs or [f'Page did not advance past {step or "current step"}'])
                    break
            else:
                missing.append('Too many pages without reaching Review')
            if not missing and not any(re.search(r'Review', v, re.I) for v in visited + [_current_step(page)]):
                missing.append('Review step not reached')
            outcome, note = apply._record_attempt(conn, attempt, job, results, missing, fields, prof=prof, bank=bank)
            note += f" [pages: {', '.join(visited)}]"
            shot = _screenshot(page, job)
            note += f' [proof: {shot}]'
            if outcome == 'blocked':
                return (page, 'needs_you', note) if staging else ('needs_you', note)
            if not submit:
                return (page, 'staged', note) if staging else ('ready', note)
            if any(r.get('source') == 'draft' for r in results) and not prof.get('permissions', {}).get('submit', {}).get('scope', {}).get('allow_drafts'):
                return (page, 'staged', 'Drafts require review') if staging else ('ready', 'Drafts require review')
            allowed, reason = policy.check('submit', {**job, 'submitted_today': prof.get('_submitted_today', 0)}, prof)
            if not allowed:
                state.record_field(conn, attempt, '__permission__', 'Submit permission', True, 'policy', 'failed', error=reason)
                state.set_state(conn, attempt, 'blocked')
                return (page, 'needs_you', reason) if staging else ('needs_you', reason)
            state.begin_submit(conn, job['url'])
            if not _button(page, r'^Submit$'):
                state.set_state(conn, attempt, 'uncertain')
                return (page, 'unconfirmed', 'Submit selector missing') if staging else ('unconfirmed', 'Submit selector missing')
            try:
                page.get_by_text(re.compile(r'Application Submitted|Thank you for applying', re.I)).first.wait_for(timeout=30000)
                state.set_state(conn, attempt, 'confirmed', apply.followup_days())
                return (page, 'applied', note) if staging else ('applied', note)
            except Exception:
                state.set_state(conn, attempt, 'uncertain')
                return (page, 'unconfirmed', note) if staging else ('unconfirmed', note)
        except StopIteration:
            outcome, note = apply._record_attempt(conn, attempt, job, results, missing, fields, prof=prof, bank=bank)
            note += f" [pages: {', '.join(visited)}]"
            try:
                note += f' [proof: {_screenshot(page, job)}]'
            except Exception:
                pass
            return (page, 'needs_you', note) if staging else ('needs_you', note)
        except Exception as exc:
            if os.environ.get('RA_WD_DEBUG'):
                import traceback, sys as _s
                traceback.print_exc(file=_s.stderr)
            error = 'Automation error: ' + _safe_error(exc)
            if conn.execute('SELECT state FROM attempts WHERE id=?', (attempt,)).fetchone()[0] == 'new':
                apply._record_attempt(conn, attempt, job, results, missing, fields, error=error, prof=prof, bank=bank)
            try:
                error += f' [proof: {_screenshot(page, job)}]'
            except Exception:
                pass
            return (page, 'needs_you', error) if staging else ('needs_you', error)
        finally:
            if not staging:
                page.close()


def discover_tenant(company, conn=None):
    """Probe public Workday boards; cache verified hits and misses for 14 days."""
    own = conn is None
    if own:
        conn = apply.db()
    try:
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=14)).isoformat()
        row = conn.execute('SELECT host,tenant,site,checked_at FROM workday_tenants WHERE company=?', (company,)).fetchone()
        if row and row[3] >= cutoff:
            return dict(host=row[0], tenant=row[1], site=row[2]) if row[0] else None
        slug = re.sub(r'[^a-z0-9]', '', company.lower())
        candidates = [slug, re.sub(r'\b(inc|llc|insurance|company|corporation)\b', '', company.lower()).replace(' ', '')]
        known = []
        try:
            import json
            listed = json.loads(apply.BOARDS.read_text()).get('workday', {})
            if company in listed:
                known.append(listed[company])
        except (OSError, ValueError):
            pass
        guesses = known + [dict(host=f'{tenant}.{wd}', tenant=tenant, site=site)
                           for tenant in dict.fromkeys(x for x in candidates if x)
                           for wd in ('wd1', 'wd3', 'wd5', 'wd12')
                           for site in dict.fromkeys(('External', 'Careers', tenant, tenant.title() + '_Careers'))]
        found = None
        company_key = apply.ckey(company)
        for cfg in guesses:
            host, tenant, site = cfg['host'], cfg['tenant'], cfg['site']
            base = f'https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}'
            try:
                data = apply._wd_post(base + '/jobs',
                                      {'limit': 20, 'offset': 0, 'searchText': '', 'appliedFacets': {}})
                posts = data.get('jobPostings') or []
                if not posts or len(company_key) < 5:
                    continue
                public = str(data.get('company') or '') + ' ' + ' '.join(str(p.get('company') or '') for p in posts)
                if company_key not in apply.ckey(public):
                    for post in posts[:2]:
                        path = post.get('externalPath')
                        if path:
                            info = apply.get_json_h(base + path).get('jobPostingInfo') or {}
                            public += ' ' + str(info.get('jobDescription') or '') + ' ' + str(info.get('hiringOrganization') or '')
                if company_key in apply.ckey(public):
                    found = dict(host=host, tenant=tenant, site=site)
                    break
            except Exception:
                pass
        conn.execute('INSERT INTO workday_tenants(company,host,tenant,site,checked_at) VALUES (?,?,?,?,?) '
                     'ON CONFLICT(company) DO UPDATE SET host=excluded.host,tenant=excluded.tenant,site=excluded.site,checked_at=excluded.checked_at',
                     (company, *(found.values() if found else (None, None, None)), state.now()))
        conn.commit()
        return found
    finally:
        if own:
            conn.close()
