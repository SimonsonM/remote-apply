import json
import os
import re
import sqlite3
import stat
from pathlib import Path

import pytest

import answer_wizard
import install
import launcher
import resume_import
import setup_assistant
import smart
import state


def memory_db():
    conn=sqlite3.connect(':memory:')
    state.migrate(conn)
    return conn


def test_install_plan_and_launcher_menu(capsys):
    plan=install.installation_plan(Path('/tmp/example'))
    assert 'Create /tmp/example/.venv if missing' in plan
    assert not any('desktop' in step.lower() for step in plan)
    assert any('remote-apply.desktop' in step for step in install.installation_plan(Path('/tmp/example'),True))
    assert launcher.menu_action('1')==['setup']
    assert launcher.menu_action('5')==['page','status']
    assert launcher.menu_action('7')==['followups']
    assert launcher.menu_action('8')==['health']
    assert launcher.menu_action('0')==[]
    assert launcher.menu_action('6',False)==['setup']
    assert launcher.menu_action('x') is None
    assert install.main(['--dry-run'])==0
    assert 'Back up and migrate applications.sqlite3' in capsys.readouterr().out


def test_catalog_schema():
    catalog=json.loads(setup_assistant.CATALOG.read_text())['questions']
    assert 35 <= len(catalog) <= 50
    assert len({q['key'] for q in catalog})==len(catalog)
    for q in catalog:
        assert {'key','prompt','category','match','answer_type','help'} <= q.keys()
        assert q['answer_type'] in {'yes_no','text','choice','sentinel'}
        re.compile(q['match'])


@pytest.mark.parametrize('suffix,text,expected',[
    ('.md','# Ada Lovelace\nada@example.test\nEngineer, Example Labs, Jan 2020 - Present','Engineer | Example Labs | Jan 2020 - Present'),
    ('.txt','Grace Hopper\nExample Labs — Engineering Lead (2018—2022)','Engineering Lead | Example Labs | 2018 - 2022'),
    ('.md','Lin Chen\nExperience\nDeveloper, Widgets Inc, Mar 2019 – Jun 2021','Developer | Widgets Inc | Mar 2019 - Jun 2021'),
    ('.txt','Sam Rivera\nExperience\nExample Co - Analyst (2020-2023)','Analyst | Example Co | 2020 - 2023'),
    ('.md','Taylor Smith\nExperience\nManager, Two Columns, Feb 2021 - Current\n- Built tools','Manager | Two Columns | Feb 2021 - Current'),
    ('.txt','Pat Jones\nSkills\nSCADA experience\nEducation\nState University','SCADA experience'),
])
def test_resume_parser_cases(tmp_path,suffix,text,expected):
    path=tmp_path/('fixture'+suffix); path.write_text(text)
    facts=resume_import.parse_text(resume_import.read_resume(path))
    assert expected in [f['value'] for f in facts]
    if 'SCADA' in text:
        assert any(f['key']=='skill' and f['confidence']=='low' for f in facts)


def test_docx_parsing(tmp_path):
    from docx import Document
    path=tmp_path/'fixture.docx'
    d=Document(); d.add_paragraph('Dana Scully'); d.add_paragraph('dana@example.test'); d.add_paragraph('Scientist, Bureau, Jan 2010 - Present'); d.save(path)
    facts=resume_import.parse_text(resume_import.read_resume(path))
    assert {'name','email','role'} <= {f['key'] for f in facts}


def test_heading_grouped_bullet_is_separate_proposed_fact():
    facts=resume_import.parse_text('A Person\nExperience\nEngineer, Acme, Jan 2020 - Present\n- Built controls\n')
    assert any(f['key']=='role_bullet' and 'Built controls' in f['value'] for f in facts)


def test_pdf_parsing(tmp_path):
    # A one-page PDF built with stdlib so no PDF producer is needed in tests.
    lines=[b'BT /F1 12 Tf 72 720 Td (Robin Example) Tj 0 -20 Td (robin@example.test) Tj ET']
    stream=b'\n'.join(lines)
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
             b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
             b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
             b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
    out=bytearray(b'%PDF-1.4\n'); offsets=[0]
    for i,obj in enumerate(objects,1):
        offsets.append(len(out)); out.extend(f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n')
    start=len(out); out.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for off in offsets[1:]: out.extend(f'{off:010d} 00000 n \n'.encode())
    out.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode())
    path=tmp_path/'fixture.pdf'; path.write_bytes(out)
    facts=resume_import.parse_text(resume_import.read_resume(path))
    assert any(f['key']=='email' and f['value']=='robin@example.test' for f in facts)


def test_setup_scripted_privacy_permissions_and_grounding(tmp_path,capsys):
    resume=tmp_path/'resume.md'
    resume.write_text('# Alex Example\nalex@example.test\n(555) 123-4567\nEngineer, Good Company, Jan 2020 - Present\nEngineer, Bad Company, Jan 2018 - Dec 2019\n')
    data={'resume':str(resume),'facts':{'role:4':'y','role:5':'n'},'profile':{'name':'Alex Example','email':'alex@example.test','phone':'(555) 123-4567'},'answers':{'work_authorized':{'value':'Yes','approve':'yes'},'salary_expectation':{'value':'100000','approve':'skip'},'city':'Madison'},'anthropic_api_key':'test-secret','settings':{'mode':'prepare','daily_cap':5,'nightly_minutes':30}}
    inputs=tmp_path/'answers-fixture.json'; inputs.write_text(json.dumps(data))
    conn=memory_db()
    setup_assistant.run(conn,tmp_path,non_interactive=True,answers_file=inputs)
    profile=json.loads((tmp_path/'profile.json').read_text())
    assert profile['mode']=='prepare' and profile['daily_cap']==5
    assert stat.S_IMODE((tmp_path/'profile.json').stat().st_mode)==0o600
    assert stat.S_IMODE((tmp_path/'.env').stat().st_mode)==0o600
    assert 'test-secret' not in capsys.readouterr().out
    assert conn.execute("SELECT status FROM answer_store WHERE match_regex LIKE '%salary expectation%'").fetchone()[0]=='pending'
    assert conn.execute("SELECT status FROM answer_store WHERE match_regex LIKE '%authori%' ORDER BY id DESC LIMIT 1").fetchone()[0]=='approved'
    text='\n'.join(v for k,v in resume_import.approved_facts(conn))
    assert 'Good Company' in text and 'Bad Company' not in text
    assert smart.check_grounding('I worked at Good Company.',text)
    assert not smart.check_grounding('I worked at Bad Company.',text)
    assert not (tmp_path/'run.log').exists()


def test_reimport_marks_changed_role_stale(tmp_path):
    path=tmp_path/'resume.txt'
    path.write_text('A Person\nEngineer, Old Company, Jan 2020 - Present\n')
    conn=memory_db()
    resume_import.import_resume(conn,path,decisions={'role':'y'})
    path.write_text('A Person\nEngineer, New Company, Jan 2020 - Present\n')
    resume_import.import_resume(conn,path,decisions={'role':'n'})
    assert conn.execute("SELECT status FROM profile_facts WHERE value LIKE '%Old Company%'").fetchone()[0]=='stale'
    assert conn.execute("SELECT status FROM profile_facts WHERE value LIKE '%New Company%'").fetchone()[0]=='dropped'


def test_grounding_rejects_dropped_single_word_employer(tmp_path):
    path=tmp_path/'resume.md'; path.write_text('A Person\nEngineer, Acme, Jan 2020 - Present\nEngineer, Beta, Jan 2018 - Dec 2019\n')
    db=tmp_path/'facts.sqlite3'
    with sqlite3.connect(db) as conn:
        state.migrate(conn)
        resume_import.import_resume(conn,path,decisions={'role:2':'y','role:3':'n'})
    profile={'resume':str(path),'_db_path':str(db)}
    assert smart.check_grounding('I worked at Acme.', '', profile=profile)
    assert not smart.check_grounding('I worked at Beta.', '', profile=profile)


def test_setup_interactive_scripted_input(tmp_path,monkeypatch):
    path=tmp_path/'resume.txt'; path.write_text('Jamie Person\njamie@example.test\n555-321-9876\n')
    answers={'Name':'Jamie Person','Email':'jamie@example.test','Phone':'555-321-9876'}
    def read(prompt):
        if prompt.startswith('Resume file path'): return str(path)
        if prompt.startswith('Confirm [y]'): return 'n'
        for label,value in answers.items():
            if prompt.startswith(label+' '): return value
        return ''
    monkeypatch.setattr(setup_assistant.getpass,'getpass',lambda _: '')
    with memory_db() as conn:
        setup_assistant.run(conn,tmp_path,read=read,write=lambda _: None)
    profile=json.loads((tmp_path/'profile.json').read_text())
    assert profile['email']=='jamie@example.test'
    assert profile['mode']=='prepare'
    assert conn.execute('SELECT count(*) FROM answer_store').fetchone()[0]==len(json.loads(setup_assistant.CATALOG.read_text())['questions'])


def test_wizard_cannot_approve_blank_catalog_answer(tmp_path):
    conn=memory_db()
    item=next(q for q in json.loads(setup_assistant.CATALOG.read_text())['questions'] if q['key']=='salary_expectation')
    setup_assistant.save_answer(conn,item,'',False)
    answers=iter(['y','s'])
    output=[]
    answer_wizard.run(conn,category='salary',read=lambda _: next(answers),write=output.append,interactive=True)
    assert any('Enter an answer' in line for line in output)
    assert conn.execute('SELECT status FROM answer_store').fetchone()[0]=='pending'
