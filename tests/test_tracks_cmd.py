import json
import sqlite3
import stat

import pytest
import state
import tracks_cmd


@pytest.fixture
def workspace(tmp_path):
    conn = sqlite3.connect(':memory:')
    state.migrate(conn)
    rows = [
        ('name', 'Jane Doe'), ('email', 'jane@example.com'), ('phone', '555-010-1234'),
        ('city', 'Columbus'), ('state', 'OH'),
        ('role', 'Project Manager | Example Co | 2020 - Present'),
        ('role_bullet', 'Project Manager | Example Co | 2020 - Present: Led approved projects.'),
        ('role_bullet', 'Project Manager | Example Co | 2020 - Present: Tracked approved budgets.'),
        ('education', 'Example University'), ('skill', 'Planning: Scheduling and budgets')]
    conn.executemany("INSERT INTO profile_facts(key,value,source,approved,status) VALUES (?,?,'resume:test',1,'approved')", rows)
    conn.execute("CREATE TABLE IF NOT EXISTS jobs (url TEXT PRIMARY KEY, title TEXT, description TEXT, status TEXT)")
    conn.execute("INSERT INTO jobs(url,title,description,status) VALUES ('fixture','Project Manager','projects','new')")
    conn.commit()
    (tmp_path / 'profile.json').write_text(json.dumps({'name': 'Jane Doe'}))
    yield conn, tmp_path
    conn.close()


def scripted(*answers):
    it = iter(answers)
    return lambda prompt: next(it)


def test_init_approved_and_private(workspace):
    conn, root = workspace
    out = []
    f = tracks_cmd.run('init', conn, root, write=out.append)
    path = root / 'resume_facts.json'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert f['track_settings']['default'] == 'General'
    assert f['variants']['General']['order'] == [['job1', ['bullet1', 'bullet2']]]
    assert f['jobs']['job1'][4]['bullet1'] == 'Led approved projects.'
    assert any('Project Manager: General' in line for line in out)


def test_add_edit_default_remove_and_list(workspace):
    conn, root = workspace
    tracks_cmd.run('init', conn, root, write=lambda _: None)
    tracks_cmd.run('add', conn, root, name='Focused', read=scripted('project manager', 'My headline', 'My summary', '1', '2,1', '1'), write=lambda _: None)
    f = json.loads((root / 'resume_facts.json').read_text())
    assert f['variants']['Focused']['order'] == [['job1', ['bullet2', 'bullet1']]]
    tracks_cmd.run('edit', conn, root, name='Focused', read=scripted('', 'New headline', '', '', '', ''), write=lambda _: None)
    assert tracks_cmd.run('list', conn, root, write=lambda _: None)['variants']['Focused']['headline'] == 'New headline'
    tracks_cmd.run('default', conn, root, name='Focused', write=lambda _: None)
    assert tracks_cmd.run('list', conn, root, write=lambda _: None)['track_settings']['default'] == 'Focused'
    tracks_cmd.run('remove', conn, root, name='General', write=lambda _: None)
    with pytest.raises(ValueError, match='last track'):
        tracks_cmd.run('remove', conn, root, name='Focused', write=lambda _: None)


def test_refuse_overwrite_and_backup(workspace):
    conn, root = workspace
    tracks_cmd.run('init', conn, root, write=lambda _: None)
    path = root / 'resume_facts.json'
    original = path.read_text()
    with pytest.raises(ValueError, match='already exists'):
        tracks_cmd.run('init', conn, root, write=lambda _: None)
    assert path.read_text() == original
    tracks_cmd.run('init', conn, root, force=True, write=lambda _: None)
    assert json.loads((root / 'resume_facts.json.bak').read_text()) == json.loads(original)
    assert stat.S_IMODE((root / 'resume_facts.json.bak').stat().st_mode) == 0o600


def test_no_approved_facts(tmp_path):
    conn = sqlite3.connect(':memory:')
    state.migrate(conn)
    with pytest.raises(ValueError, match='resume-import'):
        tracks_cmd.run('init', conn, tmp_path)
