import datetime as dt
import json
import sqlite3

import answer_store
import apply
import state


def test_import_approval_expiry_and_no_raw_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(apply, 'DB', tmp_path / 'test.sqlite3')
    bank_path = tmp_path / 'answers.json'
    bank_path.write_text(json.dumps({'answers': [{'match': '^Favorite tool', 'answer': 'Fixture tool'}]}))
    with apply.db() as conn:
        assert answer_store.import_bank(conn, bank_path) == 1
        assert answer_store.import_bank(conn, bank_path) == 0
        assert apply.resolve('Favorite tool', {}, [{'match': '^Favorite tool', 'answer': 'raw secret'}], 'Fixture Co') == ''
        assert answer_store.resolve(conn, 'Favorite tool', 'Fixture Co')[2:] == ('unapproved_answer', 1)
        assert answer_store.approve(conn, [1]) == 1
        assert apply.resolve('Favorite tool', {}, [], 'Fixture Co') == 'Fixture tool'
        conn.execute('UPDATE answer_store SET review_by=? WHERE id=1', ('2000-01-01T00:00:00+00:00',))
        conn.commit()
        assert answer_store.resolve(conn, 'Favorite tool', 'Fixture Co')[2:] == ('stale_answer', 1)


def test_restricted_answer_needs_explicit_or_live_standing(tmp_path):
    with sqlite3.connect(tmp_path / 'test.sqlite3') as conn:
        state.migrate(conn)
        conn.execute("INSERT INTO answer_store(match_regex,answer,approved,status,category,review_by,scope,ts) VALUES ('^Gender','__decline__',1,'approved','eeo','2999-01-01T00:00:00+00:00','*',?)", (state.now(),))
        assert answer_store.resolve(conn, 'Gender', 'Fixture Co')[2] == 'unapproved_answer'
        now = dt.datetime.now(dt.timezone.utc)
        conn.execute('INSERT INTO standing_authorizations(category,scope,approved_at,expires) VALUES (?,?,?,?)',
                     ('eeo', '{}', now.isoformat(), (now + dt.timedelta(days=1)).isoformat()))
        assert answer_store.resolve(conn, 'Gender', 'Fixture Co')[0] == '__decline__'
        conn.execute("UPDATE standing_authorizations SET expires='2000-01-01T00:00:00+00:00'")
        assert answer_store.resolve(conn, 'Gender', 'Fixture Co')[2] == 'unapproved_answer'
        answer_store.approve(conn, [1])
        assert answer_store.resolve(conn, 'Gender', 'Fixture Co')[0] == '__decline__'


def test_confirmation_near_misses():
    assert apply.confirmation_text('Thank you for applying')
    assert apply.confirmation_text('Your application has been submitted')
    assert not apply.confirmation_text('Thank you for your interest in this role')
    assert not apply.confirmation_text('Validation error: application submitted field is missing')


def test_status_snapshot(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(apply, 'DB', tmp_path / 'test.sqlite3')
    with apply.db() as conn:
        conn.execute("INSERT INTO jobs(url,company,title) VALUES ('fixture','Fixture Co','Tester')")
        ident = state.create_attempt(conn, 'fixture')
        state.record_field(conn, ident, 'x', 'Question', True, 'text', 'unanswered')
        state.set_state(conn, ident, 'blocked')
    apply.status_cmd()
    assert capsys.readouterr().out == ('company | title | state | filled/failed/unanswered | open blockers | evidence\n'
                                    'Fixture Co | Tester | blocked | 0/0/1 | Question (text) | n\n')


def test_additive_answer_columns_migrate_once(tmp_path):
    path = tmp_path / 'old.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE jobs(url TEXT PRIMARY KEY)')
        conn.execute("INSERT INTO jobs VALUES ('fixture')")
        conn.execute('CREATE TABLE answer_store(id INTEGER PRIMARY KEY, match_regex TEXT, answer TEXT, ts TEXT)')
        conn.execute("INSERT INTO answer_store(match_regex,answer,ts) VALUES ('^Tool','Fixture','now')")
        conn.commit()
        state.migrate(conn)
        state.migrate(conn)
        assert {'category', 'status'} <= {r[1] for r in conn.execute('PRAGMA table_info(answer_store)')}
        assert conn.execute('SELECT answer FROM answer_store').fetchone()[0] == 'Fixture'
        assert conn.execute('SELECT url FROM jobs').fetchone()[0] == 'fixture'
    assert len(list((tmp_path / 'backups').glob('*.sqlite3'))) == 1
