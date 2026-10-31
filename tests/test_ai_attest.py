"""AI-use / own-work attestations always go to the human ("needs you")."""
import apply


def test_ai_policy_attestation_not_auto_answered(monkeypatch):
    monkeypatch.setattr(apply.answer_store, 'resolve', lambda conn, label, company: ('Acknowledge', 'x', '', 1))
    label = ('By checking this box, I confirm I have read the Candidate AI Responsible Use Policy. '
             'I affirm that all materials I submit will reflect my own work and experience.')
    assert apply.resolve(label, {}, [], 'Twilio') == ''


def test_ordinary_ai_experience_question_still_resolves(monkeypatch):
    monkeypatch.setattr(apply.answer_store, 'resolve', lambda conn, label, company: ('Yes', 'x', '', 1))
    assert apply.resolve('Do you have experience with AI/ML?', {}, [], 'X') == 'Yes'


def test_employer_ai_notice_is_acknowledged_but_own_work_claims_are_not():
    notice = 'TruStage uses artificial intelligence (AI) tools to assist in reviewing applications and evaluating qualifications.'
    assert apply.ai_notice_only(notice) and not apply.restricted(notice)
    # Interview no-AI pledges are approved; own-work affirmations stay blocked.
    assert apply.restricted('I affirm that all materials I submit throughout my application will reflect my own work. Candidate AI Responsible Use Policy')


def test_interview_pledge_answered_yes_but_own_work_still_blocked():
    assert apply.resolve('AI Policy for Interviewers', {}, [], 'Samsara').startswith('Yes')
    assert not apply.restricted('Please do not use any AI tools during any part of the interview process.')
    assert apply.restricted('I affirm that all materials I submit will reflect my own work. Candidate AI Responsible Use Policy')
