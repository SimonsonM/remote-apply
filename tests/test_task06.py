"""First real-form inspection regressions."""
import sqlite3

import pytest

import answer_store
import apply
import doctor
import state
from test_browser_fixture import browser, case, fixture_site


@pytest.mark.parametrize('wording', [
    'Are you legally authorised to work full-time in the country where this job is based?',
    'Do you now, or will you ever, require employment sponsorship to work in the US?',
    'Are you legally authorized to work for any employer in the US?',
    'Do you have an employment agreement or non-compete with your current company?',
    'Do you have a restrictive covenant or non-solicit agreement?',
    'Can you work in Canada?', 'Can you work in the UK?', 'Will you relocate?',
    'Do you have security clearance?', 'Are you 18+?', 'What is your age?',
    'Are you subject to sanctions?', 'Are you a government official?',
    'Do you have a conflict of interest?', 'Have you been convicted of a crime?',
    'Can you pass a background check?', 'Were you previously employed at Toast?',
    'Have you previously worked at Datadog?', 'Are you related to a current employee?',
    'Is a family member employed here?', 'Do you have the right to work in this country?',
])
def test_attestation_wording(wording):
    assert answer_store.category(wording) == 'attestation'


def test_reclassify_is_idempotent_and_preserves_other_approvals(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(apply, 'DB', tmp_path / 'answers.sqlite3')
    with apply.db() as conn:
        for question, cat, approved, status in [
            ('Are you legally authorized to work?', 'other', 1, 'approved'),
            ('Full name', 'other', 1, 'approved'),
            ('Do you require sponsorship?', 'other', 0, 'pending'),
        ]:
            conn.execute('INSERT INTO answer_store(question_text,match_regex,category,approved,status,ts) VALUES (?,?,?,?,?,?)',
                         (question, question, cat, approved, status, state.now()))
        conn.commit()
    apply.answers_cmd('reclassify')
    assert '1' in capsys.readouterr().out
    with apply.db() as conn:
        assert conn.execute('SELECT category,approved,status FROM answer_store WHERE id=1').fetchone() == ('attestation', 0, 'pending')
        assert conn.execute('SELECT category,approved,status FROM answer_store WHERE id=2').fetchone() == ('profile_fact', 1, 'approved')
        assert conn.execute('SELECT category,approved,status FROM answer_store WHERE id=3').fetchone() == ('attestation', 0, 'pending')
        assert answer_store.reclassify(conn) == []


def test_clean_field_label():
    assert apply.clean_field_label('Gender\nSelect ...\nMale\nFemale') == 'Gender'
    assert apply.clean_field_label('\n  Work mode *  \n Remote\nOffice') == 'Work mode *'
    assert len(apply.clean_field_label('X' * 250)) == 200


def file_field(i, label='Attach', name='', required=True, **extra):
    return dict(i=i, type='file', tag='input', label=label, name=name, required=required, **extra)


@pytest.mark.parametrize('fields,expected,ambiguous', [
    ([file_field(0, name='resume')], {0}, False),
    ([file_field(0, id='candidate_cv')], {0}, False),
    ([file_field(0, aria_label='Resume upload')], {0}, False),
    ([file_field(0, heading='Resume')], {0}, False),
    ([file_field(0), file_field(1, 'Cover letter', name='cover_letter')], {0}, False),
    ([file_field(0), file_field(1)], set(), True),
    ([file_field(0, 'Cover letter', name='cover_letter')], set(), False),
])
def test_resume_file_branches(fields, expected, ambiguous):
    assert apply.resume_file_indices(fields) == (expected, ambiguous)
    actions, missing = apply.generic_plan(fields, {'resume': '/tmp/fixture.pdf'}, [], 'Fixture', resolver=lambda *args: '')
    assert {key for key, kind, _ in actions if kind == 'file'} == expected
    assert bool(any(isinstance(item, tuple) and item[1] == 'ambiguous_file' for item in missing)) == ambiguous


def test_ambiguous_file_records_blocker(tmp_path, monkeypatch):
    monkeypatch.setattr(apply, 'DB', tmp_path / 'attempt.sqlite3')
    with apply.db() as conn:
        conn.execute("INSERT INTO jobs(url,company,title) VALUES ('fixture','Fixture','Tester')")
        ident = state.create_attempt(conn, 'fixture')
        fields = [file_field(0), file_field(1)]
        _, missing = apply.generic_plan(fields, {'resume': '/tmp/fixture.pdf'}, [], 'Fixture', resolver=lambda *args: '')
        apply._record_attempt(conn, ident, {'url': 'fixture', 'company': 'Fixture'}, [], missing, fields)
        assert conn.execute("SELECT count(*) FROM blockers WHERE attempt_id=? AND kind='ambiguous_file'", (ident,)).fetchone()[0] == 2


@pytest.mark.parametrize('questions,expected_files,ambiguous', [
    ([('Attach', 'attachment')], {'attachment'}, False),
    ([('Attach', 'upload_a'), ('Attach', 'upload_b')], set(), True),
    ([('Cover letter', 'cover_letter'), ('Attach', 'upload')], {'upload'}, False),
    ([('Resume', 'document'), ('Attach', 'other')], {'document'}, False),
])
def test_greenhouse_file_branches(questions, expected_files, ambiguous):
    data = {'questions': [{'label': label, 'required': True,
                           'fields': [{'name': name, 'type': 'input_file'}]}
                          for label, name in questions]}
    actions, missing = apply.gh_plan('', '', {'resume': '/tmp/fixture.pdf'}, [], 'Fixture',
                                     resolver=lambda *args: '', questions=data)
    assert {name for name, kind, _ in actions if kind == 'file'} == expected_files
    assert bool(any(isinstance(item, tuple) and item[1] == 'ambiguous_file' for item in missing)) == ambiguous


def test_page_kind_heuristic():
    assert doctor.page_kind([file_field(0), {'type': 'email', 'name': 'email'}, {'type': 'text', 'name': 'full_name'}]) == 'application_form'
    assert doctor.page_kind([{'type': 'search', 'name': 'search'}, {'type': 'select', 'name': 'filter'}]) == 'listing_or_other'


@pytest.mark.browser
def test_chromium_clean_labels_and_matching(browser):
    page = browser.new_page()
    try:
        page.set_content('''<form>
          <label for="gender">Gender *</label><select id="gender" name="gender" required>
            <option>Select ...</option><option>Male</option><option>Female</option></select>
          <fieldset><legend>Work mode *</legend>
            <label><input type="radio" name="mode" value="Remote" required>Remote</label>
            <label><input type="radio" name="mode" value="Office" required>Office</label></fieldset>
        </form>''')
        fields = page.evaluate(apply.FIELDS_JS)
        assert fields[0]['label'] == 'Gender *'
        assert fields[0]['options'] == ['Select ...', 'Male', 'Female']
        assert [f['question'] for f in fields[1:]] == ['Work mode *', 'Work mode *']
        assert [f['label'] for f in fields[1:]] == ['Remote', 'Office']
        seen = []
        def resolver(label, *_):
            seen.append(label)
            return {'Gender *': 'Female', 'Work mode *': 'Remote'}.get(label, '')
        actions, missing = apply.generic_plan(fields, {}, [], 'Fixture', resolver=resolver)
        assert not missing
        assert (0, 'select', 'Female') in actions
        assert (1, 'click', 'Remote') in actions
        assert seen == ['Gender *', 'Work mode *']
    finally:
        page.close()


@pytest.mark.browser
def test_doctor_listing_fixture(case, browser, tmp_path):
    job, _, _, _ = case
    url = job['url'].replace('/apply', '/listing')
    _, reports = doctor.run([url], browser=browser, output_dir=tmp_path)
    assert reports[0]['host_note'] == 'custom_career_site'
    assert reports[0]['listing_page'] is True
    assert reports[0]['page_kind'] == 'listing_or_other'
