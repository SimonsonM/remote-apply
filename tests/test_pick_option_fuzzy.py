import apply


def test_numeric_answer_maps_to_range():
    assert apply.pick_option('5', ['0-2', '3-5', '6-8', '9+']) == '3-5'
    assert apply.pick_option('20', ['1-5', '6-10', '11+']) == '11+'
    assert apply.pick_option('0', ['Less than 1', '1-3']) == 'Less than 1'


def test_degree_answer_matches_curly_apostrophe_option():
    opts = ['High School Diploma/GED', 'Associate', 'Bachelor\u2019s', 'Master\u2019s', 'PhD']
    assert apply.pick_option("Master's degree (MBA)", opts) == 'Master\u2019s'


def test_short_prefix_does_not_false_match():
    assert apply.pick_option('none', ['No', 'Yes']) is None


def test_company_careers_site_alternate():
    assert apply.pick_option('Acme Careers Site|Other', ['LinkedIn Jobs', 'Acme Careers Site', 'Other']) == 'Acme Careers Site'
