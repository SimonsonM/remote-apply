import datetime as dt
import json
import sqlite3

import answer_store
import answer_wizard
import apply
import followup
import state


def make_db(tmp_path):
    conn = sqlite3.connect(tmp_path / 'wizard.sqlite3')
    state.migrate(conn)
    conn.execute('CREATE TABLE jobs(url TEXT PRIMARY KEY,company TEXT,title TEXT)')
    conn.execute("INSERT INTO jobs VALUES ('form-one','Example','Tester')")
    return conn


def add(conn, category, pattern, answer='Fixture answer'):
    cur = conn.execute("INSERT INTO answer_store(category,question_text,match_regex,answer,approved,status,scope,ts) VALUES (?,?,?,?,0,'pending','*',?)",
                       (category, pattern, pattern, answer, state.now()))
    conn.commit()
    return cur.lastrowid


def scripted(*answers):
    choices = iter(answers)
    return lambda _prompt: next(choices)


def test_wizard_all_basic_choices_and_resume(tmp_path):
    with make_db(tmp_path) as conn:
        ids = [add(conn, 'profile_fact', f'^Question {i}$') for i in range(5)]
        lines = []
        answer_wizard.run(conn, reports_dir=tmp_path, read=scripted('y', 'e', 'Updated text', 'n', 'e', 'Edited', 'y', 'n', 's', 'q'), write=lines.append, interactive=True)
        assert [r[0] for r in conn.execute('SELECT status FROM answer_store ORDER BY id')] == ['approved', 'approved', 'rejected', 'pending', 'pending']
        assert conn.execute('SELECT answer FROM answer_store WHERE id=?', (ids[1],)).fetchone()[0] == 'Edited'
        assert 'Summary: 2 approved, 1 rejected, 1 skipped' in lines
        assert any('2 of 5' in line for line in lines)
        answer_wizard.run(conn, reports_dir=tmp_path, read=scripted('y', 'y'), write=lines.append, interactive=True)
        assert conn.execute("SELECT count(*) FROM answer_store WHERE status='pending'").fetchone()[0] == 0


def test_restricted_requires_yes_and_standing_last(tmp_path):
    with make_db(tmp_path) as conn:
        ident = add(conn, 'eeo', '^Gender$', '__decline__')
        lines = []
        prompts = []
        values = iter(('y', 'y', 's'))
        def read(prompt):
            prompts.append(prompt)
            return next(values)
        answer_wizard.run(conn, reports_dir=tmp_path, read=read, write=lines.append, interactive=True)
        assert conn.execute('SELECT status FROM answer_store WHERE id=?', (ident,)).fetchone()[0] == 'pending'
        assert conn.execute('SELECT count(*) FROM standing_authorizations').fetchone()[0] == 0
        assert any('Decline to self-identify' in line for line in lines)
        assert 'Type yes' in prompts[1]
        prompts.clear()
        values = iter(('y', 'yes', 'y'))
        answer_wizard.run(conn, reports_dir=tmp_path, read=read, write=lines.append, interactive=True)
        assert conn.execute('SELECT status FROM answer_store WHERE id=?', (ident,)).fetchone()[0] == 'approved'
        assert conn.execute('SELECT count(*) FROM standing_authorizations').fetchone()[0] == 1
        assert prompts[-1].startswith('Apply this decision')
        expiry = conn.execute('SELECT expires FROM standing_authorizations').fetchone()[0]
        assert 179 <= (dt.datetime.fromisoformat(expiry) - dt.datetime.now(dt.timezone.utc)).days <= 180


def test_reject_never_resolves_and_reopen(tmp_path):
    with make_db(tmp_path) as conn:
        ident = add(conn, 'other', '^Favorite tool$')
        answer_wizard.run(conn, reports_dir=tmp_path, read=scripted('n'), write=lambda _: None, interactive=True)
        assert answer_store.resolve(conn, 'Favorite tool', 'Example')[2] == 'unapproved_answer'
        assert answer_wizard.reopen(conn, [ident, 999]) == 1
        assert conn.execute('SELECT status,approved,verified_at FROM answer_store WHERE id=?', (ident,)).fetchone() == ('pending', 0, None)
        answer_wizard.run(conn, reports_dir=tmp_path, read=scripted('y'), write=lambda _: None, interactive=True)
        assert answer_store.resolve(conn, 'Favorite tool', 'Example')[0] == 'Fixture answer'
        assert answer_wizard.reopen(conn, [ident]) == 1
        assert answer_store.resolve(conn, 'Favorite tool', 'Example')[2] == 'unapproved_answer'


def test_eof_and_noninteractive(tmp_path):
    with make_db(tmp_path) as conn:
        add(conn, 'other', '^Tool$')
        lines = []
        def eof(_):
            raise EOFError
        assert answer_wizard.run(conn, reports_dir=tmp_path, read=eof, write=lines.append, interactive=True) == 0
        assert any('Input ended' in line for line in lines)
        assert answer_wizard.run(conn, reports_dir=tmp_path, read=eof, write=lines.append, interactive=False) == 1
        assert conn.execute('SELECT status FROM answer_store').fetchone()[0] == 'pending'


def test_edit_cancel_invalid_choice_category_and_order(tmp_path):
    with make_db(tmp_path) as conn:
        add(conn, 'salary', '^Pay$', '70000')
        add(conn, 'other', '^Tool$', 'Original')
        add(conn, 'profile_fact', '^City$', 'Madison')
        lines = []
        answer_wizard.run(conn, category='other', reports_dir=tmp_path,
                          read=scripted('', 'e', '', 'e', 'Wrong', 'n', 'y'),
                          write=lines.append, interactive=True)
        assert conn.execute("SELECT answer,status FROM answer_store WHERE category='other'").fetchone() == ('Original', 'approved')
        assert any('Choose y, e, n, s, or q' in line for line in lines)
        assert any('Edit discarded' in line for line in lines)
        lines.clear()
        answer_wizard.run(conn, reports_dir=tmp_path, read=scripted('s', 's'), write=lines.append, interactive=True)
        assert any('1 of 2 | answer 3 | profile_fact' in line for line in lines)
        assert any('2 of 2 | answer 1 | salary' in line for line in lines)


def test_history_doctor_summary_status_hints(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(apply, 'DB', tmp_path / 'app.sqlite3')
    monkeypatch.setattr(apply, 'ROOT', tmp_path)
    reports = tmp_path / 'reports'
    reports.mkdir()
    with apply.db() as conn:
        conn.execute("INSERT INTO jobs(url,company,title) VALUES ('form-one','Example','Tester')")
        ident = add(conn, 'other', '^Favorite tool$')
        attempt = state.create_attempt(conn, 'form-one')
        state.record_field(conn, attempt, 'tool', 'Favorite tool', True, 'text', 'unanswered')
        report = {'jobs': [{'url': 'form-one', 'company': 'Example', 'fields': [
            {'label': 'Favorite tool', 'blocker': True, 'resolver_outcome': f'pending_answer:{ident}'}]}]}
        (reports / 'doctor_20261013.json').write_text(json.dumps(report))
        labels, latest = answer_wizard.history(conn, reports)
        assert answer_wizard.usage('^Favorite tool$', labels) == (1, 'Favorite tool')
        assert answer_wizard.still_blocked(conn, latest) == [('Example', 'form-one')]
        assert '.venv/bin/python apply.py answers wizard' in followup.render_page(conn)
    apply.status_cmd()
    assert capsys.readouterr().out.startswith('Next: .venv/bin/python apply.py answers wizard\n')
    with apply.db() as conn:
        answer_store.approve(conn, [ident])
        assert answer_wizard.still_blocked(conn, report) == []
