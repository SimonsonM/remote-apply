"""Doctor output and network safety using fixture data only."""
import json
import sqlite3

import pytest

import apply
import doctor
from test_browser_fixture import browser, case, fixture_site


def test_outcome_mapping():
    assert doctor.outcome('name', '', '', '', None) == 'profile'
    assert doctor.outcome(None, 'yes', 'answer_store:7', '', 7) == 'answer_store:7'
    assert doctor.outcome(None, '', '', 'unapproved_answer', 8) == 'pending_answer:8'
    assert doctor.outcome(None, '', '', 'stale_answer', 9) == 'stale_answer:9'
    assert doctor.outcome(None, '', '', '', None, True) == 'draftable'
    assert doctor.outcome(None, '', '', '', None) == 'none'


def test_candidates_varied_and_deterministic(tmp_path, monkeypatch):
    monkeypatch.setattr(apply, 'DB', tmp_path / 'jobs.sqlite3')
    with apply.db() as conn:
        for source in ('lever', 'ashby', 'greenhouse'):
            for status in ('needs_you', 'new'):
                conn.execute('INSERT INTO jobs(url,source,status) VALUES (?,?,?)', (f'https://{source}.example/{status}', source, status))
        conn.commit()
    with doctor.read_conn() as conn:
        chosen = doctor.candidates(conn, 6)
    assert [r['source'] for r in chosen] == ['greenhouse', 'greenhouse', 'lever', 'lever', 'ashby', 'ashby']
    assert {r['status'] for r in chosen} == {'new', 'needs_you'}


@pytest.mark.browser
def test_doctor_inspect_is_read_only_and_safe(case, browser, tmp_path):
    job, prof, bank, handler = case
    values = ['Secret Fixture Name', 'fixture.private@example.test', '555-867-5309']
    profile = json.loads(apply.CONFIG.read_text())
    profile.update(name=values[0], email=values[1], phone=values[2])
    apply.CONFIG.write_text(json.dumps(profile))
    with sqlite3.connect(apply.DB) as conn:
        before = conn.execute('SELECT count(*) FROM attempts').fetchone()[0]
    path, reports = doctor.run([job['url']], browser=browser, output_dir=tmp_path)
    report = reports[0]
    assert report['fields']
    assert report['aborted_requests'] == 0
    assert handler.posts == 0
    assert path.stat().st_mode & 0o777 == 0o600
    data = path.read_text()
    assert all(value not in data for value in values)
    with sqlite3.connect(apply.DB) as conn:
        assert conn.execute('SELECT count(*) FROM attempts').fetchone()[0] == before


@pytest.mark.browser
def test_doctor_fill_blocks_injected_submit(case, browser, tmp_path, monkeypatch):
    job, prof, bank, handler = case
    original = apply.generic_fill
    def injected_submit(page, actions, tolerant=False):
        results = original(page, actions, tolerant)
        page.evaluate("document.querySelector('form').noValidate = true; document.querySelector('button[type=submit]').click()")
        return results
    monkeypatch.setattr(apply, 'generic_fill', injected_submit)
    _, reports = doctor.run([job['url']], fill=True, browser=browser, output_dir=tmp_path)
    assert handler.posts == 0
    assert reports[0]['aborted_requests'] == 1
    assert any('fill_status' in field for field in reports[0]['fields'])


@pytest.mark.browser
def test_doctor_captcha_note(case, browser, tmp_path):
    job, _, _, handler = case
    url = job['url'].replace('/apply', '/doctor-captcha')
    _, reports = doctor.run([url], browser=browser, output_dir=tmp_path)
    assert 'captcha_iframe' in reports[0]['platform_notes']
    assert handler.posts == 0
