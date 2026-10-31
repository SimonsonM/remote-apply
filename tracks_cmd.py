"""Create and maintain private career tracks from approved local resume facts."""
import json
import os
import re
import tempfile
from pathlib import Path

import resume_import
import resumes


def _save(path, data):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _approved(conn, root):
    rows = resume_import.approved_facts(conn)
    if not rows:
        raise ValueError('No approved facts. Run ./ra setup or ./ra resume-import PATH first.')
    profile_path = Path(root) / 'profile.json'
    profile = json.loads(profile_path.read_text()) if profile_path.exists() else {}
    identity = {key: value for key, value in rows if key in ('name', 'email', 'phone', 'city', 'state', 'location')}
    name = profile.get('name') or identity.get('name', '')
    contact = ' | '.join(filter(None, (profile.get('email') or identity.get('email'),
                                       profile.get('phone') or identity.get('phone'),
                                       profile.get('location') or identity.get('location') or
                                       ', '.join(filter(None, (profile.get('city') or identity.get('city'), profile.get('state') or identity.get('state')))))))
    jobs, skills, education, certs = {}, {}, [], []
    role_keys = {}
    for key, value in rows:
        if key == 'role':
            parts = [part.strip() for part in value.split(' | ')]
            title, company = (parts + ['', ''])[:2]
            dates = parts[2] if len(parts) > 2 else ''
            job_key = f'job{len(jobs) + 1}'
            jobs[job_key] = [title, company, '', dates, {}]
            role_keys[value] = job_key
        elif key == 'role_bullet':
            for role, job_key in role_keys.items():
                if value.startswith(role + ': '):
                    lines = jobs[job_key][4]
                    lines[f'bullet{len(lines) + 1}'] = value[len(role) + 2:]
                    break
        elif key == 'skill':
            label, sep, detail = value.partition(': ')
            skills[f'skill{len(skills) + 1}'] = [label, detail if sep else value]
        elif key == 'education':
            education.append(value)
        elif key in ('certification', 'cert'):
            certs.append(value)
    return dict(name=name, contact=contact, jobs=jobs, education=education, certs=certs, skills=skills)


def _selection(read, write, label, choices, current=None):
    keys = list(choices)
    for i, key in enumerate(keys, 1):
        write(f'{i}. {choices[key]}')
    default = ','.join(str(keys.index(key) + 1) for key in current or [] if key in keys)
    while True:
        raw = read(f'{label} (numbers in display order, comma-separated) [{default}]: ').strip() or default
        try:
            nums = [int(x.strip()) for x in raw.split(',') if x.strip()]
            if len(nums) == len(set(nums)) and all(1 <= n <= len(keys) for n in nums):
                return [keys[n - 1] for n in nums]
        except ValueError:
            pass
        write('Choose valid, unique numbers from the list.')


def _ask(read, label, default=''):
    answer = read(f'{label} [{default}]: ').strip()
    return default if not answer else answer


def _edit_variant(facts, initial, read, write):
    v = dict(initial)
    old = v.get('keywords', [])
    old_text = ', '.join(old) if isinstance(old, list) else old
    entered = read(f'Keywords (comma-separated words/phrases) [{old_text}]: ').strip()
    v['keywords'] = [part.strip() for part in entered.split(',') if part.strip()] if entered else old
    v['headline'] = _ask(read, 'Headline (your own words)', v.get('headline', ''))
    v['summary'] = _ask(read, 'Summary (your own words)', v.get('summary', ''))
    old_order = dict(v.get('order', []))
    jobs = _selection(read, write, 'Jobs', {k: ' | '.join(map(str, job[:2])) for k, job in facts['jobs'].items()}, [k for k, _ in v.get('order', [])])
    v['order'] = []
    for job in jobs:
        bullets = facts['jobs'][job][4]
        picked = _selection(read, write, f'Bullets for {job}', bullets, old_order.get(job, list(bullets)))
        v['order'].append([job, picked])
    v['skills'] = _selection(read, write, 'Skill lines', {k: ' | '.join(line) for k, line in facts['skills'].items()}, v.get('skills', list(facts['skills'])))
    return v


def _sample(conn, path, write):
    try:
        columns = {row[1] for row in conn.execute('PRAGMA table_info(jobs)')}
        if 'title' not in columns:
            return
        description = 'description' if 'description' in columns else "''"
        status = "WHERE status IN ('new','ready','needs_you','staged','manual')" if 'status' in columns else ''
        rows = conn.execute(f'SELECT title, {description} FROM jobs {status} ORDER BY rowid DESC LIMIT 3').fetchall()
    except Exception:
        return
    if not rows:
        return
    prior = resumes.FACTS
    try:
        resumes.FACTS = path
        write('Sample queue routing:')
        for title, desc in rows:
            write(f'  {title}: {resumes.pick_variant(title, desc)}')
    finally:
        resumes.FACTS = prior


def run(action, conn, root=resumes.ROOT, name=None, source=None, force=False, read=input, write=print):
    root = Path(root)
    path = root / 'resume_facts.json'
    if action == 'init':
        if path.exists() and not force:
            raise ValueError('resume_facts.json already exists; use --force to back it up and replace it.')
        f = _approved(conn, root)
        f['variants'] = {'General': {'headline': '', 'summary': '',
                                     'order': [[k, list(job[4])] for k, job in f['jobs'].items()],
                                     'skills': list(f['skills']), 'keywords': []}}
        f['track_settings'] = {'default': 'General', 'tie_order': ['General'], 'title_priority': ['General']}
        if path.exists():
            backup = root / 'resume_facts.json.bak'
            index = 1
            while backup.exists():
                backup = root / f'resume_facts.json.bak.{index}'
                index += 1
            _save(backup, json.loads(path.read_text()))
            write(f'Backed up existing facts to {backup}')
    else:
        if not path.exists():
            raise ValueError('No resume_facts.json. Run ./ra tracks init first.')
        f = json.loads(path.read_text())
        variants = f['variants']
        if action == 'list':
            default = f.get('track_settings', {}).get('default')
            for track, v in variants.items():
                write(f'{track}{" (default)" if track == default else ""}: {v.get("keywords", [])}')
            return f
        if action == 'add':
            if name in variants:
                raise ValueError(f'Track {name} already exists.')
            if source and source not in variants:
                raise ValueError(f'Unknown source track: {source}')
            base = variants[source].copy() if source else {'order': [[k, list(job[4])] for k, job in f['jobs'].items()], 'skills': list(f['skills'])}
            variants[name] = _edit_variant(f, base, read, write)
        elif action == 'edit':
            if name not in variants:
                raise ValueError(f'Unknown track: {name}')
            variants[name] = _edit_variant(f, variants[name], read, write)
        elif action == 'remove':
            if name not in variants:
                raise ValueError(f'Unknown track: {name}')
            if len(variants) == 1:
                raise ValueError('Cannot remove the last track.')
            del variants[name]
            cfg = f.setdefault('track_settings', {})
            for key in ('tie_order', 'title_priority'):
                cfg[key] = [x for x in cfg.get(key, []) if x != name]
            if cfg.get('default') == name:
                cfg['default'] = next(iter(variants))
        elif action == 'default':
            if name not in variants:
                raise ValueError(f'Unknown track: {name}')
            f.setdefault('track_settings', {})['default'] = name
        else:
            raise ValueError(f'Unknown action: {action}')
    _save(path, f)
    write(f'Saved {path}')
    _sample(conn, path, write)
    return f
