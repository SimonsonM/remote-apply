"""Grounding: posting facts allowed; invented employers, credentials, metrics rejected."""
import smart

RESUME = ('Operations Manager, Northwind Energy, 2019 - 2021. Project Manager, Contoso Construction. '
          'MBA. Scrum Fundamentals Certificate. Python, Power BI, Azure.')
JOB = {'company': '1Password', 'title': 'Senior Manager, Strategic Operations',
       'description': 'You will lead Strategic Operations and partner with the Lead Product Manager on Tax Agents. '
                      'PMP preferred. Experience with Salesforce and Deal Desk. 5+ years required.'}


def kinds(text):
    return [k for k, _ in smart.grounding_problems(text, RESUME, [], job=JOB)]


def test_posting_titles_and_phrases_allowed():
    assert smart.check_grounding('I am excited about the Senior Manager role on Strategic Operations at 1Password.', RESUME, job=JOB)
    assert smart.check_grounding('I would partner closely with the Lead Product Manager and Deal Desk.', RESUME, job=JOB)
    assert smart.check_grounding('My work at Northwind and Contoso Construction maps to this.', RESUME, job=JOB)


def test_invented_employer_rejected_even_if_words_in_posting():
    assert 'employer' in kinds('I previously worked at Deal Desk Partners.') or 'name' in kinds('I previously worked at Deal Desk Partners.')
    assert 'employer' in kinds('Before that I joined Salesforce as a manager.')
    assert kinds('I spent three years at Acme Robotics.')


def test_credentials_resume_only():
    assert 'credential' in kinds('As a PMP, I run tight programs.')
    assert 'credential' in kinds('I am PMP-certified.')
    assert 'credential' in kinds('I hold the Certified Scrum Master credential.')
    assert smart.check_grounding('I earned the Scrum Fundamentals Certificate.', RESUME, job=JOB)
    assert smart.check_grounding('I do not hold a PMP, but I run disciplined programs.', RESUME, job=JOB)
    assert smart.check_grounding('The PMP preference is noted; my MBA covers the fundamentals.', RESUME, job=JOB)
    assert 'credential' in kinds('I have my PMP and five years of delivery.')


def test_invented_metrics_rejected():
    assert 'number' in kinds('I cut costs by 37% across 12 years.')
    assert smart.check_grounding('The role asks for 5+ years, and my MBA helps.', RESUME, job=JOB)


def test_unknown_product_name_rejected():
    assert kinds('I have deep experience with Nitro Enclaves.')


def test_reasons_are_reported():
    probs = smart.grounding_problems('I worked at Acme Robotics and hold a CISSP.', RESUME, [], job=JOB)
    assert smart.format_problems(probs)


def test_job_title_company_acronyms_plurals_and_resume_year_range():
    resume = 'State University. Developer at Acme, 2012 - 2016. Built an SDK.'
    job = {'company': 'Example Partners', 'title': 'Senior Partner Success Manager',
           'description': 'Work with SDKs and partner teams.'}
    text = ('I am applying for Senior Partner Success Manager at Example Partners. '
            'My State University work in 2014 included SDKs.')
    assert smart.grounding_problems(text, resume, job=job) == []
    assert ('number', '2018') in smart.grounding_problems('I worked there in 2018.', resume, job=job)


def test_generic_certificate_wording_is_not_a_credential_claim():
    assert smart.check_grounding('These certifications are specific to the role.', RESUME, job=JOB)
    assert smart.check_grounding('I can discuss specific certifications.', RESUME, job=JOB)
    assert 'credential' in kinds('I hold the Certified Scrum Master credential.')
    assert ('never_claim', 'SCADA') in smart.grounding_problems('I have SCADA experience.', RESUME, ['SCADA'], job=JOB)


def test_cover_letter_handles_empty_or_failed_model(monkeypatch, tmp_path):
    class BrokenPredictor:
        def __call__(self, **kwargs):
            raise TypeError("'NoneType' object is not subscriptable")

    monkeypatch.setattr(smart, '_setup', lambda *args, **kwargs: None)
    monkeypatch.setattr(smart, '_cover_pred', BrokenPredictor)
    monkeypatch.setattr(smart, '_cl', BrokenPredictor())
    monkeypatch.setattr(smart, 'grounded_resume_text', lambda profile: RESUME)
    monkeypatch.setattr(smart, 'COVER_DIR', tmp_path)
    profile = {'name': 'Test Person', 'email': 'test@example.com', 'phone': '555-0100'}
    letter, path = smart.cover_letter(profile, {**JOB, 'job_id': 'fixture'})
    assert 'Senior Manager, Strategic Operations' in letter
    assert path.exists()
