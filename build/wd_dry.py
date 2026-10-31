"""Dry-run Workday apply (never submits) for given companies."""
import json, sqlite3, sys
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
import apply, workday
prof = apply.prepare_profile(json.load(open('profile.json')))
c = sqlite3.connect('applications.sqlite3')
rows = []
for company in sys.argv[1:]:
    r = c.execute("select url,company,title,job_id,apply_url,source,description,location from jobs where source='workday' and status in ('new','needs_you') "
                  "and company=? order by fit desc limit 2", (company,)).fetchall()
    for x in r:
        rows.append(dict(zip(('url', 'company', 'title', 'job_id', 'apply_url', 'source', 'description', 'location'), x)))
c.close()
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(user_agent=apply.UA["User-Agent"], viewport={"width": 1280, "height": 1600})
    for job in rows:
        status, note = workday.apply_job(ctx.new_page(), job, prof, [], False)
        print('RESULT', job['company'], '|', job['title'][:70], '|', status, '|', note[:600], flush=True)
    ctx.close(); b.close()
