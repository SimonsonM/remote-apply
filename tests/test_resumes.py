"""The public fictional example is a complete, renderable fact base."""
import json
from pathlib import Path

import pytest
from docx import Document
import resumes


@pytest.fixture(autouse=True)
def example_facts(tmp_path, monkeypatch):
    source = Path(resumes.__file__).with_name('resume_facts.example.json')
    target = tmp_path / 'resume_facts.json'
    target.write_bytes(source.read_bytes())
    monkeypatch.setattr(resumes, 'FACTS', target)
    monkeypatch.setattr(resumes, 'OUT', tmp_path / 'rendered')


def test_keyword_list_regex_and_default():
    assert resumes.pick_variant('Senior Project Manager') == 'ProjectMgmt'
    assert resumes.pick_variant('Business Intelligence Analyst') == 'DataAnalytics'
    assert resumes.pick_variant('Unrelated Role') == 'ProjectMgmt'


def test_title_priority_over_description():
    assert resumes.pick_variant('Project Manager', 'analytics ' * 100) == 'ProjectMgmt'
    f = resumes.facts()
    f['track_settings']['title_priority'] = ['DataAnalytics', 'ProjectMgmt']
    resumes.FACTS.write_text(json.dumps(f))
    assert resumes.pick_variant('Project Manager and Data Analyst') == 'DataAnalytics'


def test_no_keyword_track_only_default():
    f = resumes.facts()
    f['variants']['General'] = dict(f['variants']['ProjectMgmt'], keywords=[])
    f['track_settings']['default'] = 'General'
    resumes.FACTS.write_text(json.dumps(f))
    assert resumes.pick_variant('Unrelated Role') == 'General'
    assert resumes.pick_variant('Project Manager') == 'ProjectMgmt'


def test_plan_render_and_for_job():
    planned = resumes.plan('DataAnalytics')
    path = resumes.render(planned, resumes.OUT / 'sample.docx')
    assert path.exists()
    content = '\n'.join(p.text for p in Document(path).paragraphs)
    assert 'Jane Doe' in content and 'Built dashboards' in content
    tailored, track = resumes.for_job({'title': 'Data Analyst', 'description': 'SQL dashboards'})
    assert track == 'DataAnalytics' and tailored.exists()
    again, _ = resumes.for_job({'title': 'Data Analyst', 'description': 'SQL dashboards'})
    assert again == tailored


def test_tailoring_only_reorders_approved_lines():
    f = resumes.facts()
    for name in f['variants']:
        base = resumes.plan(name)
        tailored = resumes.plan(name, 'dashboard SQL budget and stakeholders')
        assert (tailored['headline'], tailored['summary']) == (base['headline'], base['summary'])
        assert sorted(map(tuple, tailored['skills'])) == sorted(map(tuple, base['skills']))
        assert [key for key, _ in tailored['jobs']] == [key for key, _ in base['jobs']]
        for (key, original), (_, reranked) in zip(base['jobs'], tailored['jobs']):
            assert sorted(original) == sorted(reranked)
            assert set(reranked) <= set(f['jobs'][key][4].values())
