"""Workday fetcher: pre-filters title/location, builds job dicts; Delaware is in range."""
import apply


def test_fetch_workday_filters_and_builds(monkeypatch):
    posts = [
        {'title': 'Program Manager, Operations', 'externalPath': '/job/OH-Delaware/PM_R1', 'locationsText': 'OH Delaware', 'remoteType': 'Hybrid'},
        {'title': 'Senior Software Engineer', 'externalPath': '/job/OH-Delaware/SE_R2', 'locationsText': 'OH Delaware', 'remoteType': 'Hybrid'},
        {'title': 'Project Manager', 'externalPath': '/job/TX-Dallas/PM_R3', 'locationsText': 'TX Dallas', 'remoteType': 'On-site'},
    ]
    monkeypatch.setattr(apply, '_wd_post', lambda url, body: {'jobPostings': posts if body['offset'] == 0 else []})
    details = []
    def detail(url):
        details.append(url)
        return {'jobPostingInfo': {'location': 'OH Delaware', 'remoteType': 'Hybrid', 'country': {'descriptor': 'United States of America'},
                                   'jobDescription': '<p>Lead programs.</p>', 'jobReqId': 'R1', 'externalUrl': 'https://x/job/PM_R1', 'startDate': '2026-10-22'}}
    monkeypatch.setattr(apply, 'get_json_h', detail)
    jobs = list(apply.fetch_workday('Acme', {'host': 'acme.wd1', 'tenant': 'acme', 'site': 'Careers'}))
    assert [j['title'] for j in jobs] == ['Program Manager, Operations']
    assert len(details) == 1
    j = jobs[0]
    assert j['source'] == 'workday' and j['apply_url'].endswith('/apply') and j['description'] == 'Lead programs.'
    assert apply.score(j, {'focus_terms': ['program'], 'local_radius_miles': 40}, False) is not None


def test_nearby_town_in_40_not_far_city():
    assert apply.local_ok('Hybrid - OH Delaware', 40)
    assert not apply.local_ok('OH Delaware', 20)
    assert not apply.local_ok('Cleveland, OH', 40)
