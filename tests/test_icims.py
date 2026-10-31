"""Offline iCIMS discovery, routing, and application safety checks."""
from pathlib import Path

import apply
import icims

FIXTURES = Path(__file__).parent / 'fixtures'


def test_parser_and_stable_job_shape(monkeypatch):
    search = (FIXTURES / 'icims_search.html').read_text()
    detail = (FIXTURES / 'icims_job.html').read_text()
    calls = []
    def get(host, path):
        calls.append(path)
        return search if '/search?' in path else detail
    monkeypatch.setattr(icims, '_get', get)
    jobs = list(icims.fetch_icims('Snap-on', {'host': 'careers-snapon.icims.com'}, max_pages=1))
    assert len(jobs) == 2
    job = jobs[0]
    assert job['source'] == 'icims' and job['job_id'] == '12345'
    assert job['url'] == 'https://careers-snapon.icims.com/jobs/12345/job'
    assert job['apply_url'] == 'https://careers-snapon.icims.com/jobs/12345/login'
    assert job['title'] == 'Remote Program Manager'
    assert job['location'] == 'Remote - United States'
    assert 'Lead programs across teams.' in job['description']
    assert len(calls) == 3


def test_process_routes_icims_when_enabled(monkeypatch):
    calls = []
    monkeypatch.setattr(icims, 'apply_job', lambda page, job, prof, bank, dry: (calls.append((page, dry)) or ('ready', 'filled')))
    class Page:
        def goto(self): pass
    class Context:
        def new_page(self): return Page()
    result = apply.process({'source': 'icims'}, {'icims_auto': True}, [], False, Context())
    assert result == ('ready', 'filled')
    assert len(calls) == 1 and isinstance(calls[0][0], Page) and calls[0][1] is True


def test_arbitration_checkbox_page_returns_needs_you(monkeypatch):
    from contextlib import nullcontext
    label = 'I agree to binding arbitration and waive class action rights'
    class Scope:
        def evaluate(self, script):
            return [dict(i=0, type='checkbox', tag='input', name='arbitration', label=label,
                         question=label, heading='', required=True, options=[label])]
        def locator(self, selector):
            return type('Loc', (), {'count': lambda self: 0})()
    class Page:
        def goto(self, *args, **kwargs): pass
    scope = Scope()
    monkeypatch.setattr(apply, 'resolve', lambda *args: '__agree__')
    results, fields, missing = icims._fill_page(scope, {'company': 'Acme'}, {'resume': '/tmp/resume.pdf'}, [])
    assert results == []
    assert missing == [label]
    monkeypatch.setattr(icims, '_frame', lambda page: scope)
    monkeypatch.setattr(icims, '_settle', lambda page: None)
    monkeypatch.setattr(icims, '_auth', lambda *args: [])
    monkeypatch.setattr(icims, '_captcha', lambda scope: False)
    monkeypatch.setattr(icims, '_body', lambda scope: 'Application questions')
    monkeypatch.setattr(icims, '_proof', lambda *args: 'proof.png')
    monkeypatch.setattr(apply, 'db', lambda: nullcontext(object()))
    monkeypatch.setattr(apply, '_new_attempt', lambda *args: 1)
    monkeypatch.setattr(apply, '_record_attempt', lambda conn, attempt, job, results, missing, fields, **kwargs:
                        ('blocked', f'{missing[0]}: no approved answer'))
    status, note = icims.apply_job(Page(), {'url': 'https://acme.icims.com/jobs/1/job',
                                           'apply_url': 'https://acme.icims.com/jobs/1/login',
                                           'company': 'Acme'}, {'resume': '/tmp/resume.pdf'}, [], dry=True)
    assert status == 'needs_you' and note.startswith(label + ': no approved answer')


def test_rejects_unrecognized_host():
    import pytest
    with pytest.raises(ValueError):
        list(icims.fetch_icims('Acme', {'host': 'icims.com.evil.test'}))


def test_passive_hcaptcha_badge_is_not_a_challenge(monkeypatch):
    class Locator:
        def __init__(self, visible): self.visible = visible
        def count(self): return int(self.visible)
        @property
        def first(self): return self
        def is_visible(self): return self.visible
    class Scope:
        def __init__(self, visible): self.visible = visible
        def locator(self, selector): return Locator(self.visible)
    monkeypatch.setattr(icims, '_body', lambda scope: 'Protected by hCaptcha Privacy Terms')
    assert not icims._captcha(Scope(False))
    assert icims._captcha(Scope(True))


def test_dashboard_confirmation_names_job(monkeypatch):
    monkeypatch.setattr(icims, '_body', lambda scope: scope)
    job = {'title': 'Remote Program Manager'}
    assert icims._confirmed('My Applications: Remote Program Manager — Submitted', job)
    assert not icims._confirmed('My Applications: Other Job — Submitted', job)
    assert not icims._confirmed('Your application was not submitted', job)
