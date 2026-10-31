"""Offline public feed and conservative application planning checks."""
from pathlib import Path

import apply
import ats_form
from build import board_discover
import smartrecruiters
import workable

FIXTURES = Path(__file__).parent / 'fixtures'
SHAPE = {'company', 'title', 'location', 'description', 'url', 'source', 'token',
         'job_id', 'apply_url', 'salary', 'posted_at'}


def test_workable_fetcher(monkeypatch):
    calls = []
    monkeypatch.setattr(apply, 'load_keys', lambda: {})
    def post(url, payload):
        calls.append((url, payload))
        return {'results': [{'shortcode': 'ABC123', 'title': 'Remote Program Manager',
                             'state': 'published', 'remote': True, 'location': {'country': 'United States'},
                             'published': '2026-10-24T00:00:00Z'}]}
    monkeypatch.setattr(apply, 'post_json', post)
    monkeypatch.setattr(apply, 'get_json', lambda url: {'jobs': [{'shortcode': 'ABC123',
        'description': '<p>Lead programs across teams.</p>'}]})
    jobs = list(apply.fetch_jobs({'workable': {'Example': 'example'}}))
    assert len(jobs) == 1 and set(jobs[0]) == SHAPE
    assert jobs[0]['source'] == 'workable' and jobs[0]['job_id'] == 'ABC123'
    assert jobs[0]['apply_url'] == 'https://apply.workable.com/example/j/ABC123/apply'
    assert jobs[0]['description'] == 'Lead programs across teams.'
    assert calls[0][0].endswith('/api/v3/accounts/example/jobs')


def test_smartrecruiters_fetcher(monkeypatch):
    monkeypatch.setattr(apply, 'load_keys', lambda: {})
    def get(url):
        if url.endswith('/42'):
            return {'id': '42', 'name': 'Program Manager', 'postingUrl': 'https://jobs.smartrecruiters.com/example/42',
                    'applyUrl': 'https://jobs.smartrecruiters.com/example/42?oga=true',
                    'location': {'remote': True, 'country': 'us'}, 'jobAd': {'sections': {
                        'jobDescription': {'text': '<p>Manage projects.</p>'}}}}
        return {'content': [{'id': '42', 'name': 'Program Manager', 'releasedDate': '2026-10-24T00:00:00Z'}],
                'totalFound': 1}
    monkeypatch.setattr(apply, 'get_json', get)
    jobs = list(apply.fetch_jobs({'smartrecruiters': {'Example': 'example'}}))
    assert len(jobs) == 1 and set(jobs[0]) == SHAPE
    assert jobs[0]['source'] == 'smartrecruiters' and jobs[0]['job_id'] == '42'
    assert jobs[0]['description'] == 'Manage projects.'
    assert jobs[0]['location'].startswith('Remote - ')


def test_discovery_only_keeps_relevant_boards(monkeypatch):
    monkeypatch.setattr(apply, 'post_json', lambda *a: {'results': [{'shortcode': 'ABC123', 'title': 'Remote Program Manager',
        'remote': True, 'location': {'country': 'United States'}}]})
    monkeypatch.setattr(board_discover, 'get', lambda *a: {'content': [{'name': 'Office Janitor',
        'location': {'country': 'US', 'remote': False}}]})
    assert board_discover.probe('workable', 'example')[0][0] == 'Remote Program Manager'
    assert board_discover.probe('smartrecruiters', 'example')[0][0] == 'Office Janitor'
    assert board_discover.relevant(board_discover.probe('smartrecruiters', 'example'), {}) == 0


def test_routes_when_enabled_and_dry_never_submits(monkeypatch):
    class Page:
        def goto(self): pass
    class Context:
        def new_page(self): return Page()
    for source, module in [('workable', workable), ('smartrecruiters', smartrecruiters)]:
        calls = []
        monkeypatch.setattr(module, 'apply_job', lambda page, job, prof, bank, dry: (
            calls.append(dry) or ('ready', 'filled')))
        assert apply.process({'source': source}, {source + '_auto': True, 'resume_variants': False},
                             [], False, Context()) == ('ready', 'filled')
        assert calls == [True]


def test_local_form_fixtures_block_unapproved_questions():
    from bs4 import BeautifulSoup
    for source, fixture in [('workable', 'workable_form.html'),
                            ('smartrecruiters', 'smartrecruiters_form.html')]:
        soup = BeautifulSoup((FIXTURES / fixture).read_text(), 'html.parser')
        fields = []
        for i, control in enumerate(soup.select('input, textarea')):
            label = soup.find('label', attrs={'for': control.get('id')})
            question = label.get_text(' ', strip=True) if label else (control.find_parent('fieldset').legend.get_text(' ', strip=True) if control.find_parent('fieldset') else '')
            fields.append({'i': i, 'type': control.get('type', 'text'), 'tag': control.name,
                           'name': control.get('name', ''), 'label': question, 'question': question,
                           'heading': '', 'required': control.has_attr('required'), 'options': [question]})
        actions, missing = ats_form.plan(fields, {'company': 'Example'}, {'resume': '/tmp/resume.pdf'}, [])
        assert actions == [] and missing
        assert any('arbitration' in m.lower() if source == 'workable' else 'consent' in m.lower() for m in missing)


def test_unknown_required_question_blocks_without_fabrication(monkeypatch):
    monkeypatch.setattr(apply, 'resolve', lambda *a: '')
    fields = [{'i': 0, 'type': 'text', 'tag': 'input', 'name': 'question', 'label': 'Secret qualification',
               'question': 'Secret qualification', 'heading': '', 'required': True, 'options': []}]
    actions, missing = ats_form.plan(fields, {'company': 'Example'}, {'resume': '/tmp/resume.pdf'}, [])
    assert actions == [] and missing == ['Secret qualification']


def test_workable_machine_name_fields_use_profile_facts(monkeypatch):
    seen = []
    def resolve(label, profile, bank, company):
        seen.append(label)
        return {'First name': profile['first_name'], 'Last name': profile['last_name']}.get(label, '')
    monkeypatch.setattr(apply, 'resolve', resolve)
    fields = [{'i': i, 'type': 'text', 'tag': 'input', 'name': name, 'label': name,
               'question': '', 'heading': '', 'required': True, 'options': []}
              for i, name in enumerate(('firstname', 'lastname'))]
    actions, missing = ats_form.plan(fields, {'company': 'Example'},
                                     {'first_name': 'Jane', 'last_name': 'Doe'}, [])
    assert missing == [] and [a[2] for a in actions] == ['Jane', 'Doe']
    assert seen == ['First name', 'Last name']
