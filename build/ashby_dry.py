"""Dry-run the generic (Ashby/Lever) fill path on queued jobs. Never submits. Usage: ashby_dry.py ashby|lever [N]"""
import json, sqlite3, sys
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
import apply
src = sys.argv[1] if len(sys.argv) > 1 else 'ashby'
n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
prof = apply.prepare_profile(json.load(open('profile.json')))
c = sqlite3.connect('applications.sqlite3', timeout=30)
cols = [d[0] for d in c.execute("select * from jobs limit 0").description]
rows = [dict(zip(cols, r)) for r in c.execute(
    "select * from jobs where source=? and status in ('captcha','needs_you') order by fit desc limit ?", (src, n))]
c.close()
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(user_agent=apply.UA["User-Agent"], viewport={"width": 1280, "height": 1600})
    for job in rows:
        page = ctx.new_page()
        try:
            url = job['apply_url'] or job['url']
            if src == 'ashby' and not url.rstrip('/').endswith('/application'):
                url = url.rstrip('/') + '/application'
            page.goto(url, wait_until='domcontentloaded', timeout=45000)
            page.wait_for_selector('input, textarea, select', timeout=25000)
            results, missing, _ = apply.prepare_fields(page, job, prof, [], page.evaluate(apply.FIELDS_JS))
            bad = [r for r in results if r.get('status') == 'failed']
            print('RESULT', job['company'], '|', job['title'][:60], '| filled', len(results) - len(bad),
                  '| failed', [(r['key'], r['error'][:60]) for r in bad], '| missing', missing, flush=True)
            page.screenshot(path=f"/tmp/dry_{src}_{job['company'][:12].replace(' ', '_')}.png", full_page=True)
        except Exception as e:
            print('ERROR', job['company'], str(e).splitlines()[0][:200], flush=True)
        page.close()
    ctx.close(); b.close()
