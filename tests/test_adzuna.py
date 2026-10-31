"""Adzuna: local results must be inside the radius; remote queries keep only remote postings; call budget holds."""
import apply
import io
import urllib.error
import json


def test_adzuna_filters_and_budget(monkeypatch):
    apply.ADZUNA_CALLS['n'] = 0
    calls = []
    def fake(url):
        calls.append(url)
        if 'where=43215' in url:
            return {'results': [
                {'id': 1, 'title': 'Project Manager', 'description': 'Lead projects.', 'company': {'display_name': 'Near Co'},
                 'location': {'display_name': 'Dublin, Franklin County'}, 'latitude': 40.10, 'longitude': -83.11, 'redirect_url': 'https://a/1'},
                {'id': 2, 'title': 'Project Manager', 'description': 'x', 'company': {'display_name': 'Far Co'},
                 'location': {'display_name': 'Cleveland'}, 'latitude': 41.50, 'longitude': -81.69, 'redirect_url': 'https://a/2'}]}
        return {'results': [
            {'id': 3, 'title': 'Technical Program Manager (Remote)', 'description': 'remote role', 'company': {'display_name': 'R'},
             'location': {'display_name': 'US'}, 'redirect_url': 'https://a/3', 'salary_min': 100000, 'salary_max': 100000},
            {'id': 4, 'title': 'Technical Program Manager', 'description': 'onsite in Austin', 'company': {'display_name': 'S'},
             'location': {'display_name': 'Austin'}, 'redirect_url': 'https://a/4'}]}
    monkeypatch.setattr(apply, 'get_json', fake)
    cfg = {'local': ['project manager'], 'remote': ['technical program manager'], 'pages': 3}
    jobs = list(apply.fetch_adzuna(cfg, {'ADZUNA_APP_ID': 'i', 'ADZUNA_APP_KEY': 'k'}, 40, max_calls=5))
    assert [j['company'] for j in jobs] == ['Near Co', 'R']
    assert jobs[0]['local'] and jobs[0]['source'] == 'adzuna'
    assert jobs[1]['salary'].startswith('est.')
    assert len(calls) == 2  # short pages stop paging
    prof = {'focus_terms': ['program'], 'local_radius_miles': 40}
    assert apply.score(jobs[0], prof, False) is not None


def test_lead_sources_never_automated(monkeypatch):
    monkeypatch.setattr(apply, 'prepare_lead', lambda job, prof: 'LEAD')
    class Boom:
        def new_page(self):
            raise AssertionError('browser must not open for leads')
    for src in apply.LEAD_SOURCES:
        expected = 'manual' if src in ('adzuna', 'remotive', 'himalayas') else 'needs_you'
        assert apply.process({'source': src, 'apply_url': 'u'}, {}, [], True, Boom()) == (expected, 'LEAD')


def test_adzuna_redirect_to_ats_and_details_link(monkeypatch):
    class Response:
        def __init__(self, url, body):
            self.url, self.body = url, body
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def geturl(self):
            return self.url
        def read(self, limit):
            return self.body.encode()[:limit]
    def fetch(req, timeout):
        url = req.full_url
        if url.endswith('/redirect'):
            return Response('https://jobs.lever.co/acme/123?utm_source=adzuna', '')
        return Response(url, '<a href="https://jobs.ashbyhq.com/acme/abc?utm_medium=feed">Apply now</a>')
    monkeypatch.setattr(apply.urllib.request, 'urlopen', fetch)
    assert apply.resolve_adzuna_url('https://www.adzuna.com/redirect', '') == ('https://jobs.lever.co/acme/123', 'lever')
    assert apply.resolve_adzuna_url('', 'https://www.adzuna.com/details/1') == ('https://jobs.ashbyhq.com/acme/abc', 'ashby')


def test_refresh_adzuna_reclassifies_and_deduplicates(monkeypatch, tmp_path):
    monkeypatch.setattr(apply, 'DB', tmp_path / 'jobs.sqlite3')
    with apply.db() as conn:
        conn.execute("INSERT INTO jobs(url,company,title,source,apply_url,status,fit) VALUES ('https://www.adzuna.com/details/1','A','PM','adzuna','https://adzuna/1','needs_you',90)")
        conn.execute("INSERT INTO jobs(url,company,title,source,apply_url,status,fit) VALUES ('https://www.adzuna.com/details/2','B','PM','adzuna','https://adzuna/2','needs_you',90)")
        conn.execute("INSERT INTO jobs(url,company,title,source,apply_url,status,fit) VALUES ('https://www.adzuna.com/details/3','C','PM','adzuna','https://adzuna/3','needs_you',90)")
        conn.execute("INSERT INTO jobs(url,company,title,source,apply_url,status,fit) VALUES ('https://jobs.lever.co/a/1','A','PM','lever','https://jobs.lever.co/a/1','new',75)")
        monkeypatch.setattr(apply, 'resolve_adzuna_url', lambda url, details: {
            'https://adzuna/1': ('https://jobs.lever.co/a/1?utm_source=adzuna', 'lever'),
            'https://adzuna/2': ('https://jobs.ashbyhq.com/b/2', 'ashby'),
            'https://adzuna/3': (None, None),
        }[url])
        apply.refresh_adzuna_jobs(conn)
        rows = conn.execute("SELECT status,source,apply_url FROM jobs WHERE url LIKE '%adzuna.com/details/%' ORDER BY url").fetchall()
        assert rows == [('duplicate', 'adzuna', 'https://adzuna/1'),
                        ('new', 'ashby', 'https://jobs.ashbyhq.com/b/2'),
                        ('manual', 'adzuna', 'https://adzuna/3')]
        assert [r[0] for r in apply.application_candidates(conn, 60)] == ['https://www.adzuna.com/details/2', 'https://jobs.lever.co/a/1']


def test_application_candidates_prioritize_ats(tmp_path, monkeypatch):
    monkeypatch.setattr(apply, 'DB', tmp_path / 'jobs.sqlite3')
    with apply.db() as conn:
        conn.execute("INSERT INTO jobs(url,company,title,source,status,fit) VALUES ('lead','A','PM','usajobs','new',99)")
        conn.execute("INSERT INTO jobs(url,company,title,source,status,fit) VALUES ('ats','B','PM','greenhouse','new',70)")
        assert [r[0] for r in apply.application_candidates(conn, 60)] == ['ats', 'lead']


def test_adzuna_503_retries_then_continues(monkeypatch):
    apply.ADZUNA_CALLS['n'] = 0
    calls, sleeps = [], []
    def fake(url):
        calls.append(url)
        if 'where=43215' in url:
            raise urllib.error.HTTPError(url, 503, 'Unavailable', {}, io.BytesIO())
        return {'results': [{'id': 9, 'title': 'Program Manager Remote', 'description': 'Remote in US',
                             'company': {'display_name': 'B'}, 'location': {'display_name': 'US'}}]}
    monkeypatch.setattr(apply, 'get_json', fake)
    monkeypatch.setattr(apply.time, 'sleep', sleeps.append)
    monkeypatch.setattr(apply, 'log', lambda message: None)
    jobs = list(apply.fetch_adzuna({'local': ['pm'], 'remote': ['pm'], 'pages': 1},
                                   {'ADZUNA_APP_ID': 'i', 'ADZUNA_APP_KEY': 'k'}, max_calls=5))
    assert len(jobs) == 1 and jobs[0]['company'] == 'B'
    assert len(calls) == 4 and sleeps == [1, 2]


def test_scan_promotes_adzuna_and_avoids_existing_ats(monkeypatch, tmp_path):
    monkeypatch.setattr(apply, 'DB', tmp_path / 'jobs.sqlite3')
    config = tmp_path / 'profile.json'
    boards = tmp_path / 'boards.json'
    config.write_text(json.dumps({'min_fit': 60, 'focus_terms': []}))
    boards.write_text('{}')
    monkeypatch.setattr(apply, 'CONFIG', config)
    monkeypatch.setattr(apply, 'BOARDS', boards)
    monkeypatch.setattr(apply, 'LOG', tmp_path / 'run.log')
    monkeypatch.setattr(apply, 'score', lambda job, profile, eu: 80)
    monkeypatch.setattr(apply, 'resolve_adzuna_url', lambda redirect, details: (
        ('https://jobs.lever.co/acme/123?utm_source=adzuna', 'lever') if redirect.endswith('/1')
        else (None, None)))
    def listing(number):
        return dict(company='Acme', title='Program Manager', location='Remote - US', description='Remote',
                    url=f'https://www.adzuna.com/details/{number}', apply_url=f'https://adzuna/{number}',
                    source='adzuna', token='adzuna', job_id=str(number), salary='', posted_at='')
    monkeypatch.setattr(apply, 'fetch_jobs', lambda _: iter((listing(1), listing(2))))
    import smart, signals
    monkeypatch.setattr(smart, 'score_jobs', lambda conn, profile, logger: conn.execute('UPDATE jobs SET fit=score'))
    monkeypatch.setattr(signals, 'refresh', lambda conn, logger: None)
    with apply.db() as conn:
        conn.execute("INSERT INTO jobs(url,company,title,source,apply_url,status,fit) VALUES ('https://jobs.lever.co/acme/123','Acme','PM','lever','https://jobs.lever.co/acme/123','new',80)")
    apply.scan()
    with apply.db() as conn:
        assert conn.execute("SELECT count(*) FROM jobs WHERE source='lever'").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM jobs WHERE url='https://www.adzuna.com/details/2'").fetchone()[0] == 'manual'
        assert conn.execute("SELECT count(*) FROM jobs WHERE status='needs_you'").fetchone()[0] == 0
