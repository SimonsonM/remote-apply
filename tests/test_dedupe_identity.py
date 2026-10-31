"""Duplicate-application guard: same role under a different URL, ATS id, or company+title."""
import datetime as dt
import sqlite3

import pytest

import state

JOBS = ("CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT, location TEXT, status TEXT DEFAULT 'new', "
        "source TEXT DEFAULT '', job_id TEXT DEFAULT '', applied_at TEXT DEFAULT '')")


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / 'dedupe.sqlite3')
    state.migrate(c)
    c.execute(JOBS)
    yield c
    c.close()


def add_job(conn, url, company='Acme Inc.', title='Program Manager', source='greenhouse', job_id='', status='new', applied_at=''):
    conn.execute('INSERT INTO jobs(url,company,title,source,job_id,status,applied_at) VALUES (?,?,?,?,?,?,?)',
                 (url, company, title, source, job_id, status, applied_at))
    conn.commit()


def submit(conn, url, final='confirmed'):
    a = state.create_attempt(conn, url)
    state.set_state(conn, a, 'ready_for_review')
    state.begin_submit(conn, url)
    if final:
        state.set_state(conn, a, final)
    return a


def ready(conn, url):
    a = state.create_attempt(conn, url)
    state.set_state(conn, a, 'ready_for_review')
    return a


def test_normalize_url_strips_tracking_but_keeps_job_ids():
    a = state.normalize_url('HTTPS://Boards.Greenhouse.io/acme/jobs/123/?utm_source=x&gh_src=abc#apply')
    b = state.normalize_url('https://boards.greenhouse.io/acme/jobs/123')
    assert a == b
    assert 'gh_jid=9' in state.normalize_url('https://careers.acme.com/jobs?gh_jid=9&utm_medium=y')


def test_tracking_param_variant_is_refused(conn):
    add_job(conn, 'https://boards.greenhouse.io/acme/jobs/1', job_id='1')
    submit(conn, 'https://boards.greenhouse.io/acme/jobs/1')
    add_job(conn, 'https://boards.greenhouse.io/acme/jobs/1?utm_source=linkedin', title='Different Title')
    ready(conn, 'https://boards.greenhouse.io/acme/jobs/1?utm_source=linkedin')
    with pytest.raises(state.DuplicateSubmission, match='different URL'):
        state.begin_submit(conn, 'https://boards.greenhouse.io/acme/jobs/1?utm_source=linkedin')


def test_same_ats_job_id_on_careers_page_is_refused(conn):
    add_job(conn, 'https://job-boards.greenhouse.io/toast/jobs/8245664', company='Toast', title='Senior PM, Lending', job_id='8245664')
    submit(conn, 'https://job-boards.greenhouse.io/toast/jobs/8245664')
    add_job(conn, 'https://careers.toasttab.com/jobs?gh_jid=8245664', company='Toast, Inc.', title='Sr. Product Manager - Lending', job_id='8245664')
    ready(conn, 'https://careers.toasttab.com/jobs?gh_jid=8245664')
    with pytest.raises(state.DuplicateSubmission, match='job id 8245664'):
        state.begin_submit(conn, 'https://careers.toasttab.com/jobs?gh_jid=8245664')


def test_same_company_and_title_repost_is_refused(conn):
    add_job(conn, 'https://jobs.lever.co/acme/aaa', company='Acme Inc.', title='Program Manager (Remote)', source='lever', job_id='aaa')
    submit(conn, 'https://jobs.lever.co/acme/aaa', final='uncertain')
    add_job(conn, 'https://jobs.ashbyhq.com/acme/bbb', company='ACME', title='Program Manager', source='ashby', job_id='bbb')
    ready(conn, 'https://jobs.ashbyhq.com/acme/bbb')
    with pytest.raises(state.DuplicateSubmission, match='company and title'):
        state.begin_submit(conn, 'https://jobs.ashbyhq.com/acme/bbb')


def test_different_roles_at_same_company_are_allowed(conn):
    add_job(conn, 'https://jobs.ashbyhq.com/1password/a', company='1Password', title='Staff Program Manager, CX Operations', source='ashby', job_id='a')
    submit(conn, 'https://jobs.ashbyhq.com/1password/a')
    add_job(conn, 'https://jobs.ashbyhq.com/1password/b', company='1Password', title='Senior Manager, Strategic Operations', source='ashby', job_id='b')
    ready(conn, 'https://jobs.ashbyhq.com/1password/b')
    state.begin_submit(conn, 'https://jobs.ashbyhq.com/1password/b')


def test_abandoned_attempt_does_not_block_retry_elsewhere(conn):
    add_job(conn, 'https://x.example/jobs/1', job_id='1')
    a = ready(conn, 'https://x.example/jobs/1')
    state.begin_submit(conn, 'https://x.example/jobs/1')
    state.set_state(conn, a, 'abandoned')  # CAPTCHA wall: definitively not submitted
    add_job(conn, 'https://x.example/jobs/1?utm_source=z', job_id='1')
    ready(conn, 'https://x.example/jobs/1?utm_source=z')
    state.begin_submit(conn, 'https://x.example/jobs/1?utm_source=z')


def test_manual_applied_status_counts(conn):
    add_job(conn, 'https://a.example/1', company='Mercury', title='BCM Lead', status='applied', applied_at=dt.date.today().isoformat())
    add_job(conn, 'https://b.example/2', company='Mercury', title='BCM Lead')
    ready(conn, 'https://b.example/2')
    with pytest.raises(state.DuplicateSubmission):
        state.begin_submit(conn, 'https://b.example/2')


def test_company_title_match_expires_after_window(conn):
    old = (dt.date.today() - dt.timedelta(days=state.DUPLICATE_WINDOW_DAYS + 5)).isoformat()
    add_job(conn, 'https://a.example/1', company='Mercury', title='BCM Lead', status='applied', applied_at=old)
    add_job(conn, 'https://b.example/2', company='Mercury', title='BCM Lead')
    ready(conn, 'https://b.example/2')
    state.begin_submit(conn, 'https://b.example/2')


def test_refusal_rolls_back_and_leaves_attempt_ready(conn):
    add_job(conn, 'https://a.example/1', job_id='9', status='applied')
    add_job(conn, 'https://b.example/2', job_id='9', title='Other')
    a = ready(conn, 'https://b.example/2')
    with pytest.raises(state.DuplicateSubmission):
        state.begin_submit(conn, 'https://b.example/2')
    assert conn.execute('SELECT state FROM attempts WHERE id=?', (a,)).fetchone()[0] == 'ready_for_review'
    assert not conn.in_transaction


def test_minimal_jobs_table_still_works(tmp_path):
    with sqlite3.connect(tmp_path / 'min.sqlite3') as c:
        state.migrate(c)
        c.execute('CREATE TABLE jobs (url TEXT PRIMARY KEY, status TEXT)')
        c.execute("INSERT INTO jobs VALUES ('fixture','new')")
        ready(c, 'fixture')
        state.begin_submit(c, 'fixture')
