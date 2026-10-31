"""Clean-room first-run checks with scripted terminal input."""
import json
import sqlite3
import stat

import answer_store
import launcher


def test_launcher_setup_then_menu_same_session(tmp_path, monkeypatch):
    import setup_assistant
    import state
    monkeypatch.setattr(launcher, 'ROOT', tmp_path)
    monkeypatch.setattr(setup_assistant.getpass, 'getpass', lambda _: '')
    calls = []
    def setup(commands):
        calls.append(commands)
        with sqlite3.connect(tmp_path / 'applications.sqlite3') as conn:
            state.migrate(conn)
            setup_assistant.run(conn, tmp_path, read=lambda prompt: 's' if prompt.startswith('Resume file path or number') else 'Alex Example' if prompt.startswith('Name ') else '', write=lambda _: None)
    monkeypatch.setattr(launcher, 'run_commands', setup)
    output = []
    launcher.menu(read=lambda _: '0', write=output.append)
    assert calls == [['setup']]
    assert output.count('No profile found. First-time setup will start now.') == 1
    assert '0. Quit' in output
import resume_import
import setup_assistant
import state


def test_full_setup_scripted_input_in_temporary_directory(tmp_path, monkeypatch, capsys):
    resume = tmp_path / 'my_resume.txt'
    resume.write_text('Alex Example\nMadison, WI 53703 | alex@example.test | 555-123-4567\n'
                      'Experience\nEngineer, Acme, Jan 2020 - Present\n- Built controls\n'
                      'Education\nExample University\nCertifications\nExample Certificate\n')
    (tmp_path / 'README.md').write_text('# Project\nSkills\n')
    (tmp_path / 'TECH_BRIEF.md').write_text('# Project\nEducation\n')
    assert setup_assistant.find_resumes(tmp_path, tmp_path) == [resume]
    prompts = []

    def read(prompt):
        prompts.append(prompt)
        if prompt.startswith('Resume file path or number'): return '1'
        if prompt.startswith('Confirm [y]'): return 'y' if 'role' not in prompts[-2].lower() else 'n'
        if prompt.startswith('Keep these as achievement'): return 'n'
        if prompt.startswith('Name '): return 'Alex Example'
        if prompt.startswith('Email '): return 'alex@example.test'
        if prompt.startswith('Phone '): return '555-123-4567'
        if prompt.startswith('Are you legally authorized'): return 'y'
        if prompt.startswith('You are telling employers: yes.'): return 'yes'
        return ''

    monkeypatch.setattr(setup_assistant.getpass, 'getpass', lambda _: 'test-key')
    db = tmp_path / 'applications.sqlite3'
    with sqlite3.connect(db) as conn:
        state.migrate(conn)
        result = setup_assistant.run(conn, tmp_path, read=read)
        assert result is not None
        rows_before = conn.execute('SELECT count(*) FROM answer_store').fetchone()[0]
        result = setup_assistant.run(conn, tmp_path, read=read)
        assert result is not None
        assert conn.execute('SELECT count(*) FROM answer_store').fetchone()[0] == rows_before
        assert conn.execute("SELECT status FROM answer_store WHERE question_text LIKE 'Are you legally authorized%'").fetchone()[0] == 'approved'
        assert conn.execute("SELECT status FROM answer_store WHERE question_text LIKE 'Do you need visa sponsorship%'").fetchone()[0] == 'pending'
        facts = dict(resume_import.approved_facts(conn))
        assert facts['city'] == 'Madison' and facts['state'] == 'WI' and facts['zip'] == '53703'
    profile = json.loads((tmp_path / 'profile.json').read_text())
    assert profile['mode'] == 'prepare'
    assert stat.S_IMODE((tmp_path / 'profile.json').stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / '.env').stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert 'Traceback' not in out
    assert 'Approved answers:' in out and 'Still pending:' in out
    print('TASK 10 end-to-end: no traceback; private files 0600; typed answer approved; skipped attestation pending; no duplicate rows')


def test_invalid_resume_and_menu_eof(tmp_path, monkeypatch):
    conn = sqlite3.connect(':memory:')
    state.migrate(conn)
    answers = iter(['/missing/path', '/missing/path', '/missing/path'])
    output = []
    assert setup_assistant.run(conn, tmp_path, read=lambda _: next(answers), write=output.append) is None
    assert setup_assistant.EXIT_MESSAGE in output
    monkeypatch.setattr(launcher, 'ROOT', tmp_path)
    monkeypatch.setattr(launcher, 'run_commands', lambda _: None)
    launcher.menu(read=lambda _: (_ for _ in ()).throw(EOFError()), write=lambda _: None)


def test_answers_import_twice_does_not_duplicate_rows(tmp_path):
    bank = tmp_path / 'answers.json'
    bank.write_text(json.dumps({'answers': [{'match': 'favorite color', 'answer': 'blue'}]}))
    conn = sqlite3.connect(':memory:')
    state.migrate(conn)
    assert answer_store.import_bank(conn, bank) == 1
    assert answer_store.import_bank(conn, bank) == 0
    assert conn.execute('SELECT count(*) FROM answer_store').fetchone()[0] == 1
