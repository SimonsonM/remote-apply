"""Option matching for country spellings and confirm-style consent options."""
import apply

STRIPE = ['Australia', 'Canada', 'The Netherlands', 'UAE', 'UK', 'US', 'Other']


def test_country_alias_to_short_code():
    assert apply.pick_option('United States', STRIPE) == 'US'


def test_country_alias_to_long_form():
    assert apply.pick_option('United States', ['Canada', 'United States of America']) == 'United States of America'


def test_us_does_not_match_other_countries():
    assert apply.pick_option('United States', ['Australia', 'UAE', 'Other']) is None


def test_agree_matches_confirmed_and_acknowledge():
    assert apply.pick_option('__agree__', ['Confirmed']) == 'Confirmed'
    assert apply.pick_option('__agree__', ['Acknowledge']) == 'Acknowledge'
    assert apply.pick_option('__agree__', ['I accept']) == 'I accept'


def test_agree_still_prefers_explicit_agree_over_no():
    assert apply.pick_option('__agree__', ['No', 'Yes']) == 'Yes'


def test_long_answer_prefix_matches_option():
    opts = ['I regularly use AI tools to speed up existing tasks (e.g., drafting).',
            'I design or automate workflows with AI tools (e.g., building agents, integrating AI into team processes).']
    assert apply.pick_option('I design or automate workflows with AI tools', opts) == opts[1]


def test_waivers_are_never_answered_automatically(monkeypatch):
    prof = apply.prepare_profile({'name': 'Jane Doe', 'resume': __file__})
    for q in ['Please confirm receipt of the above linked Global Data Privacy Notice and US Arbitration Agreement.',
              'I agree to waive my right to a jury trial',
              'Do you accept the class action waiver?',
              'I agree to the mutual arbitration agreement']:
        assert apply.resolve(q, prof, [], 'Coinbase') == ''


def test_noncompete_status_question_still_answered_but_agreement_blocked():
    prof = apply.prepare_profile({'name': 'Jane Doe', 'resume': __file__})
    assert apply.WAIVER.search('I agree to sign a non-compete agreement as a condition of employment')
    assert not apply.WAIVER.search('Are you subject to any employment agreements and/or post-employment restrictions such as a non-compete?')
