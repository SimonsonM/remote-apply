"""Title filter drops IC engineering roles; on-site/hybrid roles near home qualify."""
import apply
import sqlite3

PROF = {'focus_terms': ['program'], 'local_radius_miles': 20}


def job(title, loc):
    return {'title': title, 'location': loc, 'description': 'program work'}


def test_ic_engineering_titles_rejected():
    for t in ('Senior Machine Learning Operations Engineer', 'Senior .NET Developer', 'Principal Engineer (AI)'):
        assert not apply.role_ok(t), t
    for t in ('Technical Program Manager', 'Engineering Program Manager', 'Implementation Manager'):
        assert apply.role_ok(t), t


def test_local_onsite_accepted_far_onsite_rejected():
    assert apply.score(job('Project Manager', 'Columbus, OH (On-site)'), PROF, False) is not None
    assert apply.score(job('Project Manager', 'Dublin, Ohio'), PROF, False) is not None
    assert apply.score(job('Project Manager', 'Dayton, OH (Hybrid)'), PROF, False) is None
    assert apply.score(job('Project Manager', 'Clinton, NY'), PROF, False) is None
    assert apply.score(job('Project Manager', 'Remote - US'), PROF, False) is not None


def test_metro_zone_only_when_enabled(monkeypatch):
    cincy = (39.1031, -84.512)
    monkeypatch.setattr(apply, 'METRO_CENTER', cincy)
    monkeypatch.setattr(apply, 'METRO_NAME', 'cincinnati')
    monkeypatch.setattr(apply, 'METRO_STATES', ['oh', 'ohio', 'ky', 'kentucky'])
    monkeypatch.setattr(apply, 'METRO_PLACES', {'cincinnati': cincy, 'covington': (39.0837, -84.5086)})
    on = dict(PROF, metro=True)
    assert apply.score(job('Program Manager', 'Hybrid - Cincinnati, OH'), on, False) is not None
    assert apply.score(job('Program Manager', 'Onsite - Covington, KY'), on, False) is not None
    assert apply.score(job('Program Manager', 'Covington, GA'), on, False) is None
    assert apply.score(job('Program Manager', 'Cincinnati, OH'), PROF, False) is None


def test_ai_program_roles_get_priority_points_not_ai_engineers():
    p = dict(PROF)
    assert apply.score(job('Senior Manager - Data & AI Governance', 'Remote - US'), p, False) >= 12
    assert apply.score(job('Staff AI Engineer', 'Remote - US'), p, False) is None


def test_overlevel_titles_get_low_fit(tmp_path):
    titles = ('Director of Product Management', 'Staff Technical Program Manager',
              'Senior Program Manager', 'Chief of Staff', 'Project Manager')
    conn = sqlite3.connect(tmp_path / 'jobs.sqlite3')
    conn.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, title TEXT, status TEXT, notes TEXT)")
    conn.executemany("INSERT INTO jobs VALUES (?, ?, 'new', '')", [(str(i), title) for i, title in enumerate(titles)])
    apply.mark_overlevel_jobs(conn)
    assert conn.execute("SELECT status, notes FROM jobs ORDER BY url").fetchall() == [
        ('low_fit', 'overlevel'), ('low_fit', 'overlevel'),
        ('new', ''), ('new', ''), ('new', '')]
    conn.close()
