"""Probe listboxes on a Workday My Information page (no submit)."""
import json, sqlite3, sys
sys.path.insert(0, '.')
from playwright.sync_api import sync_playwright
import apply, workday
prof = apply.prepare_profile(json.load(open('profile.json')))
c = sqlite3.connect('applications.sqlite3')
r = c.execute("select url,company,title,job_id,apply_url,source from jobs where source='workday' and company=? order by fit desc limit 1", (sys.argv[1],)).fetchone()
job = dict(zip(('url', 'company', 'title', 'job_id', 'apply_url', 'source'), r))
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_context(user_agent=apply.UA["User-Agent"], viewport={"width": 1280, "height": 1600}).new_page()
    page.goto(job['url'], wait_until='domcontentloaded'); page.wait_for_timeout(4000)
    workday._button(page, r'^Apply$'); page.wait_for_timeout(1500)
    with apply.db() as conn:
        print('auth', workday._auth(page, job, prof, apply.load_keys(), conn, [], []))
    workday._settle(page)
    nxt='[data-automation-id="pageFooterNextButton"], [data-automation-id="bottom-navigation-next-button"]'
    for _ in range(4):
        if 'Application Questions' in workday._current_step(page): break
        workday._click(page, nxt); page.wait_for_timeout(6000); workday._settle(page)
    print('step', workday._current_step(page).replace(chr(10),' '))
    print(page.evaluate('''() => [...document.querySelectorAll('button, [role=combobox], [aria-haspopup]')].filter(e=>/Select One/.test(e.innerText||e.value||'')).slice(0,4).map(e=>e.outerHTML.slice(0,350))'''))
    b.close()
