"""Short stored answers into sentence-style options (2026-10-24 run failures)."""
import pytest
import apply


@pytest.fixture(autouse=True)
def citizen(monkeypatch):
    monkeypatch.setattr(apply, 'US_CITIZEN', True)


ENGINE = ['Yes – I am not currently authorized to work in the U.S. without sponsorship, or I will need sponsorship in the future.',
          'No – I am authorized to work in the U.S. and will not require sponsorship at any point in the future.']
LAUNCHDARKLY = ['No, I do not require sponsorship now or in the future to work legally for LaunchDarkly in the United States.',
                'Yes, I currently require sponsorship, or may require sponsorship in the future, to work legally for LaunchDarkly in the United States.']
NADIA = ['I am currently authorized to work in the United States on a full-time basis and will not require sponsorship for a visa now or anytime in the future.',
         'I will require sponsorship now and/or in the future.']
DATADOG = ['Yes, no restriction.', 'Yes, but I will need sponsorship in the future.', 'No, I need sponsorship now.']


@pytest.mark.parametrize('opts', [ENGINE, LAUNCHDARKLY, NADIA])
def test_no_sponsorship(opts):
    want = opts[1] if opts is ENGINE else opts[0]
    assert apply.pick_option("No", opts) == want


def test_engine_no():
    assert apply.pick_option('No', ENGINE) == ENGINE[1]


def test_authorized_yes():
    assert apply.pick_option('Yes', DATADOG) == 'Yes, no restriction.'


def test_non_citizen_never_claims(monkeypatch):
    monkeypatch.setattr(apply, 'US_CITIZEN', False)
    assert apply.pick_option('No', NADIA) is None
    assert apply.pick_option('Yes', DATADOG) is None


def test_negation_option():
    opts = ['I have not previously been employed at Affirm', 'I have been employed at Affirm as a full-time employee',
            'I have been employed at Affirm as a contractor']
    assert apply.pick_option('No', opts) == opts[0]


def test_state_inside_option():
    opts = ['(US) Alabama', '(US) West Virginia', '(US) Wisconsin', '(US) Wyoming']
    assert apply.pick_option('Wisconsin|WI', opts) == '(US) Wisconsin'


def test_contains_does_not_cross_claims():
    assert apply.pick_option('Bachelor', ['Bachelor degree required - I hold one', 'None']) is None


def test_location_preference_not_profile_location():
    assert not any(__import__('re').search(p, 'Location Preference', __import__('re').I) and k == 'location'
                   for p, k in apply.PROFILE_FIELDS)
