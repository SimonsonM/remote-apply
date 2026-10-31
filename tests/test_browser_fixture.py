"""Localhost-only browser checks for durable attempts and submit safety."""
import json
import sqlite3
import threading
import hashlib
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

import apply
import state
import answer_store

def approve_fixture_bank():
    with apply.db() as conn:
        answer_store.import_bank(conn, apply.ANSWERS)
        answer_store.approve(conn, [r[0] for r in conn.execute("SELECT id FROM answer_store WHERE source='answers.json' AND status='pending'")])

pytestmark = pytest.mark.browser

FORM = '''<!doctype html><title>Fixture application</title>
<form action="/submit" method="post" enctype="multipart/form-data">
<label for="name">Full name *</label><input id="name" name="name" required>
<label for="country">Country *</label><select id="country" name="country" required>
<option value="">Choose</option><option>USA</option></select>
<fieldset><legend>Work mode *</legend>
<label><input type="radio" name="mode" value="Remote" required>Remote</label>
<label><input type="radio" name="mode" value="Office" required>Office</label></fieldset>
<label for="story">Why join us? *</label><textarea id="story" name="story" required></textarea>
<label for="bad">Unavailable choice *</label><select id="bad" name="bad" required>
<option value="">Choose</option><option>Other</option></select>
<label for="cv">Resume *</label><input id="cv" name="cv" type="file" required>
<label for="cleared">Cleared field</label><input id="cleared" name="cleared">
<script>document.getElementById('cleared').addEventListener('input', e => e.target.value = '')</script>
<button type="submit">Submit</button></form>'''
BOILERPLATE = '<p>Thank you for applying</p>'


@pytest.fixture
def fixture_site():
    class Handler(BaseHTTPRequestHandler):
        posts = 0
        successful_posts = 0
        def log_message(self, *args):
            pass
        def do_GET(self):
            path = urlsplit(self.path).path
            body = ('' if path == '/no-form' else
                    '<h1>Open jobs</h1><input name="search" placeholder="Search jobs"><select name="location_filter"><option>All</option></select>' if path == '/listing' else
                    '<iframe src="/recaptcha/anchor"></iframe>' if path == '/doctor-captcha' else
                    '<h1>Blank result</h1>' if path == '/blank' else
                    BOILERPLATE if path == '/done' else
                    '<h1>Application sent</h1>' if path == '/thanks-empty' else
                    '<h1>Thank you for applying</h1>' if path == '/thanks' else
                    FORM + BOILERPLATE if path == '/boilerplate' else FORM).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            assert urlsplit(self.path).path == '/submit'
            self.rfile.read(int(self.headers['Content-Length']))
            Handler.posts += 1
            if self.headers.get('Referer', '').endswith('/reject'):
                body = FORM.replace('<form ', '<p role="alert">Validation failed</p><form ', 1).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.headers.get('Referer', '').endswith('/no-text'):
                Handler.successful_posts += 1
                self.send_response(303)
                self.send_header('Location', '/thanks-empty')
                self.end_headers()
            elif self.headers.get('Referer', '').endswith('/boilerplate'):
                Handler.successful_posts += 1
                self.send_response(303)
                self.send_header('Location', '/done')
                self.end_headers()
            elif self.headers.get('Referer', '').endswith('/blank-submit'):
                Handler.successful_posts += 1
                self.send_response(303)
                self.send_header('Location', '/blank')
                self.end_headers()
            else:
                Handler.successful_posts += 1
                self.send_response(303)
                self.send_header('Location', '/thanks')
                self.end_headers()
    try:
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    except PermissionError as exc:
        pytest.skip(f'localhost socket unavailable: {exc}')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', Handler
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        try:
            instance = pw.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f'Chromium cannot launch: {exc}')
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def case(tmp_path, monkeypatch, fixture_site):
    base, handler = fixture_site
    monkeypatch.setattr(apply, 'DB', tmp_path / 'applications.sqlite3')
    monkeypatch.setattr(apply, 'CSV_OUT', tmp_path / 'applications.csv')
    monkeypatch.setattr(apply, 'PROOF', tmp_path / 'proof')
    profile = tmp_path / 'profile.json'
    resume = tmp_path / 'resume.pdf'
    resume.write_bytes(b'fixture resume')
    profile.write_text(json.dumps({'name': 'Test Person', 'resume': str(resume),
                                   'draft_answers': False, 'cover_letters': False,
                                   'manual_sources': [], 'permissions': {}}))
    answers = tmp_path / 'answers.json'
    bank = [{'match': '^Country', 'answer': 'USA'},
            {'match': '^Work mode', 'answer': 'Remote'},
            {'match': '^Unavailable choice', 'answer': 'Missing'},
            {'match': '^Cleared field', 'answer': 'will clear'}]
    answers.write_text(json.dumps({'answers': bank}))
    monkeypatch.setattr(apply, 'CONFIG', profile)
    monkeypatch.setattr(apply, 'ANSWERS', answers)
    url = base + '/apply'
    job = {'url': url, 'apply_url': url, 'company': 'Fixture Co', 'title': 'Tester', 'source': 'lever'}
    with apply.db() as conn:
        conn.execute('INSERT INTO jobs(url,company,title,apply_url,source,status) VALUES (?,?,?,?,?,?)',
                     (url, job['company'], job['title'], url, job['source'], 'new'))
        conn.commit()
    approve_fixture_bank()
    return job, apply.prepare_profile(json.loads(profile.read_text())), bank, handler


def ready_bank(case):
    job, prof, bank, handler = case
    bank[2]['answer'] = 'Other'
    bank.pop()  # the optional cleared field is covered separately
    bank.append({'match': '^Why join', 'answer': 'I can test'})
    apply.ANSWERS.write_text(json.dumps({'answers': bank}))
    with apply.db() as conn:
        conn.execute("UPDATE answer_store SET approved=0,status='rejected' WHERE match_regex='^Cleared field'")
        conn.commit()
    approve_fixture_bank()
    return job, prof, bank, handler


def latest_state(conn):
    return conn.execute('SELECT state FROM attempts ORDER BY id DESC LIMIT 1').fetchone()[0]


def test_real_fill_results_and_readback(case, browser):
    job, prof, bank, handler = case
    page, status, _ = apply.stage_job(job, prof, bank, browser)
    page.close()
    assert status == 'needs_you'
    with sqlite3.connect(apply.DB) as conn:
        rows = conn.execute('SELECT label,status,error FROM field_results').fetchall()
        assert any(l == 'Full name' and s == 'filled' for l, s, _ in rows)
        assert any(l == 'Country' and s == 'filled' for l, s, _ in rows)
        assert any(l == 'Work mode' and s == 'filled' for l, s, _ in rows)
        assert any(l == 'Resume' and s == 'filled' for l, s, _ in rows)
        assert any(l == 'Why join us?' and s == 'unanswered' for l, s, _ in rows)
        assert any(l == 'Unavailable choice' and s == 'failed' and e for l, s, e in rows)
        assert any(l == 'Cleared field' and s == 'failed' and 'readback' in e for l, s, e in rows)
        assert latest_state(conn) == 'blocked'
    assert handler.posts == 0


def test_answer_resume_no_post(case, browser):
    job, prof, bank, handler = ready_bank(case)
    bank.pop()  # leave textarea unanswered for the staged attempt
    apply.ANSWERS.write_text(json.dumps({'answers': bank}))
    with apply.db() as conn:
        conn.execute("UPDATE answer_store SET approved=0,status='rejected' WHERE match_regex='^Why join'")
        conn.commit()
    page, status, _ = apply.stage_job(job, prof, bank, browser)
    page.close()
    assert status == 'needs_you'
    apply.answer_cmd(job['url'], 'Why join', 'I can test')
    apply.resume_cmd(job['url'], ctx=browser)
    with sqlite3.connect(apply.DB) as conn:
        assert latest_state(conn) == 'ready_for_review'
        latest = conn.execute('SELECT max(id) FROM attempts').fetchone()[0]
        rows = conn.execute('SELECT label,status FROM field_results WHERE attempt_id=?', (latest,)).fetchall()
        assert ('Why join us?', 'filled') in rows
        assert ('Full name', 'filled') in rows
        assert ('Resume', 'filled') in rows
        assert not state.open_blockers(conn, job['url'])
    assert handler.posts == 0


def test_policy_denial_no_post(case, browser):
    job, prof, bank, handler = ready_bank(case)
    status, note = apply.process(job, prof, bank, True, browser)
    assert status == 'needs_you' and 'permission' in note
    with sqlite3.connect(apply.DB) as conn:
        assert latest_state(conn) == 'blocked'
    assert handler.posts == 0


def test_permitted_submit_and_duplicate(case, browser):
    job, prof, bank, handler = ready_bank(case)
    prof['permissions'] = {'submit': {'allowed': True, 'scope': {'companies': ['Fixture Co'], 'daily_cap': 2},
                                      'expires': '2999-01-01T00:00:00+00:00'}}
    status, _ = apply.process(job, prof, bank, True, browser, confirmation_timeout=1000)
    assert status == 'applied'
    with sqlite3.connect(apply.DB) as conn:
        assert latest_state(conn) == 'confirmed'
    assert handler.posts == handler.successful_posts == 1
    status, _ = apply.process(job, prof, bank, True, browser, confirmation_timeout=1000)
    assert status == 'needs_you'
    assert handler.posts == 1
    with sqlite3.connect(apply.DB) as conn:
        assert conn.execute("SELECT count(*) FROM attempts WHERE state='confirmed'").fetchone()[0] == 1
        assert latest_state(conn) == 'blocked'


def test_same_form_response_is_uncertain_and_retry_refused(case, browser):
    job, prof, bank, handler = ready_bank(case)
    job['apply_url'] = job['url'].replace('/apply', '/reject')
    prof['permissions'] = {'submit': {'allowed': True, 'scope': {'companies': ['Fixture Co']},
                                      'expires': '2999-01-01T00:00:00+00:00'}}
    status, _ = apply.process(job, prof, bank, True, browser, confirmation_timeout=400)
    assert status == 'unconfirmed'
    with sqlite3.connect(apply.DB) as conn:
        assert latest_state(conn) == 'uncertain'
        assert conn.execute('SELECT status FROM jobs WHERE url=?', (job['url'],)).fetchone()[0] == 'unconfirmed'
    assert handler.posts == 1 and handler.successful_posts == 0
    status, _ = apply.process(job, prof, bank, True, browser, confirmation_timeout=400)
    assert status == 'needs_you'
    assert handler.posts == 1


def test_changed_url_without_confirmation_is_uncertain(case, browser):
    job, prof, bank, handler = ready_bank(case)
    job['apply_url'] = job['url'].replace('/apply', '/no-text')
    prof['permissions'] = {'submit': {'allowed': True, 'scope': {'companies': ['Fixture Co']},
                                      'expires': '2999-01-01T00:00:00+00:00'}}
    status, _ = apply.process(job, prof, bank, True, browser, confirmation_timeout=400)
    assert status == 'unconfirmed'
    with sqlite3.connect(apply.DB) as conn:
        assert latest_state(conn) == 'uncertain'
    assert handler.posts == handler.successful_posts == 1


def test_boilerplate_text_with_url_change_is_uncertain(case, browser):
    job, prof, bank, handler = ready_bank(case)
    job['apply_url'] = job['url'].replace('/apply', '/boilerplate')
    prof['permissions'] = {'submit': {'allowed': True, 'scope': {'companies': ['Fixture Co']},
                                      'expires': '2999-01-01T00:00:00+00:00'}}
    status, _ = apply.process(job, prof, bank, True, browser, confirmation_timeout=400)
    assert status == 'unconfirmed'
    with sqlite3.connect(apply.DB) as conn:
        assert latest_state(conn) == 'uncertain'
        assert conn.execute('SELECT status FROM jobs WHERE url=?', (job['url'],)).fetchone()[0] == 'unconfirmed'
        assert conn.execute("SELECT count(*) FROM attempts WHERE state='confirmed'").fetchone()[0] == 0
    assert handler.posts == handler.successful_posts == 1


def test_form_gone_without_confirmation_text_is_uncertain(case, browser):
    job, prof, bank, handler = ready_bank(case)
    job['apply_url'] = job['url'].replace('/apply', '/blank-submit')
    prof['permissions'] = {'submit': {'allowed': True, 'scope': {'companies': ['Fixture Co']},
                                      'expires': '2999-01-01T00:00:00+00:00'}}
    status, _ = apply.process(job, prof, bank, True, browser, confirmation_timeout=400)
    assert status == 'unconfirmed'
    with sqlite3.connect(apply.DB) as conn:
        assert latest_state(conn) == 'uncertain'
        assert conn.execute('SELECT status FROM jobs WHERE url=?', (job['url'],)).fetchone()[0] == 'unconfirmed'
        assert conn.execute("SELECT count(*) FROM attempts WHERE state='confirmed'").fetchone()[0] == 0
    assert handler.posts == handler.successful_posts == 1


def test_selector_timeout_blocks(case, browser):
    job, prof, bank, handler = case
    job['apply_url'] = job['url'].replace('/apply', '/no-form')
    page, status, _ = apply.stage_job(job, prof, bank, browser, selector_timeout=100)
    page.close()
    assert status == 'needs_you'
    with sqlite3.connect(apply.DB) as conn:
        assert latest_state(conn) == 'blocked'
        assert state.open_blockers(conn, job['url'])
    assert handler.posts == 0


def test_preclick_boilerplate_never_confirms(case, browser):
    job, prof, bank, handler = ready_bank(case)
    job['apply_url'] = job['url'].replace('/apply', '/reject')
    page, status, _ = apply.stage_job(job, prof, bank, browser)
    assert status == 'staged'
    page.evaluate("document.body.insertAdjacentHTML('afterbegin', '<p>Thank you for your interest</p>')")
    before = page.inner_text('body')
    baseline = (page.url, hashlib.sha256(before.encode()).hexdigest(), before)
    page.get_by_role('button', name='Submit').click()
    assert not apply.confirm_submission(page, *baseline, timeout=100)
    assert handler.posts == 1 and handler.successful_posts == 0
    page.close()


def test_human_submit_confirms_with_evidence(case, browser):
    job, prof, bank, handler = ready_bank(case)
    page, status, _ = apply.stage_job(job, prof, bank, browser)
    assert status == 'staged'
    before = page.inner_text('body')
    baseline = (page.url, hashlib.sha256(before.encode()).hexdigest(), before)
    page.get_by_role('button', name='Submit').click()
    page.wait_for_url('**/thanks')
    with apply.db() as conn:
        assert apply.confirm_human(conn, job, page, baseline)
        assert conn.execute("SELECT state FROM attempts ORDER BY id DESC LIMIT 1").fetchone()[0] == 'confirmed'
        assert conn.execute("SELECT count(*) FROM evidence WHERE kind='human'").fetchone()[0] == 1
    assert handler.posts == 1
    page.close()


def test_prepare_run_exits_and_records(case, monkeypatch):
    job, prof, bank, handler = ready_bank(case)
    prof.update(mode='prepare', email='fixture@example.test', phone='555-0000', stage_cap=1)
    apply.CONFIG.write_text(json.dumps(prof))
    monkeypatch.setattr(apply, 'scan', lambda: None)
    monkeypatch.setattr('plan.value', lambda *args: 1)
    monkeypatch.setattr('plan.pick', lambda candidates, *args: candidates[:1])
    with apply.db() as conn:
        conn.execute("UPDATE jobs SET fit=90,ghost=0,status='new' WHERE url=?", (job['url'],))
        conn.commit()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(apply.run).result(timeout=15)
    with apply.db() as conn:
        assert conn.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1
    assert handler.posts == 0


def test_review_refills_without_post(case, browser):
    job, prof, bank, handler = ready_bank(case)
    page, status, _ = apply.stage_job(job, prof, bank, browser)
    assert status == 'staged'
    page.close()
    apply.review_cmd(ctx=browser)
    with apply.db() as conn:
        assert conn.execute("SELECT count(*) FROM attempts").fetchone()[0] == 2
        latest = conn.execute("SELECT max(id) FROM attempts").fetchone()[0]
        assert conn.execute("SELECT count(*) FROM field_results WHERE attempt_id=? AND status='filled'", (latest,)).fetchone()[0] >= 4
    assert handler.posts == 0
