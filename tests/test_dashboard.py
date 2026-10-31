"""The dashboard reports live job state without writing to the applications DB."""

import sqlite3

import dashboard


def test_snapshot_prioritizes_action_queue_and_keeps_links_safe(tmp_path):
    path = tmp_path / "applications.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE jobs (url TEXT, apply_url TEXT, company TEXT, title TEXT, "
                 "location TEXT, salary TEXT, fit REAL, status TEXT, notes TEXT, source TEXT, "
                 "updated TEXT, found_at TEXT, applied_at TEXT)")
    conn.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        ("https://example.com/one", "https://example.com/apply", "Acme", "Program Manager",
         "Remote", "$100k", 83.7, "needs_you", "Question requires review", "greenhouse", "", "", ""),
        ("https://example.com/two", "javascript:alert(1)", "Beta", "Project Manager",
         "Remote", "", 94, "staged", "", "lever", "", "", ""),
        ("https://example.com/three", "", "Gamma", "Operations Manager",
         "Remote", "", 75, "applied", "", "ashby", "", "", "2026-10-30"),
    ])
    conn.execute("CREATE TABLE attempts (id INTEGER, job_url TEXT)")
    conn.execute("CREATE TABLE blockers (attempt_id INTEGER, status TEXT)")
    conn.execute("INSERT INTO attempts VALUES (1, 'https://example.com/one')")
    conn.execute("INSERT INTO blockers VALUES (1, 'open')")
    conn.commit()
    conn.close()

    result = dashboard.snapshot(path)
    assert result["totals"] == {"tracked": 3, "action": 2, "applied": 1,
                                "interviews": 0, "offers": 0, "open_blockers": 1}
    assert [job["status"] for job in result["jobs"]] == ["staged", "needs_you"]
    assert result["jobs"][0]["link"] == "https://example.com/two"
    assert result["jobs"][1]["blockers"] == 1
    assert result["jobs"][1]["fit"] == 84
    assert sqlite3.connect(path).execute("SELECT count(*) FROM jobs").fetchone()[0] == 3


def test_missing_database_has_empty_dashboard(tmp_path):
    result = dashboard.snapshot(tmp_path / "missing.sqlite3")
    assert result["database_found"] is False
    assert result["jobs"] == []
