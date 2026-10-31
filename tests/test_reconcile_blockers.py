"""Open answer blockers close once the bank can answer them; waivers never do."""
import sqlite3

import apply
import state


def test_reconcile(tmp_path, monkeypatch):
    monkeypatch.setattr(apply, 'DB', tmp_path / 'r.sqlite3')
    with apply.db() as c:
        c.execute("INSERT INTO jobs(url,company,title) VALUES ('u','Stripe','TPM')")
        a = state.create_attempt(c, 'u')
        for kind, label in [('text', 'Please select the country where you currently reside.'),
                            ('text', 'Please confirm receipt of the US Arbitration Agreement.'),
                            ('file', 'resume'),
                            ('text', 'Totally unknown question about llamas?')]:
            c.execute("INSERT INTO blockers(attempt_id,label,kind,status,ts) VALUES (?,?,?,'open','t')", (a, label, kind))
        c.execute("INSERT INTO answer_store(match_regex,answer,approved,status,scope,ts,category) VALUES ('country.*reside','United States',1,'approved','*','t','other')")
        c.commit()
        assert apply.reconcile_blockers(c, {}) == 1
        left = {r[0] for r in c.execute("SELECT label FROM blockers WHERE status='open'")}
    assert left == {'Please confirm receipt of the US Arbitration Agreement.', 'resume', 'Totally unknown question about llamas?'}


def test_rejected_draft_retries_without_clearing_waiver(tmp_path, monkeypatch):
    monkeypatch.setattr(apply, 'DB', tmp_path / 'r.sqlite3')
    with apply.db() as conn:
        conn.execute("INSERT INTO jobs(url,company,title,fit,status) VALUES ('u','Example','Manager',70,'needs_you')")
        attempt = state.create_attempt(conn, 'u')
        conn.execute("INSERT INTO blockers(attempt_id,label,kind,status,ts) VALUES (?,?,?,'open','t')",
                     (attempt, 'Cover letter', 'draft_rejected'))
        conn.execute("INSERT INTO blockers(attempt_id,label,kind,status,ts) VALUES (?,?,?,'open','t')",
                     (attempt, 'Arbitration agreement', 'checkbox'))
        conn.commit()
        assert apply.reconcile_blockers(conn, {}) == 1
        assert conn.execute("SELECT kind FROM blockers WHERE status='open'").fetchall() == [('checkbox',)]
        assert apply.application_candidates(conn, 66) == []
