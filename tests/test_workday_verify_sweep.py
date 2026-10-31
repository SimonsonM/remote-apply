"""Pending Workday verification links found in mail are matched to tenants (no network)."""
import datetime as dt
import email
import email.policy
from email.message import EmailMessage

import workday


def mail(tenant, hours_ago, path='activate/abc123/?redirect=x'):
    m = EmailMessage()
    m['From'] = f'Workday at {tenant.title()} <{tenant}@otp.workday.com>'
    m['Subject'] = 'Verify your candidate account'
    m['Date'] = email.utils.format_datetime(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago))
    m.set_content(f'Click this link https://{tenant}.wd5.myworkdayjobs.com/{tenant}/{path} The link will expire after 24 hours.')
    return m.as_bytes()


class FakeImap:
    boxes = {}

    def __init__(self, *a):
        self.sock = None
        self.folder = None

    def starttls(self, ctx):
        pass

    def login(self, *a):
        pass

    def logout(self):
        pass

    def select(self, folder, readonly=True):
        self.folder = folder
        return ('OK', [b''])

    def search(self, *a):
        msgs = self.boxes.get(self.folder, [])
        return ('OK', [b' '.join(str(i + 1).encode() for i in range(len(msgs)))])

    def fetch(self, uid, what):
        return ('OK', [(b'1', self.boxes[self.folder][int(uid) - 1])])


def test_links_from_inbox_and_folder(monkeypatch):
    FakeImap.boxes = {'INBOX': [mail('woodward', 2)], '"Folders/Job Applications"': [mail('humana', 5), mail('zendesk', 30)]}
    monkeypatch.setattr(workday, '_bridge_client', lambda keys, f=None: FakeImap())
    got = workday.pending_verification_links({'BRIDGE_USER': 'u', 'BRIDGE_PASS': 'p'}, {'woodward', 'humana', 'zendesk', 'jci'})
    assert set(got) == {'woodward', 'humana'}  # zendesk's link is past 24h
    assert got['woodward'].startswith('https://woodward.wd5.myworkdayjobs.com/woodward/activate/')


def test_foreign_host_ignored(monkeypatch):
    bad = mail('woodward', 1).replace(b'woodward.wd5.myworkdayjobs.com', b'evil.example.com')
    FakeImap.boxes = {'INBOX': [bad]}
    monkeypatch.setattr(workday, '_bridge_client', lambda keys, f=None: FakeImap())
    assert workday.pending_verification_links({'BRIDGE_USER': 'u', 'BRIDGE_PASS': 'p'}, {'woodward'}) == {}
