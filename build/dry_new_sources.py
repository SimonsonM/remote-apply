"""Dry run (never submits) of Workable/SmartRecruiters jobs against a COPY of the DB. Usage: RA_DB=/tmp/copy.sqlite3 python build/dry_new_sources.py [N]"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
assert os.environ.get('RA_DB', '').startswith('/tmp/'), 'run against a copy: RA_DB=/tmp/...'
import apply
from playwright.sync_api import sync_playwright

n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
boards = json.loads(apply.BOARDS.read_text())
prof = apply.prepare_profile(json.loads(apply.CONFIG.read_text()))
prof = dict(prof, workable_auto=True, smartrecruiters_auto=True)  # in memory only; profile.json untouched
sub = {k: boards.get(k, {}) for k in ('workable', 'smartrecruiters')}
print('boards:', {k: len(v) for k, v in sub.items()}, flush=True)
jobs = [j for j in apply.fetch_jobs({**sub, 'usajobs': {'enabled': False}}) if j.get('source') in ('workable', 'smartrecruiters')]
fit = [j for j in jobs if apply.score(j, prof, set()) is not None]
print(f'{len(jobs)} postings, {len(fit)} pass filters', flush=True)
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    try:
        for j in fit[:n]:
            try:
                st, note = apply.process(j, prof, [], False, b.new_context())
            except Exception as e:
                st, note = 'error', str(e)[:150]
            print(f"{j['source']:15} {j['company'][:22]:22} {j['title'][:40]:40} -> {st}: {(note or '')[:160]}", flush=True)
    finally:
        b.close()
print('DONE', flush=True)
