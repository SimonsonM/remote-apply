"""Compensation brackets: posted-range midpoint (floor $100k); otherwise senior $125-150k, else $100-125k."""
import apply

BR = ['$0 - $35,000', '$90,000 - $100,000', '$100,000 - $125,000', '$125,000 - $150,000', '$150,000 - $175,000', '$175,000+']
Q = 'Please state your desired compensation (base and incentive combined):'


def pick(job):
    apply._set_current_job(job)
    a = apply.resolve(Q, {}, [], 'X')
    return apply.pick_option(a, BR), apply.text_answer(a)


def test_posted_range_midpoint_with_floor():
    assert pick({'title': 'Manager', 'salary': '$73,100.00 - $109,700.00'})[0] == '$100,000 - $125,000'
    assert pick({'title': 'Lead TPM', 'salary': 'USD 118,000-180,000'})[0] == '$125,000 - $150,000'
    assert pick({'title': 'Sr PM', 'description': 'The base pay range is $150k - $190k.'})[0] == '$150,000 - $175,000'


def test_no_range_uses_title_and_ignores_estimates():
    assert pick({'title': 'Technical Program Manager', 'salary': 'est. USD 139,591 (Adzuna estimate)'})[0] == '$125,000 - $150,000'
    assert pick({'title': 'Project Manager', 'salary': ''}) == ('$100,000 - $125,000', '$100,000 - $125,000')


def test_accept_listed_range_questions_are_not_bracketed():
    apply._set_current_job({'title': 'PM', 'salary': '$90,000 - $110,000'})
    assert '__salary__' not in apply.resolve('Do you accept the listed salary range for this position?', {}, [], 'X')
    assert apply.text_answer('__decline__') == '' and apply.text_answer('Yes|No') == 'Yes'
