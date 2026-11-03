import json
import datetime as dt
from urllib.error import HTTPError

import apply
import eightfold
import employer_link


def _conn(tmp_path, monkeypatch):
    monkeypatch.setattr(apply, 'DB', tmp_path / 'jobs.sqlite3')
    return apply.db()


def test_employer_link_match_dedupe_and_cache(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    conn.execute("INSERT INTO jobs(url,company,title,location,fit,source,status,apply_url) VALUES(?,?,?,?,?,?,?,?)",
                 ('https://www.adzuna.com/details/1', 'Acme Inc.', 'Technical Program Manager',
                  'Remote - United States', 80, 'adzuna', 'manual', 'https://www.adzuna.com/details/1'))
    calls = []
    def listings(ats, slug):
        calls.append((ats, slug))
        if ats == 'greenhouse' and slug == 'acme':
            return [dict(title='Technical Program Manager', location='Remote - United States',
                         url='https://job-boards.greenhouse.io/acme/jobs/42', token='acme', job_id='42')]
        return []
    monkeypatch.setattr(employer_link, '_listings', listings)
    monkeypatch.setattr(employer_link.time, 'sleep', lambda _: None)
    boards = {'greenhouse': {'Acme Inc.': 'acme'}}
    f = employer_link.Finder(conn, boards, {'min_fit': 66}, budget=10)
    assert f.run()['found'] == 1
    row = conn.execute('SELECT source,status,apply_url,notes,token,job_id FROM jobs').fetchone()
    assert row == ('greenhouse', 'new', 'https://job-boards.greenhouse.io/acme/jobs/42',
                   'employer link found via greenhouse', 'acme', '42')
    assert calls == [('greenhouse', 'acme')]
    assert conn.execute('SELECT count(*) FROM employer_link_cache').fetchone()[0] == 1
    assert employer_link.Finder(conn, boards, {'min_fit': 66}, dry=True).listings('greenhouse','acme')[0]['title'] == 'Technical Program Manager'
    conn.close()


def test_link_location_and_staffing(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    f = employer_link.Finder(conn, {'staffing_agencies': ['Robert Half']}, {'min_fit': 66}, dry=True)
    assert f.find(dict(company='Robert Half',title='Program Manager',location='Remote - United States')) is None
    assert not employer_link.location_compatible('Remote - United States', 'London, UK')
    assert not employer_link.location_compatible('Madison, Wisconsin', 'Chicago, Illinois')
    assert employer_link.title_similarity('Senior Project Manager', 'Project Manager, Senior') == 1.0
    conn.close()


def test_dry_link_search_does_not_change_job_or_schema(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    conn.execute("INSERT INTO jobs(url,company,title,location,fit,source,status,apply_url) VALUES(?,?,?,?,?,?,?,?)",
                 ('https://www.adzuna.com/details/7', 'Robert Half Inc', 'Program Manager',
                  'Remote - United States', 80, 'adzuna', 'manual', 'https://www.adzuna.com/details/7'))
    result = employer_link.Finder(conn, {}, {'min_fit': 66}, dry=True).run()
    assert result['missed'] == 1 or result['skipped'] == 1
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == 'manual'
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='employer_link_cache'").fetchone()
    conn.close()


def test_canonical_ats_duplicate_is_not_promoted(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    target = 'https://job-boards.greenhouse.io/acme/jobs/42'
    conn.execute("INSERT INTO jobs(url,company,title,location,fit,source,status,apply_url) VALUES(?,?,?,?,?,?,?,?)",
                 ('https://www.adzuna.com/details/8', 'Acme', 'Program Manager',
                  'Remote - United States', 80, 'adzuna', 'manual', 'https://www.adzuna.com/details/8'))
    conn.execute("INSERT INTO jobs(url,company,title,location,fit,source,status,apply_url) VALUES(?,?,?,?,?,?,?,?)",
                 (target, 'Acme', 'Program Manager', 'Remote - United States', 80,
                  'greenhouse', 'new', target))
    monkeypatch.setattr(employer_link, '_listings', lambda ats, slug:
                        [dict(title='Program Manager', location='Remote - United States', url=target)]
                        if ats == 'greenhouse' else [])
    result = employer_link.Finder(conn, {'greenhouse': {'Acme': 'acme'}}, {'min_fit': 66}).run()
    assert result['duplicate'] == 1
    assert conn.execute("SELECT status FROM jobs WHERE source='adzuna'").fetchone()[0] == 'duplicate'
    conn.close()


def test_miss_cache_expires_after_seven_days(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    finder = employer_link.Finder(conn, {}, {}, budget=10)
    monkeypatch.setattr(employer_link, '_listings', lambda ats, slug: [])
    assert finder.listings('lever', 'missing') == []
    assert conn.execute("SELECT listings FROM employer_link_cache").fetchone()[0] == '[]'
    finder2 = employer_link.Finder(conn, {}, {}, budget=10)
    monkeypatch.setattr(employer_link, '_listings', lambda ats, slug: (_ for _ in ()).throw(AssertionError('cached')))
    assert finder2.listings('lever', 'missing') == []
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)).isoformat()
    conn.execute("UPDATE employer_link_cache SET checked_at=?", (old,))
    calls = []
    monkeypatch.setattr(employer_link, '_listings', lambda ats, slug: calls.append(slug) or [])
    assert employer_link.Finder(conn, {}, {}, budget=10).listings('lever', 'missing') == []
    assert calls == ['missing']
    conn.close()


def test_transient_rate_limit_is_unavailable_not_miss(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    conn.execute("INSERT INTO jobs(url,company,title,location,fit,source,status,apply_url) VALUES(?,?,?,?,?,?,?,?)",
                 ('https://www.adzuna.com/details/9', 'Acme', 'Program Manager',
                  'Remote - United States', 80, 'adzuna', 'manual', 'https://www.adzuna.com/details/9'))
    def limited(ats, slug):
        raise HTTPError('https://example.com', 429, 'rate limited', {}, None)
    monkeypatch.setattr(employer_link, '_listings', limited)
    monkeypatch.setattr(employer_link.time, 'sleep', lambda _: None)
    result = employer_link.Finder(conn, {}, {'min_fit': 66}, budget=10).run()
    assert result['unavailable'] == 1 and result['missed'] == 0
    assert conn.execute('SELECT count(*) FROM employer_link_cache').fetchone()[0] == 0
    conn.close()


def test_eightfold_search_and_filter(monkeypatch):
    calls = []
    def get_json(url):
        calls.append(url)
        if '/search?' in url:
            return {'data': {'positions': [
                {'id': 1, 'name': 'Program Manager', 'locations': ['United States, Wisconsin, Madison'],
                 'positionUrl': '/careers/job/1', 'postedTs': 1790000000},
                {'id': 2, 'name': 'Program Manager', 'locations': ['United Kingdom, London']} ]}}
        return {'data': {'jobDescription': 'Salary $100,000 to $120,000', 'efcustomTextWorkSite': '3 days / week in-office'}}
    monkeypatch.setattr(apply, 'get_json', get_json)
    jobs = list(eightfold.fetch_eightfold('Microsoft', {'base_url': 'https://apply.careers.microsoft.com',
                                                      'domain': 'microsoft.com', 'queries': ['program manager'],
                                                      'locations': ['Madison, Wisconsin'], 'max_pages': 1}, source='microsoft'))
    assert len(jobs) == 1
    assert jobs[0]['source'] == 'microsoft'
    assert jobs[0]['apply_url'].endswith('pid=1')
    assert jobs[0]['salary']
    assert len(calls) == 2
    assert eightfold.eligible_location({'locations': ['United States, Wisconsin, Milwaukee']},
                                       {'efcustomTextWorkSite': ['0 days / week in-office – remote']})
    assert apply.company_cap_reason(apply.cap_key('Microsoft'), {'microsoft': 15}, {}, {}) == 'company cap: 15 per 30 days'


def test_microsoft_scan_needs_sign_in_and_is_not_application_candidate(tmp_path, monkeypatch):
    import smart
    import signals
    conn = _conn(tmp_path, monkeypatch)
    conn.close()
    config = tmp_path / 'profile.json'
    boards = tmp_path / 'boards.json'
    config.write_text(json.dumps({'focus_terms': ['program'], 'min_fit': 0,
                                  'remote_min_fit': 0, 'min_salary': 80000}))
    boards.write_text('{}')
    monkeypatch.setattr(apply, 'CONFIG', config)
    monkeypatch.setattr(apply, 'BOARDS', boards)
    monkeypatch.setattr(apply, 'log', lambda _: None)
    monkeypatch.setattr(signals, 'refresh', lambda *args: None)
    monkeypatch.setattr(smart, 'score_jobs', lambda conn, profile, log:
                        conn.execute('UPDATE jobs SET fit=80'))
    monkeypatch.setattr(apply, 'fetch_jobs', lambda boards: iter([dict(
        company='Microsoft', title='Program Manager', location='Remote - United States',
        description='Program delivery', url='https://apply.careers.microsoft.com/careers/job/1',
        apply_url='https://apply.careers.microsoft.com/careers/apply?pid=1',
        source='microsoft', token='microsoft.com', job_id='1', salary='$100,000-$120,000',
        posted_at='2026-11-01')]))
    apply.scan()
    with apply.db() as conn:
        assert conn.execute('SELECT source,status,notes FROM jobs').fetchone() == (
            'microsoft', 'needs_you', 'Microsoft account sign-in needed (one-time)')
        assert apply.application_candidates(conn, {'min_fit': 0}) == []


def test_rescan_keeps_promoted_employer_link(tmp_path, monkeypatch):
    import smart
    import signals
    conn = _conn(tmp_path, monkeypatch)
    target = 'https://job-boards.greenhouse.io/acme/jobs/42'
    lead = 'https://www.adzuna.com/details/42'
    conn.execute("INSERT INTO jobs(url,company,title,location,description,fit,source,status,apply_url,notes,token,job_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (lead, 'Acme', 'Program Manager', 'Remote - United States', 'Program work', 80,
                  'greenhouse', 'new', target, 'employer link found via greenhouse', 'acme', '42'))
    conn.commit(); conn.close()
    config, boards = tmp_path / 'profile.json', tmp_path / 'boards.json'
    config.write_text(json.dumps({'focus_terms': ['program'], 'min_fit': 0,
                                  'remote_min_fit': 0, 'min_salary': 0}))
    boards.write_text('{}')
    monkeypatch.setattr(apply, 'CONFIG', config)
    monkeypatch.setattr(apply, 'BOARDS', boards)
    monkeypatch.setattr(apply, 'log', lambda _: None)
    monkeypatch.setattr(apply, 'promote_adzuna', lambda job: job)
    monkeypatch.setattr(signals, 'refresh', lambda *args: None)
    monkeypatch.setattr(smart, 'score_jobs', lambda conn, profile, log:
                        conn.execute('UPDATE jobs SET fit=80'))
    monkeypatch.setattr(apply, 'fetch_jobs', lambda boards: iter([dict(
        company='Acme', title='Program Manager', location='Remote - United States',
        description='Program work', url=lead, apply_url=lead, source='adzuna',
        token='adzuna', job_id='42', salary='', posted_at='2026-11-01')]))
    apply.scan()
    with apply.db() as conn:
        assert conn.execute('SELECT source,status,apply_url,token,job_id FROM jobs').fetchone() == (
            'greenhouse', 'new', target, 'acme', '42')
