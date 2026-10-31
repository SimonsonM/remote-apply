import sqlite3

import mail_sort


class Mailbox:
    def __init__(self, bodies):
        self.bodies = bodies
        self.selected = None

    def select(self, mailbox, readonly=False):
        self.selected = mailbox

    def uid(self, command, uid, query):
        assert self.selected == '"Folders/Job Applications"'
        assert query == "(BODY.PEEK[TEXT])"
        return "OK", [(b"", self.bodies[uid])]


def test_rejections_match_sender_domain_without_double_counting(tmp_path, monkeypatch):
    monkeypatch.setattr(mail_sort, "__file__", str(tmp_path / "mail_sort.py"))
    conn = sqlite3.connect(tmp_path / "applications.sqlite3")
    conn.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT, status TEXT, updated TEXT)")
    conn.executemany("INSERT INTO jobs (url, company, title, status) VALUES (?, ?, ?, ?)", [
        ("mercury", "Mercury Inc.", "Program Manager", "applied"),
        ("grafana-a", "Grafana Labs", "Program Manager", "applied"),
        ("grafana-b", "Grafana Labs", "Project Manager", "applied"),
        ("microsoft", "Microsoft Corp", "Project Manager", "rejected"),
    ])
    conn.commit()
    mailbox = Mailbox({
        b"1": b"We will not be moving forward with your application.",
        b"2": b"We will not be moving forward with your application.",
        b"3": b"We will not be moving forward with your application.",
        b"4": b"We will not be moving forward with your application.",
    })
    hits = [
        (b"1", 'Mercury Recruiting <no-reply@mercury.com>', "Application update"),
        (b"2", 'Grafana Recruiting <jobs@grafana.com>', "Application update"),
        (b"3", 'Microsoft Careers <jobs@microsoft.com>', "Application update"),
        (b"4", 'Mercury Recruiting <no-reply@mercury.com>', "Application update"),
    ]
    assert mail_sort.log_rejections(mailbox, '"Folders/Job Applications"', hits) == 1
    assert dict(conn.execute("SELECT url, status FROM jobs")) == {
        "mercury": "rejected", "grafana-a": "applied", "grafana-b": "applied", "microsoft": "rejected"
    }
    conn.close()
