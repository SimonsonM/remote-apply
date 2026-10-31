import hashlib
import json
import stat
import sqlite3

import pytest

import apply
import policy
import state
import answer_store

def approve_fixture_bank():
    with apply.db() as conn:
        answer_store.import_bank(conn, apply.ANSWERS)
        ids = [r[0] for r in conn.execute("SELECT id FROM answer_store WHERE source='answers.json' AND status='pending'")]
        answer_store.approve(conn, ids)

FIELDS = [
    dict(i=0, tag='input', type='text', name='name', value='', label='Full name *', question='', required=True, options=[]),
    dict(i=1, tag='select', type='select-one', name='country', value='', label='Country *', question='', required=True, options=['Choose', 'USA']),
    dict(i=2, tag='input', type='radio', name='mode', value='Remote', label='Remote', question='Work mode *', required=True, options=[]),
    dict(i=3, tag='input', type='radio', name='mode', value='Office', label='Office', question='Work mode *', required=True, options=[]),
    dict(i=4, tag='textarea', type='textarea', name='story', value='', label='Why join us? *', question='', required=True, options=[]),
    dict(i=5, tag='select', type='select-one', name='bad', value='', label='Unavailable choice *', question='', required=True, options=['Choose', 'Other']),
]

class FakeLocator:
    def __init__(self, page, index, option=False):
        self.page, self.index, self.option = page, index, option
        self.first = self
    def count(self): return 1
    def fill(self, value): self.page.values[self.index] = value
    def select_option(self, label):
        if label not in FIELDS[self.index]['options']: raise ValueError('option unavailable')
        self.page.values[self.index] = label
    def check(self, force=False): self.page.checked.add(self.index)
    def is_checked(self): return self.index in self.page.checked
    def input_value(self): return self.page.values.get(self.index, '')
    def evaluate(self, script): return FIELDS[self.index]['tag'].upper()
    def locator(self, selector): return FakeLocator(self.page, self.index, True)
    def inner_text(self): return self.page.values.get(self.index, '')
    def set_input_files(self, value): self.page.values[self.index] = value

class FakePage:
    def __init__(self): self.values = {}; self.checked = set(); self.url = ''
    def goto(self, url, **kwargs): self.url = url
    def wait_for_selector(self, *args, **kwargs): pass
    def wait_for_timeout(self, *args): pass
    def evaluate(self, script): return FIELDS if script == apply.FIELDS_JS else []
    def locator(self, selector):
        import re
        match = re.search(r"data-ra='(\d+)'", selector)
        return FakeLocator(self, int(match.group(1)))
    def close(self): pass

class FakeContext:
    def new_page(self): return FakePage()

@pytest.fixture
def setup(tmp_path, monkeypatch):
    url = 'http://localhost:8765/apply'
    dbpath = tmp_path / 'applications.sqlite3'
    monkeypatch.setattr(apply, 'DB', dbpath)
    monkeypatch.setattr(apply, 'CSV_OUT', tmp_path / 'applications.csv')
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'name': 'Test Person', 'resume': str(tmp_path / 'resume.pdf'), 'draft_answers': False,
                                   'cover_letters': False, 'manual_sources': [], 'permissions': {}}))
    (tmp_path / 'resume.pdf').write_bytes(b'fixture')
    answers = tmp_path / 'answers.json'
    answers.write_text(json.dumps({'answers': [
        {'match': '^Country', 'answer': 'USA'}, {'match': '^Work mode', 'answer': 'Remote'},
        {'match': '^Unavailable choice', 'answer': 'Missing'}]}))
    monkeypatch.setattr(apply, 'CONFIG', profile)
    monkeypatch.setattr(apply, 'ANSWERS', answers)
    with apply.db() as conn:
        conn.execute('INSERT INTO jobs(url,company,title,apply_url,source,status) VALUES (?,?,?,?,?,?)',
                     (url, 'Fixture Co', 'Tester', url, 'lever', 'new'))
        conn.commit()
    job = {'url': url, 'apply_url': url, 'company': 'Fixture Co', 'title': 'Tester', 'source': 'lever'}
    prof = apply.prepare_profile(json.loads(profile.read_text()))
    bank = json.loads(answers.read_text())['answers']
    approve_fixture_bank()
    return job, prof, bank, tmp_path

def test_fill_results_blockers_and_persistence(setup):
    job, prof, bank, root = setup
    page, status, note = apply.stage_job(job, prof, bank, FakeContext())
    assert status == 'needs_you'
    page.close()
    with sqlite3.connect(apply.DB) as conn:
        rows = conn.execute('SELECT label,status,error FROM field_results').fetchall()
        assert any('Full name' in l and s == 'filled' for l, s, _ in rows)
        assert any('Country' in l and s == 'filled' for l, s, _ in rows)
        assert any('Work mode' in l and s == 'filled' for l, s, _ in rows)
        assert any('Unavailable choice' in l and s == 'failed' and e for l, s, e in rows)
        assert any('Why join us' in l and s == 'unanswered' for l, s, _ in rows)
    with sqlite3.connect(apply.DB) as reopened:
        labels = [r[2] for r in state.open_blockers(reopened, job['url'])]
        assert len(labels) == 2

def test_answer_and_resume_never_submit(setup):
    job, prof, bank, root = setup
    # Use a fixture bank whose required select option is available.
    bank[-1]['answer'] = 'Other'
    apply.ANSWERS.write_text(json.dumps({'answers': bank}))
    approve_fixture_bank()
    page, status, _ = apply.stage_job(job, prof, bank, FakeContext())
    assert status == 'needs_you'
    page.close()
    apply.answer_cmd(job['url'], 'Why join', 'Because I can test')
    apply.resume_cmd(job['url'], ctx=FakeContext())
    with sqlite3.connect(apply.DB) as conn:
        attempts = conn.execute('SELECT state,note FROM attempts ORDER BY id').fetchall()
        assert attempts[-1][0] == 'ready_for_review'
        assert 'resumes attempt' in attempts[-1][1]
        latest = conn.execute('SELECT id FROM attempts ORDER BY id DESC LIMIT 1').fetchone()[0]
        rows = conn.execute('SELECT label,status FROM field_results WHERE attempt_id=?', (latest,)).fetchall()
        assert any('Why join' in l and s == 'filled' for l, s in rows)
        assert any('Full name' in l and s == 'filled' for l, s in rows)
        assert not state.open_blockers(conn, job['url'])

def test_duplicate_recovery_and_transitions(tmp_path):
    with sqlite3.connect(tmp_path / 'test.sqlite3') as conn:
        state.migrate(conn)
        a = state.create_attempt(conn, 'http://localhost/job')
        with pytest.raises(ValueError): state.set_state(conn, a, 'confirmed')
        state.set_state(conn, a, 'ready_for_review')
        state.begin_submit(conn, 'http://localhost/job')
        with pytest.raises(ValueError): state.set_state(conn, a, 'blocked')
        with pytest.raises(state.DuplicateSubmission): state.begin_submit(conn, 'http://localhost/job')
        state.recover(conn)
        assert conn.execute('SELECT state,note FROM attempts WHERE id=?', (a,)).fetchone() == ('uncertain', 'crash/restart during submit')
        with pytest.raises(state.DuplicateSubmission): state.begin_submit(conn, 'http://localhost/job')
        with pytest.raises(ValueError): state.set_state(conn, a, 'confirmed')
        state.reconcile(conn, a, False)
        assert conn.execute('SELECT state FROM attempts WHERE id=?', (a,)).fetchone()[0] == 'abandoned'

def test_policy_and_denied_submit(setup):
    job, prof, bank, root = setup
    bank[-1]['answer'] = 'Other'; bank.append({'match': '^Why join', 'answer': 'Testing'})
    apply.ANSWERS.write_text(json.dumps({'answers': bank}))
    approve_fixture_bank()
    assert policy.check('submit', job, prof)[0] is False
    status, note = apply.process(job, prof, bank, True, FakeContext())
    assert status == 'needs_you' and 'permission' in note
    prof['permissions'] = {'submit': {'allowed': True, 'scope': {'companies': ['Fixture Co'], 'daily_cap': 2}, 'expires': '2999-01-01T00:00:00+00:00'}}
    assert policy.check('submit', job, prof)[0] is True
    assert policy.check('submit', {**job, 'company': 'Other'}, prof)[0] is False
    called = []
    original = apply.submit_and_confirm
    apply.submit_and_confirm = lambda page, timeout: (called.append(True) or ('needs_you', 'fixture stopped before click'))
    try:
        status, note = apply.process(job, prof, bank, True, FakeContext())
    finally:
        apply.submit_and_confirm = original
    assert called and status == 'unconfirmed'


def test_http_404_closes_posting_without_form_blocker(setup):
    job, prof, bank, _ = setup
    prof['resume_variants'] = False

    class MissingPage(FakePage):
        def goto(self, url, **kwargs):
            raise RuntimeError('HTTP Error 404: Not Found')

    class MissingContext:
        def new_page(self):
            return MissingPage()

    status, note = apply.process(job, prof, bank, False, MissingContext())
    assert (status, note) == ('closed', 'posting 404')
    with sqlite3.connect(apply.DB) as conn:
        assert not state.open_blockers(conn, job['url'])
        assert not conn.execute("SELECT 1 FROM field_results WHERE field_key='__form__'").fetchone()


def test_candidates_retry_zero_blockers_and_respect_fit(setup):
    job, _, _, _ = setup
    with apply.db() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("UPDATE jobs SET fit=70, status='needs_you' WHERE url=?", (job['url'],))
        conn.execute("INSERT INTO jobs(url,company,title,fit,status) VALUES ('blocked','Other','Manager',72,'needs_you')")
        conn.execute("INSERT INTO jobs(url,company,title,fit,status) VALUES ('low','Other','Manager',50,'new')")
        conn.execute("INSERT INTO jobs(url,company,title,fit,status) VALUES ('closed','Other','Manager',75,'closed')")
        attempt = state.create_attempt(conn, 'blocked')
        conn.execute("INSERT INTO blockers(attempt_id,label,kind,status,ts) VALUES (?,?,?,'open','t')", (attempt, 'Arbitration agreement', 'checkbox'))
        conn.commit()
        assert [r['url'] for r in apply.application_candidates(conn, 66)] == [job['url']]
        conn.execute("UPDATE blockers SET status='resolved' WHERE attempt_id=?", (attempt,))
        conn.commit()
        assert {r['url'] for r in apply.application_candidates(conn, 66)} == {job['url'], 'blocked'}


def test_existing_404_form_failure_is_retired(setup):
    job, _, _, _ = setup
    with apply.db() as conn:
        conn.execute("UPDATE jobs SET status='needs_you', fit=70 WHERE url=?", (job['url'],))
        attempt = state.create_attempt(conn, job['url'])
        state.record_field(conn, attempt, '__form__', 'Application form', True, 'form', 'failed',
                           error='Automation error: HTTP Error 404: Not Found')
        assert apply.close_404_postings(conn) == 1
        assert conn.execute('SELECT status,notes FROM jobs WHERE url=?', (job['url'],)).fetchone() == ('closed', 'posting 404')
        assert apply.close_404_postings(conn) == 0

def test_migrate_backup_and_jobs_unchanged(tmp_path):
    path = tmp_path / 'test.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE jobs(url TEXT PRIMARY KEY, status TEXT)')
        conn.execute("INSERT INTO jobs VALUES ('fixture', 'new')")
        conn.commit()
        before = hashlib.sha256(repr(conn.execute('SELECT * FROM jobs').fetchall()).encode()).hexdigest()
        state.migrate(conn); state.migrate(conn)
        after = hashlib.sha256(repr(conn.execute('SELECT * FROM jobs').fetchall()).encode()).hexdigest()
        assert before == after
        assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1
    assert len(list((tmp_path / 'backups').glob('applications.*.sqlite3'))) == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in (tmp_path / 'backups').glob('*.sqlite3'))

def test_sensitive_field_hash_and_plaintext_answer_store(tmp_path):
    path = tmp_path / 'test.sqlite3'
    with sqlite3.connect(path) as conn:
        state.migrate(conn)
        a = state.create_attempt(conn, 'http://localhost/job')
        state.record_field(conn, a, 'salary', 'Salary expectation', True, 'text', 'filled', 'secret salary token')
        conn.execute('INSERT INTO answer_store(question_text,answer,sensitive,approved,ts) VALUES (?,?,?,?,?)',
                     ('Salary expectation', 'secret salary token', 1, 1, state.now()))
        conn.commit()
        assert conn.execute('SELECT answer,sensitive FROM answer_store').fetchone() == ('secret salary token', 1)
        row = conn.execute('SELECT sensitive,value_preview,value_sha256 FROM field_results').fetchone()
        assert row == (1, None, hashlib.sha256(b'secret salary token').hexdigest())
        assert 'secret salary token' not in repr(conn.execute('SELECT * FROM field_results').fetchall())
    assert path.read_bytes().count(b'secret salary token') == 1  # only the answer_store row


def test_sensitive_cli_answer_is_not_printed(setup, capsys):
    job, prof, bank, root = setup
    with apply.db() as conn:
        a = state.create_attempt(conn, job['url'])
        state.record_field(conn, a, 'salary', 'Salary expectation', True, 'text', 'unanswered')
        state.set_state(conn, a, 'blocked')
    apply.answer_cmd(job['url'], 'Salary', 'private salary answer')
    assert 'private salary answer' not in capsys.readouterr().out
    with sqlite3.connect(apply.DB) as conn:
        assert conn.execute("SELECT answer,sensitive FROM answer_store WHERE source='user_cli'").fetchone() == ('private salary answer', 1)
        assert not state.open_blockers(conn, job['url'])


def test_tab_closed_does_not_mark_passed_or_teach(setup, monkeypatch):
    job, prof, bank, root = setup
    called = []
    monkeypatch.setattr(apply, 'teach', lambda *args: called.append(args))
    with apply.db() as conn:
        a = state.create_attempt(conn, job['url'])
        state.set_state(conn, a, 'ready_for_review')
        conn.execute("UPDATE jobs SET status='staged' WHERE url=?", (job['url'],))
        conn.commit()
        apply._tab_closed(conn, job['url'])
        assert conn.execute('SELECT status FROM jobs WHERE url=?', (job['url'],)).fetchone()[0] == 'staged'
        assert conn.execute('SELECT note FROM attempts WHERE id=?', (a,)).fetchone()[0] == 'tab closed, intent unknown'
    assert not called
