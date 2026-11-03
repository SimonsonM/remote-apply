"""Offline Workday safety checks; no browser or mail network calls."""
import datetime as dt
from email.message import EmailMessage

import apply
import workday


class Element:
    def __init__(self, label, kind='checkbox', required=True):
        self.label, self.kind, self.required = label, kind, required
        self.checked = False
        self.value = ''

    def get_attribute(self, name, timeout=None):
        return {'id': self.label, 'type': self.kind, 'required': '' if self.required else None}.get(name)

    def evaluate(self, script):
        if 'tagName' in script:
            return 'INPUT'
        return self.label

    def input_value(self):
        return self.value

    def check(self, force=False, timeout=None):
        self.checked = True

    def is_checked(self):
        return self.checked

    def fill(self, value):
        self.value = value

    def count(self):
        return 1


class Locator:
    def __init__(self, els):
        self.els = els
    def all(self):
        return self.els
    def wait_for(self, **kwargs): pass
    def count(self): return len(self.els)
    def is_enabled(self): return True
    @property
    def first(self): return self.els[0] if self.els else self


class Page:
    def __init__(self, els):
        self.els = els
    def locator(self, selector):
        if selector.startswith('[id="'):
            return Locator([e for e in self.els if f'[id="{e.label}"]' == selector])
        return Locator([] if 'radio' in selector or 'listbox' in selector or 'multiselect' in selector else self.els)
    def evaluate(self, script, arg=None):
        return [f'[id="{e.label}"]' for e in self.els] if '.map(' in script else len(self.els)
    def wait_for_timeout(self, ms): pass


def fill(els, monkeypatch):
    monkeypatch.setattr(workday, '_field_label', lambda e: e.label)
    monkeypatch.setattr(apply, '_read_back', lambda e, kind, expected: e.value == expected)
    monkeypatch.setattr(apply, 'resolve', lambda label, prof, bank, company: '')
    results, fields = [], []
    missing = workday._fill_page(Page(els), {'company': 'Acme'}, {}, [], results, fields)
    return results, missing


def test_waiver_and_ai_attestation_stop(monkeypatch):
    for label in ('I agree to binding arbitration and waive class action rights',
                  'I certify this is my own work without AI assistance'):
        el = Element(label)
        results, missing = fill([el], monkeypatch)
        assert not el.checked
        assert results[0]['status'] == 'failed'
        assert label in results[0]['key']


def test_unknown_required_and_eeo_decline(monkeypatch):
    unknown = Element('How many years of domain experience?', 'text')
    results, missing = fill([unknown], monkeypatch)
    assert missing == [unknown.label]
    assert not results
    monkeypatch.setattr(apply, 'resolve', lambda label, *args: '')
    assert workday._resolve('Disability status', {}, [], {'company': 'Acme'}) == '__decline__'


def test_unverified_resume_parse_blocks(monkeypatch):
    monkeypatch.setattr(workday, '_field_label', lambda e: e.label)
    monkeypatch.setattr(apply, 'resolve', lambda *args: '')
    monkeypatch.setattr(workday.smart, 'grounded_resume_text', lambda prof: 'Acme Corp Manager')
    el=Element('Previous employer', 'text')
    el.value='Invented Corp'
    results, fields = [], []
    assert workday._fill_page(Page([el]), {'company':'Acme'}, {'employment':{'company':'Acme Corp'}}, [], results, fields, 'My Experience') == []
    assert results[0]['status'] == 'failed' and results[0]['error'] == 'parsed value not verified'


def test_no_parsed_entries_is_allowed_resume_attached():
    # Optional, empty Work Experience/Education is fine; required sections surface as page errors on Save.
    class PageWithoutEntries:
        def locator(self, selector): return Locator([])
    assert workday._experience_blockers(PageWithoutEntries(), {}) == []


def test_sign_in_vs_create_account(monkeypatch):
    class Conn:
        def __init__(self, row): self.row = row
        def execute(self, *args): return self
        def fetchone(self): return self.row
        def commit(self): pass
    class AuthPage:
        def __init__(self): self.clicks=[]
        def inner_text(self, selector): return 'My Information'
        def wait_for_timeout(self, ms): pass
        def wait_for_selector(self, *args, **kwargs): pass
        def wait_for_load_state(self, state): pass
        def locator(self, selector): return Locator([])
    for row, expected in ((None, 'createAccountLink'), (('verified',), 'signInSubmitButton')):
        page=AuthPage()
        monkeypatch.setattr(workday, '_click', lambda p, selector: (p.clicks.append(selector) or True))
        monkeypatch.setattr(workday, '_visible', lambda loc: False)
        monkeypatch.setattr(workday, '_account', lambda *args: None)
        monkeypatch.setattr(workday, '_tenant', lambda job: 'acme')
        result=workday._auth(page, {'url':'https://acme.wd1.myworkdayjobs.com/Careers'}, {},
                             {'WORKDAY_EMAIL':'secret-email','WORKDAY_PASSWORD':'secret-pass'}, Conn(row), [], [])
        assert result == []
        assert any(expected in x for x in page.clicks)


def test_dry_run_never_submits(monkeypatch):
    from contextlib import nullcontext
    class DryPage:
        def goto(self, *args, **kwargs): pass
        def inner_text(self, selector): return 'Review'
        def get_by_role(self, *args, **kwargs): return type('Role', (), {'wait_for': lambda self, **kwargs: None})()
        def locator(self, selector): return Locator([])
        def close(self): pass
        def wait_for_timeout(self, ms): pass
        def wait_for_selector(self, *args, **kwargs): pass
    clicked=[]
    monkeypatch.setattr(apply, 'db', lambda: nullcontext(object()))
    monkeypatch.setattr(apply, '_new_attempt', lambda *args: 1)
    monkeypatch.setattr(apply, '_record_attempt', lambda *args, **kwargs: ('ready_for_review', 'ready'))
    monkeypatch.setattr(workday, '_auth', lambda *args: [])
    monkeypatch.setattr(workday, '_fill_page', lambda *args: [])
    monkeypatch.setattr(workday, '_current_step', lambda *args: 'Review')
    monkeypatch.setattr(workday, '_visible', lambda *args: True)
    monkeypatch.setattr(workday, '_button', lambda page, label: clicked.append(label) or True)
    monkeypatch.setattr(workday, '_screenshot', lambda *args: 'proof.png')
    assert workday.apply_job(DryPage(), {'url':'https://acme.wd1.myworkdayjobs.com/Careers', 'company':'Acme'}, {}, [], False)[0] == 'ready'
    assert not any('Submit' in x for x in clicked)


def test_imap_search_filters_before_fetch(monkeypatch):
    now=dt.datetime.now(dt.timezone.utc)
    good=EmailMessage()
    good['From']='Workday <noreply@myworkday.com>'
    good['Subject']='Verify your account'
    good['Date']=now.strftime('%a, %d %b %Y %H:%M:%S +0000')
    good.set_content('https://acme.wd1.myworkdayjobs.com/verify/abc')
    class Mail:
        def __init__(self):
            self.fetched=[]; self.criteria=()
            self.sock=type('Socket', (), {'getpeercert': lambda self, binary_form: b'certificate'})()
        def starttls(self, context): pass
        def login(self, user, password): pass
        def select(self, box, readonly=False): assert readonly
        def search(self, charset, *criteria):
            self.criteria=criteria
            return 'OK',[b'5']
        def fetch(self, uid, what):
            self.fetched.append((uid,what))
            return 'OK',[(b'5',good.as_bytes())]
        def logout(self): pass
    m=Mail()
    monkeypatch.setattr(workday.ssl, 'DER_cert_to_PEM_cert', lambda cert: 'certificate')
    monkeypatch.setattr(workday.ssl, 'SSLContext', lambda protocol: type('Context', (), {
        'check_hostname': True, 'verify_mode': None, 'load_verify_locations': lambda self, **kwargs: None})())
    link=workday.verification_link({'BRIDGE_USER':'u','BRIDGE_PASS':'p'},timeout=1,imap_factory=lambda *args:m)
    assert link.endswith('/verify/abc')
    assert m.criteria[0] == 'UNSEEN' and m.criteria[1] == 'SINCE'  # Bridge has no YOUNGER; Date header filters to 30 min
    assert 'FROM' in m.criteria and 'SUBJECT' in m.criteria
    assert m.fetched == [(b'5','(BODY.PEEK[])')]


def test_verification_link_rejects_other_hosts():
    msg=EmailMessage()
    msg['From']='Workday <noreply@myworkday.com>'
    msg['Subject']='Verify your account'
    msg['Date']=dt.datetime.now(dt.timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')
    msg.set_content('https://myworkdayjobs.com.evil.example/verify')
    assert workday._verify_message(msg, dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=30)) is None


def test_credentials_redacted_from_errors(monkeypatch):
    monkeypatch.setattr(apply, 'load_keys', lambda: {'WORKDAY_EMAIL':'private@example.com','WORKDAY_PASSWORD':'topsecret'})
    assert 'topsecret' not in workday._safe_error('login topsecret failed')
    assert 'private@example.com' not in workday._safe_error('private@example.com failed')


def test_proof_masks_account_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(apply, 'PROOF', tmp_path)
    monkeypatch.setattr(apply, 'load_keys', lambda: {'WORKDAY_EMAIL':'private@example.com'})
    class ProofPage:
        def __init__(self): self.masks=[]
        def locator(self, selector): return selector
        def get_by_text(self, text, exact=False): return ('text', text, exact)
        def screenshot(self, **kwargs): self.masks=kwargs['mask']
    page=ProofPage()
    workday._screenshot(page, {'company':'Acme','job_id':'R1'})
    assert '[data-automation-id="password"]' in page.masks
    assert ('text','private@example.com',True) in page.masks


def test_tenant_discovery_verifies_company_and_caches(monkeypatch, tmp_path):
    import sqlite3
    import state
    conn=sqlite3.connect(tmp_path/'test.sqlite3')
    state.migrate(conn)
    boards=tmp_path/'boards.json'
    boards.write_text('{"workday": {"Acme Corp": {"host": "acme.wd1", "tenant": "acme", "site": "Careers"}}}')
    monkeypatch.setattr(apply, 'BOARDS', boards)
    calls=[]
    def jobs(url, body):
        calls.append(url)
        return {'jobPostings':[{'title':'Project Manager','externalPath':'/job/project'}]}
    monkeypatch.setattr(apply, '_wd_post', jobs)
    monkeypatch.setattr(apply, 'get_json_h', lambda url: {'jobPostingInfo':{'jobDescription':'Join Acme Corp as a project manager'}})
    assert workday.discover_tenant('Acme Corp', conn)['site'] == 'Careers'
    assert workday.discover_tenant('Acme Corp', conn)['site'] == 'Careers'
    assert len(calls) == 1
    conn.close()


def test_option_with_extra_status_claim_is_not_picked():
    opts = ['Select One', 'Yes-I hold a temporary visa', 'No-I hold a visa status that provides work authorization',
            'N/A-I am a US Citizen or US Resident (Green Card Holder)']
    assert apply.pick_option('No', opts) is None
    assert apply.pick_option('No', ['Yes', 'No-I am a citizen', 'No-I hold a visa']) is None
    assert apply.pick_option('No', ['Yes', 'No, I have not previously worked for Dropbox']).startswith('No, I have not')
    assert apply.pick_option('__decline__', ['Yes, I have a disability', 'I do not want to answer']) == 'I do not want to answer'


def test_us_citizen_flag_unlocks_only_citizen_options(monkeypatch):
    opts = ['Yes-I hold a temporary visa', 'No-I hold a visa status that provides work authorization',
            'N/A-I am a US Citizen or US Resident (Green Card Holder)']
    monkeypatch.setattr(apply, 'US_CITIZEN', False)
    assert apply.pick_option('No|N/A', opts) is None
    monkeypatch.setattr(apply, 'US_CITIZEN', True)
    assert apply.pick_option('No|N/A', opts).startswith('N/A-I am a US Citizen')
    assert apply.pick_option('No', ['Yes', 'No - I am a U.S. citizen', 'No - I hold a visa']) == 'No - I am a U.S. citizen'
    assert apply.pick_option('No', opts).startswith('N/A-I am a US Citizen')


def test_process_gives_workday_a_page_and_returns_pair(monkeypatch):
    calls = []
    def fake_apply(page, job, prof, bank, submit):
        calls.append(hasattr(page, 'goto'))
        return 'ready', 'ok'
    monkeypatch.setattr(workday, 'apply_job', fake_apply)
    class Pg:
        def goto(self, *a, **k): pass
    class Ctx:
        def new_page(self): return Pg()
    assert apply.process({'source': 'workday'}, {'workday_auto': True}, [], False, Ctx()) == ('ready', 'ok')
    assert calls == [True]
