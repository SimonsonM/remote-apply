"""Guided local setup, with no network access."""
import datetime as dt
import getpass
import json
import os
import re
from pathlib import Path

import answer_store
import resume_import
import state

ROOT=Path(__file__).resolve().parent
CATALOG=ROOT/'answer_catalog.json'
IDENTITY=('name','email','phone','location','city','state','zip','linkedin','github','website','timezone')
RESUME_NAME=re.compile(r'resume|cv|curriculum|vita',re.I)
RESUME_HEADING=re.compile(r'(?im)^\s*#*\s*(?:experience|education|skills)\b')
EXIT_MESSAGE='Progress saved. Run ./ra and choose 1 to continue.'


def find_resumes(root=ROOT, home=None):
    home=Path.home() if home is None else Path(home)
    found=[]
    for base in (Path(root),home/'Documents',home/'Desktop'):
        if base.is_dir():
            for p in sorted(base.iterdir()):
                if not p.is_file() or p.name.startswith('.') or p.suffix.lower() not in ('.docx','.pdf','.md','.txt','.rtf'):
                    continue
                named=bool(RESUME_NAME.search(p.stem))
                if p.parent.resolve()==Path(root).resolve() and not named:
                    continue
                heading=False
                if not named:
                    try:
                        if p.suffix.lower() in ('.md','.txt','.rtf'):
                            with p.open(encoding='utf-8',errors='ignore') as stream:
                                sample=stream.read(4096)
                        else:
                            sample=resume_import.read_resume(p)[:4096]
                        heading=bool(RESUME_HEADING.search(sample))
                    except (OSError,ValueError,UnicodeError,ImportError): pass
                if named or heading: found.append((0 if named else 1,p.resolve()))
    return [p for _,p in sorted(set(found),key=lambda row:(row[0],str(row[1]).lower()))[:8]]


def save_private(path, contents):
    path=Path(path)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f: f.write(contents)
    os.chmod(path,0o600)


def save_fact(conn,key,value,source='setup'):
    if not value: return
    row=conn.execute("SELECT id FROM profile_facts WHERE key=? AND source='setup' ORDER BY id DESC LIMIT 1",(key,)).fetchone()
    if row:
        conn.execute("UPDATE profile_facts SET value=?,approved=0,status='proposed' WHERE id=?",(value,row[0]))
        ident=row[0]
    else:
        ident=conn.execute('INSERT INTO profile_facts(key,value,source,approved,status) VALUES (?,?,?,0,\'proposed\')',(key,value,source)).lastrowid
    resume_import.approve_fact(conn,ident)


def save_answer(conn,item,value,approved):
    row=conn.execute('SELECT id FROM answer_store WHERE match_regex=? AND source=\'setup\' ORDER BY id DESC LIMIT 1',(item['match'],)).fetchone()
    category=item['category']
    scope=json.dumps({'explicit_id':True}) if category in answer_store.RESTRICTED and approved else '*'
    now=dt.datetime.now(dt.timezone.utc)
    review=(now+dt.timedelta(days=90 if category=='salary' else 365)).isoformat() if approved else None
    args=(item['prompt'],value,int(bool(re.search(state.SENSITIVE,item['prompt']))),int(approved),now.isoformat() if approved else None,review,scope,now.isoformat(),category,'approved' if approved else 'pending')
    if row:
        conn.execute('''UPDATE answer_store SET question_text=?,answer=?,sensitive=?,approved=?,verified_at=?,review_by=?,scope=?,ts=?,category=?,status=? WHERE id=?''',args+(row[0],))
    else:
        conn.execute('''INSERT INTO answer_store(match_regex,question_text,answer,sensitive,source,approved,verified_at,review_by,scope,ts,category,status)
                        VALUES (?,?,?,?,'setup',?,?,?,?,?,?,?)''',(item['match'],)+args)
    conn.commit()


def _ask(read,prompt,default=''):
    shown=f' [{default}]' if default else ''
    answer=read(f'{prompt}{shown}: ').strip()
    return answer if answer else default


def _valid(read,write,prompt,check,default=''):
    for _ in range(3):
        answer=_ask(read,prompt,default)
        if check(answer): return answer
        write('Invalid answer. Please try again.')
    raise EOFError


def _plain(read,write,prompt,default=''):
    answer=_ask(read,prompt+' Type your answer, or press Enter to skip.',default)
    if answer.lower() in ('n','no','skip'): return ''
    return answer


def _catalog_answer(item,read,write,default):
    kind=item['answer_type']
    if kind=='yes_no':
        raw=_valid(read,write,item['prompt']+' [yes/no/skip]',lambda v:v.lower() in ('y','n','yes','no','skip',''),default)
        return {'y':'yes','n':'no'}.get(raw.lower(),raw.lower()) if raw.lower()!='skip' else ''
    if kind=='choice':
        write('1. Type your answer')
        write('2. Skip')
        raw=_valid(read,write,item['prompt']+' Choose 1 or 2',lambda v:v in ('1','2','') ,'1')
        return _plain(read,write,'Type your answer',default) if raw=='1' else ''
    if kind=='sentinel':
        write('1. Agree')
        write('2. Disagree')
        write('3. Skip')
        raw=_valid(read,write,item['prompt']+' Choose 1, 2, or 3',lambda v:v in ('1','2','3',''), '3')
        return {'1':'yes','2':'no'}.get(raw,'')
    return _plain(read,write,item['prompt'],default)


def _usable_resume(value):
    candidate=Path(value).expanduser()
    if not candidate.is_file() or candidate.suffix.lower() not in ('.docx','.pdf','.txt','.md','.rtf'):
        return False
    try:
        resume_import.read_resume(candidate)
        return True
    except (OSError,ValueError,UnicodeError,ImportError):
        return False


def run(conn,root=ROOT,read=input,write=print,non_interactive=False,answers_file=None):
    try:
        return _run(conn,root,read,write,non_interactive,answers_file)
    except (KeyboardInterrupt,EOFError,StopIteration):
        write(EXIT_MESSAGE)
        return None


def _run(conn,root,read,write,non_interactive,answers_file):
    root=Path(root)
    data=json.loads(Path(answers_file).read_text()) if non_interactive and answers_file else {}
    if non_interactive and not answers_file:
        raise ValueError('--non-interactive requires --answers-file')
    profile_path=root/'profile.json'
    profile=json.loads(profile_path.read_text()) if profile_path.exists() else {}
    paths=find_resumes(root)
    if not non_interactive:
        write('Welcome. Setup takes about 10 minutes. You can quit anytime and continue with ./ra option 1.')
        write('We will review your resume, collect your details and answers, then save your settings.')
        write('Resume files found:')
        for i,p in enumerate(paths,1): write(f'{i}. {p}')
        write('[p] type a path')
        write("[s] skip - I'll type my details by hand")
    if non_interactive:
        chosen=data.get('resume','')
    else:
        def good_resume(value):
            if value.lower() in ('s','skip'): return True
            if value.lower()=='p': return True
            if value.isdigit(): return 1<=int(value)<=len(paths)
            return _usable_resume(value)
        chosen=_valid(read,write,'Resume file path or number',good_resume,profile.get('resume',''))
        if chosen.lower()=='p':
            chosen=_valid(read,write,'Resume file path',_usable_resume)
    if isinstance(chosen,str) and chosen.isdigit() and 1<=int(chosen)<=len(paths): chosen=str(paths[int(chosen)-1])
    resume=None if str(chosen).lower() in ('','s','skip') else Path(chosen).expanduser().resolve()
    if resume and not resume.is_file(): raise ValueError('Resume file not found')
    counts={'approved':0,'proposed':0,'dropped':0}
    if resume:
        _,counts=resume_import.import_resume(conn,resume,read,write,data.get('facts',{}) if non_interactive else None)
    proposed={k:v for k,v in resume_import.approved_facts(conn) if k in IDENTITY}
    for key,value in conn.execute("SELECT key,value FROM profile_facts WHERE source LIKE 'resume:%' ORDER BY id"):
        if key in IDENTITY and key not in proposed: proposed[key]=value
    identity=data.get('profile',{}) if non_interactive else {}
    for key in IDENTITY:
        default=profile.get(key,proposed.get(key,''))
        value=identity.get(key,default) if non_interactive else _plain(read,write,key.replace('_',' ').title(),default)
        if value:
            profile[key]=value
            save_private(profile_path,json.dumps(profile,indent=2)+'\n')
            existing=conn.execute("SELECT value,status FROM profile_facts WHERE key=? AND source='setup' ORDER BY id DESC LIMIT 1",(key,)).fetchone()
            if not existing or existing[0]!=value or existing[1]!='approved':
                save_fact(conn,key,value)
    if resume:
        profile['resume']=str(resume)
        save_private(profile_path,json.dumps(profile,indent=2)+'\n')
    tz_names=('any','eastern','central','mountain','arizona','pacific','alaska','hawaii')
    tz_alias={'est':'eastern','edt':'eastern','et':'eastern','cst':'central','cdt':'central','ct':'central',
              'mst':'mountain','mdt':'mountain','mt':'mountain','pst':'pacific','pdt':'pacific','pt':'pacific',
              'akst':'alaska','akdt':'alaska','hst':'hawaii'}
    if non_interactive:
        tz=str(data.get('timezone',profile.get('timezone','any'))).lower()
    else:
        write('Remote jobs: which timezone can you work? (any, eastern, central, mountain, arizona, pacific, alaska, hawaii; EST/CST/MST/PST also work)')
        tz=_valid(read,write,'Timezone',lambda v: v.strip().lower() in tz_names or v.strip().lower() in tz_alias,profile.get('timezone','any')).strip().lower()
    tz=tz_alias.get(tz,tz) if tz in tz_names or tz in tz_alias else 'any'
    profile['timezone']=tz
    save_private(profile_path,json.dumps(profile,indent=2)+'\n')
    facts=dict(resume_import.approved_facts(conn))
    catalog=json.loads(CATALOG.read_text())['questions']
    supplied=data.get('answers',{}) if non_interactive else {}
    for item in catalog:
        key=item['key']; cat=item['category']
        old=conn.execute("SELECT answer,status FROM answer_store WHERE match_regex=? AND source='setup' ORDER BY id DESC LIMIT 1",(item['match'],)).fetchone()
        if non_interactive and old and key not in supplied:
            continue
        default=old[0] if old else ''
        if not default and cat not in answer_store.RESTRICTED:
            default=facts.get(item.get('derive_from',''),profile.get(item.get('derive_from',''),''))
        if non_interactive:
            value=supplied.get(key,default)
            explicit=key in supplied
            if isinstance(value,dict):
                approved=value.get('approve')=='yes'; value=value.get('value','')
            else:
                approved=explicit and cat not in answer_store.RESTRICTED and bool(value)
            if str(value).lower()=='skip':
                value=''; approved=False
        else:
            value=_catalog_answer(item,read,write,default)
            if old and value==old[0] and old[1]=='approved':
                continue
            approved=bool(value)
            if cat in answer_store.RESTRICTED and value:
                confirmation=_valid(read,write,f'You are telling employers: {value}. Type yes to confirm, or press Enter to skip.',lambda v:v.lower() in ('yes',''), '')
                approved=confirmation.lower()=='yes'
        save_answer(conn,item,str(value or ''),approved)
    key=data.get('anthropic_api_key','') if non_interactive else getpass.getpass('Anthropic API key (optional, Enter to skip): ').strip()
    if key:
        if '\n' in key or '\r' in key: raise ValueError('Invalid API key')
        env_path=root/'.env'
        kept=[line for line in env_path.read_text().splitlines() if not line.startswith('ANTHROPIC_API_KEY=')] if env_path.exists() else []
        save_private(env_path,'\n'.join(kept+['ANTHROPIC_API_KEY='+key])+'\n')
    defaults={'mode':'prepare','daily_cap':8,'nightly_minutes':60}
    options=data.get('settings',{}) if non_interactive else {}
    for key,default in defaults.items():
        if non_interactive:
            value=options.get(key,profile.get(key,default))
        elif key=='mode':
            write('1. prepare (recommended: fills forms in the background and never waits)')
            write('2. stage (opens browser tabs and waits for you)')
            current=profile.get(key,default)
            raw=_valid(read,write,'Mode: choose 1 or 2',lambda v:v in ('1','2'), '1' if current=='prepare' else '2')
            value={'1':'prepare','2':'stage'}[raw]
        else:
            value=_valid(read,write,key.replace('_',' ').title(),lambda v:str(v).isdigit() and int(v)>0,str(profile.get(key,default)))
        if key!='mode': value=int(value)
        elif value not in ('prepare','stage','manual'): raise ValueError('Invalid mode')
        profile[key]=value
        save_private(profile_path,json.dumps(profile,indent=2)+'\n')
    approved=conn.execute("SELECT count(*) FROM answer_store WHERE source='setup' AND status='approved'").fetchone()[0]
    pending=[row[0] for row in conn.execute("SELECT question_text FROM answer_store WHERE source='setup' AND status='pending'")]
    write(f'Saved: profile.json, settings, and {counts["approved"]} resume facts.')
    write(f'Approved answers: {approved}.')
    write(f'Still pending: {len(pending)} answers.')
    write('Pending questions: '+('; '.join(pending) if pending else 'none'))
    write('Next command: ./ra (choose option 2 to finish pending answers)')
    return profile
