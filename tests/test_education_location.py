"""Location region options, career-site spelling, and Greenhouse education actions."""
import apply

DBX = ['Australia', 'United Kingdom', 'US - Virginia', 'US - West Virginia', 'US - Wisconsin', 'Other Location']


def test_city_state_into_region_option():
    assert apply.pick_option('Milwaukee, WI, United States', DBX) == 'US - Wisconsin'


def test_city_into_country_only_option():
    assert apply.pick_option('Milwaukee, WI, United States', ['Canada', 'United States', 'Other']) == 'United States'


def test_career_site_spelling():
    opts = ['BuiltIn', 'Dropbox Career Site', 'LinkedIn']
    assert apply.pick_option('Dropbox Careers Site|Company website', opts) == 'Dropbox Career Site'


def test_education_actions_added_only_when_form_has_education():
    prof = {'resume': '/x.pdf', 'education': {'school': 'Ohio State University',
            'degree': 'Master of Business Administration (M.B.A.)|Master\'s Degree', 'discipline': 'Business Administration'}}
    with_edu, _ = apply.gh_plan('t', '1', prof, [], 'Stripe', resolver=lambda *a: '', questions={'education': 'education_required', 'questions': []})
    assert ('school--0', 'select', 'Ohio State University') in with_edu
    assert any(a[0] == 'degree--0' for a in with_edu)
    without, _ = apply.gh_plan('t', '1', prof, [], 'Stripe', resolver=lambda *a: '', questions={'questions': []})
    assert without == []
