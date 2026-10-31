"""Per-company caps and the product-management role filter."""
import datetime as dt
import sqlite3

import pytest

import apply


@pytest.mark.parametrize('title,ok', [
    ('Director of Product Management, Agentic Software Delivery', False),
    ('Principal Product Manager, Orbit', False),
    ('Staff Product Manager, Agentic Test Intelligence', False),
    ('Growth Product Manager, Activation & Monetization', False),
    ('Head of Product', False),
    ('Partner Program Manager', True),
    ('Technical Program Manager, AI Platform', True),
    ('Product Operations Manager', True),
    ('Product Manager, Implementation Programs', True),
    ('AI Operations Manager', True),
    ('Business Continuity Management Lead', True),
])
def test_role_filter(title, ok):
    assert apply.role_ok(title) is ok


def jobs_db(tmp_path):
    c = sqlite3.connect(tmp_path / 'caps.sqlite3')
    c.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, status TEXT, applied_at TEXT DEFAULT '')")
    return c


def test_company_submissions_window(tmp_path):
    c = jobs_db(tmp_path)
    today = dt.date.today()
    rows = [('a', 'GitLab', 'applied', today.isoformat()),
            ('b', 'Gitlab, Inc', 'unconfirmed', (today - dt.timedelta(days=11)).isoformat()),
            ('c', 'GitLab', 'applied', (today - dt.timedelta(days=45)).isoformat()),
            ('d', 'GitLab', 'needs_you', today.isoformat()),
            ('e', 'Chime', 'interview', today.isoformat())]
    c.executemany('INSERT INTO jobs VALUES (?,?,?,?)', rows)
    assert apply.company_submissions(c, 30) == {'gitlab': 2, 'chime': 1}
    assert apply.company_submissions(c, 0) == {'gitlab': 1, 'chime': 1}


def test_cap_reasons():
    prof = {}
    assert apply.company_cap_reason('acme', {}, {}, prof) == ''
    assert 'per night' in apply.company_cap_reason('acme', {'acme': 1}, {'acme': 1}, prof)
    assert '30 days' in apply.company_cap_reason('acme', {'acme': 2}, {}, prof)
    assert apply.company_cap_reason('acme', {'acme': 1}, {}, prof) == ''
    assert apply.company_cap_reason('acme', {'acme': 3}, {}, {'company_per_window': 5}) == ''
