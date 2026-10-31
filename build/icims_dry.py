"""Fill up to N iCIMS applications for Company; never submit."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright
import apply
import icims


def main():
    if len(sys.argv) < 2:
        raise SystemExit('Usage: build/icims_dry.py Company [N]')
    company = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    if limit < 1:
        raise SystemExit('N must be positive')
    processes = subprocess.check_output(['ps', '-eo', 'args'], text=True)
    if any('apply.py run' in line and 'icims_dry.py' not in line for line in processes.splitlines()):
        raise SystemExit('apply.py run is active; wait for it to finish')
    boards = json.loads(apply.BOARDS.read_text())
    cfg = boards['icims'][company]
    prof = apply.prepare_profile(json.loads(apply.CONFIG.read_text()))
    with apply.db() as conn:
        rows = conn.execute("SELECT url,company,title,job_id,apply_url,source,description,location FROM jobs "
                            "WHERE source='icims' AND company=? AND status IN ('new','needs_you') ORDER BY fit DESC LIMIT ?",
                            (company, limit)).fetchall()
    columns = ('url', 'company', 'title', 'job_id', 'apply_url', 'source', 'description', 'location')
    jobs = [dict(zip(columns, row)) for row in rows]
    if len(jobs) < limit:
        known = {j['url'] for j in jobs}
        for job in icims.fetch_icims(company, cfg, max_pages=1):
            if job['url'] not in known:
                jobs.append(job)
            if len(jobs) >= limit:
                break
    if not jobs:
        print('RESULT no iCIMS jobs found for', company, flush=True)
        return
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=apply.UA['User-Agent'], viewport={'width': 1280, 'height': 1600})
        try:
            for job in jobs:
                status, note = icims.apply_job(context.new_page(), job, prof, [], dry=True)
                print('RESULT', company, '|', job['title'], '|', status, '|', note, flush=True)
        finally:
            context.close()
            browser.close()


if __name__ == '__main__':
    main()
