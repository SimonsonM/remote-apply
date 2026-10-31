"""Greenhouse react-select fields keyed by API name get the API question label."""
import sqlite3

import apply
import state


def test_question_key_gets_api_label(monkeypatch):
    conn = sqlite3.connect(':memory:')
    state.migrate(conn)
    apply._GH_Q_CACHE.clear()
    calls = []

    def fake(url):
        calls.append(url)
        return {'questions': [
            {'label': 'Do you require <b>sponsorship</b>?', 'required': True,
             'fields': [{'name': 'question_111', 'type': 'multi_value_single_select'}]},
            {'label': 'Which tools?', 'required': False,
             'fields': [{'name': 'question_222[]', 'type': 'multi_value_multi_select'}]},
        ]}
    monkeypatch.setattr(apply, 'get_json', fake)
    job = {'source': 'greenhouse', 'token': 'acme', 'job_id': '9', 'company': 'Acme', 'url': 'u'}
    aid = apply._new_attempt(conn, job, {}) if hasattr(apply, '_new_attempt') else 1
    results = [dict(key='question_111', kind='select', status='failed', error='value readback mismatch', value='No'),
               dict(key='question_222[]', kind='check', status='filled', error='', value='Python')]
    fields = [{'i': 0, 'id': 'question_111', 'name': '', 'label': '', 'question': ''}]
    try:
        apply._record_attempt(conn, aid, job, results, [], fields=fields)
    except Exception:
        pass
    labels = [r[0] for r in conn.execute('SELECT label FROM field_results ORDER BY id')]
    assert labels[0].startswith('Do you require sponsorship')
    assert labels[1].startswith('Which tools')
    assert len(calls) == 1
    req = conn.execute("SELECT required FROM field_results WHERE field_key='question_222[]'").fetchone()[0]
    assert req == 0
