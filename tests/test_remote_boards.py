"""Remote board API parsing and the draft-only recruiter email command."""
import json

import apply
import smart
import pytest


def test_remotive_parsing_and_us_eligibility(monkeypatch):
    calls = []
    def fake(url):
        calls.append(url)
        return {'jobs': [
            {'id': 1, 'url': 'https://remotive.com/one', 'title': 'Program Manager',
             'company_name': 'Acme', 'candidate_required_location': 'Northern America, LATAM',
             'description': '<p>Lead &amp; deliver</p>', 'salary': '$90k - $105k',
             'publication_date': '2026-10-01T12:00:00'},
            {'id': 2, 'url': 'https://remotive.com/two', 'title': 'Project Manager',
             'candidate_required_location': 'Europe'},
        ]}
    monkeypatch.setattr(apply, 'get_json', fake)
    jobs = list(apply.fetch_remotive({}))
    assert calls == ['https://remotive.com/api/remote-jobs']
    assert len(jobs) == 1
    assert jobs[0]['source'] == 'remotive' and jobs[0]['company'] == 'Acme'
    assert jobs[0]['description'] == 'Lead & deliver'
    assert jobs[0]['salary'] == '90,000-105,000 yearly'
    assert jobs[0]['posted_at'] == '2026-10-01'
    assert apply.score(jobs[0], {'focus_terms': []}, False) is not None


def test_himalayas_parsing_paging_and_timezone(monkeypatch):
    calls, sleeps = [], []
    def fake(url):
        calls.append(url)
        if 'offset=0' in url:
            return {'jobs': [
                {'id': 'a', 'applicationLink': 'https://himalayas.app/a', 'title': 'Operations Manager',
                 'companyName': 'Acme', 'description': '<b>Operations</b>',
                 'locationRestrictions': ['United States'], 'timezoneRestrictions': [-5],
                 'minSalary': 50, 'maxSalary': 80, 'salaryPeriod': 'hourly', 'currency': 'USD',
                 'pubDate': 1790812800},
                {'id': 'b', 'applicationLink': 'https://himalayas.app/b', 'title': 'Project Manager',
                 'locationRestrictions': ['India']},
            ]}
        return {'jobs': []}
    monkeypatch.setattr(apply, 'get_json', fake)
    monkeypatch.setattr(apply.time, 'sleep', sleeps.append)
    cfg = {'page_size': 2, 'max_pages': 2, 'home_utc_offsets': [-5, -4]}
    jobs = list(apply.fetch_himalayas(cfg))
    assert len(calls) == 2 and 'limit=2&offset=2' in calls[1]
    assert len(sleeps) == 1 and 1 <= sleeps[0] <= 2.5
    assert len(jobs) == 1
    assert jobs[0]['source'] == 'himalayas' and jobs[0]['salary'] == '50-80 hourly'
    assert jobs[0]['posted_at'] == '2026-10-01'
    assert apply.score(jobs[0], {'focus_terms': []}, False) is not None
    assert not apply.himalayas_allows_us({'timezoneRestrictions': [-8]}, [-5, -4])
    assert apply.himalayas_allows_us({'timezoneRestrictions': [-8]}, [])
    assert list(apply.fetch_himalayas({**cfg, 'home_utc_offsets': [-8]})) == []


def test_disqualifier_penalty_is_optional_and_word_bounded():
    job = dict(company='Acme', title='Program Manager', location='Remote - United States',
               description='Requires active security clearance.')
    base = apply.score(job, {'focus_terms': []}, False)
    assert apply.score(job, {'focus_terms': [], 'disqualifiers': {}}, False) == base
    assert apply.score(job, {'focus_terms': [], 'disqualifiers': {'ACTIVE SECURITY CLEARANCE': 20}}, False) == base - 20
    job['description'] = 'Requires inactive security clearances.'
    assert apply.score(job, {'focus_terms': [], 'disqualifiers': {'active security clearance': 20}}, False) == base


def test_remote_board_ats_promotion_uses_existing_dedupe(tmp_path, monkeypatch):
    monkeypatch.setattr(apply, 'DB', tmp_path / 'jobs.sqlite3')
    job = dict(url='https://remotive.com/one', apply_url='https://jobs.lever.co/acme/123?utm_source=remotive',
               source='remotive', token='remotive', job_id='1')
    apply.promote_remote_board(job)
    assert job['source'] == 'lever'
    with apply.db() as conn:
        conn.execute("INSERT INTO jobs(url,apply_url,source) VALUES (?,?,?)",
                     ('https://jobs.lever.co/acme/123', 'https://jobs.lever.co/acme/123', 'lever'))
        assert apply.existing_ats_url(conn, job['apply_url'])


def test_recruiter_email_draft_and_grounding_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(apply, 'DB', tmp_path / 'jobs.sqlite3')
    monkeypatch.setattr(apply, 'ROOT', tmp_path)
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'name': 'Jane Doe', 'resume': 'resume.docx'}))
    monkeypatch.setattr(apply, 'CONFIG', profile)
    url = 'https://example.test/job'
    with apply.db() as conn:
        conn.execute('INSERT INTO jobs(url,company,title,description,job_id) VALUES (?,?,?,?,?)',
                     (url, 'Acme Co', 'Program Manager', 'Lead projects', '42'))
    monkeypatch.setattr(smart, 'grounded_resume_text', lambda profile: 'Jane led projects at Acme Co.')
    monkeypatch.setattr(smart, 'recruiter_email', lambda profile, job: 'Subject: Program Manager\n\nI led projects at Acme Co.\n\nJane Doe')
    monkeypatch.setattr(smart, 'check_grounding', lambda *args, **kwargs: True)
    apply.recruiter_email_cmd(url)
    path = tmp_path / 'followups' / 'recruiter_Acme_Co_42.md'
    assert path.read_text().startswith('DRAFT - not sent\n\nSubject:')
    path.unlink()
    monkeypatch.setattr(smart, 'check_grounding', lambda *args, **kwargs: False)
    with pytest.raises(SystemExit, match='failed grounding'):
        apply.recruiter_email_cmd(url)
    assert not path.exists()


def test_timezone_setting_names():
    assert apply.tz_offsets({}) == ()
    assert apply.tz_offsets({'timezone': 'any'}) == ()
    assert apply.tz_offsets({'timezone': 'CST'}) == (-6, -5)
    assert apply.tz_offsets({'timezone': 'Pacific'}) == (-8, -7)
    assert apply.tz_offsets({'timezone': 'any', 'home_utc_offsets': [-5]}) == (-5,)
    job = {'timezoneRestrictions': [-8, -7]}
    assert apply.himalayas_allows_us(job, apply.tz_offsets({'timezone': 'any'}))
    assert not apply.himalayas_allows_us(job, apply.tz_offsets({'timezone': 'eastern'}))
