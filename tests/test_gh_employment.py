"""Greenhouse employment section is planned from profile.employment."""
import apply

PROF = {'employment': {'company': 'Acme Corp', 'title': 'Manager', 'start_month': 'May',
                       'start_year': 2026, 'end': '2026-10-31'}}


def test_current_role_before_end_date():
    acts = apply.gh_employment_actions({'employment': 'employment_required'}, PROF, today='2026-10-22')
    assert ('company-name-0', 'text', 'Acme Corp') in acts
    assert ('title-0', 'text', 'Manager') in acts
    assert ('start-date-month-0', 'select', 'May') in acts
    assert ('start-date-year-0', 'text', '2026') in acts
    assert ('current-role-0_1', 'tick', '1') in acts
    assert not any(a[0].startswith('end-date') for a in acts)


def test_end_date_after_role_ends():
    acts = apply.gh_employment_actions({'employment': 'employment_optional'}, PROF, today='2026-11-02')
    assert ('end-date-month-0', 'select', 'October') in acts
    assert ('end-date-year-0', 'text', '2026') in acts
    assert not any(a[0] == 'current-role-0_1' for a in acts)


def test_no_section_or_no_profile():
    assert apply.gh_employment_actions({}, PROF) == []
    assert apply.gh_employment_actions({'employment': 'employment_required'}, {}) == []
