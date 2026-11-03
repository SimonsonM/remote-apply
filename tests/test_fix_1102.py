"""Regression checks for the November 2 supply and form blockers."""
import sqlite3
from types import SimpleNamespace

import apply
import ats_form
import smart
import state
import workday


def test_salary_max_and_candidate_floors(tmp_path, monkeypatch):
    for text, expected in [('$ 65,000 - $120,000', 120000), ('$110K - $150K', 150000),
                           ('USD 105,000-150,000 per-year-salary', 150000),
                           ('$87,700.00 - $157,800.00', 157800), ('$40/hr', 83200),
                           ('', None)]:
        assert apply.posted_salary_max(text) == expected
    monkeypatch.setattr(apply, 'DB', tmp_path / 'jobs.sqlite3')
    prof = {'min_fit': 66, 'remote_min_fit': 58, 'min_salary': 80000}
    with apply.db() as conn:
        conn.row_factory = sqlite3.Row
        rows = [('remote', 'Remote - United States', 58, '$40/hr', 'Program Manager'),
                ('local', 'Springfield, IL', 58, '', 'Program Manager'),
                ('cheap', 'Remote - United States', 70, '$65K - $75K', 'Program Manager'),
                ('unknown', 'Remote - United States', 59, '', 'Program Manager'),
                ('german', 'Remote - United States', 90, '', 'German Program Manager')]
        for url, loc, fit, salary, title in rows:
            conn.execute('INSERT INTO jobs(url,company,title,location,fit,salary,status) VALUES (?,\'Example\',?,?,?, ?,\'new\')',
                         (url, title, loc, fit, salary))
        assert {r['url'] for r in apply.application_candidates(conn, prof)} == {'remote', 'unknown'}


def test_sponsorship_and_employer_history(tmp_path, monkeypatch):
    monkeypatch.setattr(apply, 'DB', tmp_path / 'answers.sqlite3')
    with apply.db() as conn:
        conn.execute("INSERT INTO answer_store(match_regex,answer,approved,status,scope,ts,category) VALUES ('sponsor','No',1,'approved','*','t','attestation')")
        import answer_store
        answer_store.grant_standing(conn, 'attestation')
    prof = {'us_citizen': True}
    for q in ('Do you require sponsorship, now or in the future, for permanent employment?',
              'If you are a foreign national, will you now or in the future require sponsorship?',
              'Do you, now or in the future, require sponorship?'):
        assert apply.resolve(q, prof, [], 'Backblaze') == 'No'
    assert apply.resolve('Work authorization', prof, [], 'Backblaze') == 'Yes'
    for q in ('Have you previously worked at Backblaze?', 'Have you been employed at Backblaze?',
              'Have you consulted for Backblaze previously?', 'Are you a former Backblaze employee?',
              'Are you an employee of Backblaze or an affiliated company (currently or previously)?'):
        assert apply.resolve(q, prof, [], 'Backblaze') == 'No'
    prof['past_employers'] = ['Acme Logistics', 'Globex']
    for company in prof['past_employers']:
        assert apply.resolve(f'Are you a former {company} employee?', prof, [], company) == ''


def test_citizen_options_and_education(monkeypatch):
    monkeypatch.setattr(apply, 'US_CITIZEN', True)
    options = ['Select One', 'Yes-I hold a temporary visa status', 'No-I hold a visa status',
               'N/A-I am a US Citizen or US Resident (Green Card Holder)']
    assert apply.pick_option('No', options) == options[-1]
    assert apply.pick_option('Master', ['Bachelor degree', "Master's degree", 'Doctorate']) == "Master's degree"
    assert apply.pick_option('2 years', ['0-3 years', '3-4 years', '4-7 years', '7+ years']) == '0-3 years'


def test_workday_skills_dates_and_upload_log(tmp_path):
    for label in ('Excel intermediate-to-advance functions', 'MS Project', 'PowerPoint', 'Leading projects'):
        assert workday.checklist_choice('Select skills: Excel / MS Project / Smart Sheets / PowerPoint / Leading projects / None of the above', label)
    assert not workday.checklist_choice('Select skills: Excel / Smart Sheets / None of the above', 'Smart Sheets')
    assert not workday.checklist_choice('Select skills: Excel / None of the above', 'None of the above')
    profile = {'employment': {'start_month': 'May', 'start_year': 2026}}
    assert workday.date_part_answer('Start date month*', 'My Experience', profile) == 'May'
    assert workday.date_part_answer('Start date year*', 'My Experience', profile) == '2026'
    assert workday.date_part_answer('Start date month*', 'When can you start?', profile)
    file = tmp_path / 'resume.docx'; file.write_bytes(b'resume')
    assert f'{file} (6 bytes, file=True)' == apply.attachment_details(file)


def test_credentials_and_timeout_migration(tmp_path, monkeypatch):
    resume = 'Scrum Fundamentals Certificate (SFC) from SCRUMstudy; Microsoft Learn training.'
    assert not smart.grounding_problems('I hold the Scrum Fundamentals Certificate (SFC).', resume)
    assert ('credential', 'Scrum Fundamentals Certification') in smart.grounding_problems('I hold the Scrum Fundamentals Certification.', resume)
    assert ('credential', 'Microsoft Learn certification') in smart.grounding_problems('I hold a Microsoft Learn certification.', resume)
    monkeypatch.setattr(apply, 'DB', tmp_path / 'retry.sqlite3')
    with apply.db() as conn:
        for url, source, note in [('sr', 'smartrecruiters', 'Automation error: Page.wait_for_selector: Timeout 25000ms exceeded.'),
                                  ('ashby', 'ashby', 'Automation error: Page.wait_for_selector: Timeout 25000ms exceeded.'),
                                  ('other', 'lever', 'Automation error: Page.wait_for_selector: Timeout 25000ms exceeded.')]:
            conn.execute("INSERT INTO jobs(url,company,title,source,status,notes) VALUES (?,'Example','Manager',?,'needs_you',?)", (url, source, note))
        assert apply.retry_form_timeouts(conn) == 2
        assert {r[0] for r in conn.execute("SELECT url FROM jobs WHERE status='new'")} == {'sr', 'ashby'}


def test_smartrecruiters_challenge_detection():
    page = SimpleNamespace(frames=[SimpleNamespace(url='https://geo.captcha-delivery.com/captcha/')], content=lambda: '')
    assert ats_form.challenge_present(page)
    page = SimpleNamespace(frames=[], content=lambda: '<form><input></form>')
    assert not ats_form.challenge_present(page)


def test_reconcile_reopens_newly_answered_history(tmp_path, monkeypatch):
    monkeypatch.setattr(apply, 'DB', tmp_path / 'reconcile.sqlite3')
    with apply.db() as conn:
        conn.execute("INSERT INTO jobs(url,company,title,location,fit,status) VALUES ('u','Backblaze','Program Manager','Remote - United States',60,'needs_you')")
        attempt = state.create_attempt(conn, 'u')
        conn.execute("INSERT INTO blockers(attempt_id,label,kind,status,ts) VALUES (?,?,?,'open','t')",
                     (attempt, 'Have you previously worked at Backblaze?', 'text'))
        conn.commit()
        assert apply.reconcile_blockers(conn, {'us_citizen': True}) == 1
        assert [r[0] for r in apply.application_candidates(conn, {'min_fit': 66, 'remote_min_fit': 58})] == ['u']
