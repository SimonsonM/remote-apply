"""Track resumes: routing, and per-job tailoring may only reorder approved lines."""
import json
import pytest
import resumes

pytestmark = pytest.mark.skipif(not resumes.FACTS.exists(), reason='resume_facts.json is local-only (PII)')


@pytest.mark.parametrize('title,want', [
    ('Senior Technical Program Manager', 'ProgramMgmt'),
    ('Manager, Implementation Management', 'ProgramMgmt'),
    ('AI Operations Manager', 'AIOps'),
    ('Director, AI Solutions Strategy & Planning', 'AIOps'),
    ('Product Marketing Manager', 'Marketing'),
    ('Director, Marketing Operations', 'Marketing'),
    ('IT Manager', 'ITSecurity'),
    ('Identity and Access Management Analyst', 'ITSecurity'),
    ('Chief of Staff', 'ProgramMgmt'),
])
def test_pick_variant(title, want):
    assert resumes.pick_variant(title) == want


def test_tailoring_only_reorders_approved_lines():
    f = resumes.facts()
    posting = 'We need someone to own IAM and PAM rollout, endpoint hardening, and stakeholder communication.'
    for name, v in f['variants'].items():
        base, tailored = resumes.plan(name), resumes.plan(name, posting)
        assert tailored['summary'] == base['summary'] and tailored['headline'] == base['headline']
        assert sorted(map(tuple, tailored['skills'])) == sorted(map(tuple, base['skills']))
        assert [j for j, _ in tailored['jobs']] == [j for j, _ in base['jobs']]  # job order is fixed
        for (job, a), (_, b) in zip(base['jobs'], tailored['jobs']):
            assert sorted(a) == sorted(b)
            assert set(b) <= set(f['jobs'][job][4].values())


def test_tailoring_moves_relevant_line_up():
    p = resumes.plan('ITSecurity', 'Hiring for IAM and PAM: identity and access management, privileged access management.')
    consult = dict(p['jobs'])['consult']
    assert consult[0].startswith('Work with identity and access management')


def test_for_job_writes_named_file(tmp_path, monkeypatch):
    monkeypatch.setattr(resumes, 'OUT', tmp_path)
    path, variant = resumes.for_job({'title': 'Product Marketing Manager', 'description': 'brand and content'})
    assert variant == 'Marketing' and path.name == resumes.FILENAME and path.exists()
    again, _ = resumes.for_job({'title': 'Product Marketing Manager', 'description': 'brand and content'})
    assert again == path  # cached


@pytest.mark.parametrize('title,want', [
    ('Project Manager - Medical Communications', 'ProgramMgmt'),
    ('Vice President, AI Context Content Management', 'AIOps'),
    ('Content Marketing Manager', 'Marketing'),
])
def test_ambiguous_words(title, want):
    assert resumes.pick_variant(title) == want
