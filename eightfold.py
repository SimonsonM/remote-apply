"""Public Eightfold career search; application sign-in is handled by the owner."""
import datetime as dt
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin

import apply

TRACK_QUERIES = ('program manager', 'project manager', 'technical program manager',
                 'AI operations', 'customer success', 'partner manager',
                 'marketing', 'security', 'IT operations')
DEFAULT_LOCATIONS = ('United States', 'Chicago, Illinois', 'Madison, Wisconsin', 'Wisconsin')
class Client:
    def __init__(self, base):
        self.base = base
        self.playwright = self.browser = self.page = None

    def get(self, url):
        if self.page is None:
            try:
                return apply.get_json(url)
            except HTTPError as exc:
                if exc.code not in (403, 429):
                    raise
            from playwright.sync_api import sync_playwright
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(headless=True)
            self.page = self.browser.new_page()
            self.page.goto(self.base + '/careers', wait_until='domcontentloaded', timeout=45000)
        response = self.page.request.get(url, timeout=30000)
        if not response.ok:
            raise RuntimeError(f'Eightfold API HTTP {response.status}')
        return response.json()

    def close(self):
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()


def eligible_location(item, detail=None):
    locations = item.get('locations') or []
    text = ' / '.join(locations).lower()
    site = str((detail or {}).get('efcustomTextWorkSite') or '').lower()
    remote = 'remote' in site or 'remote' in str(item.get('workLocationOption') or '').lower()
    us = any(x in text for x in ('united states', 'usa', 'remote - us'))
    local = any(x in text for x in ('chicago', 'madison', 'milwaukee', 'wisconsin'))
    return (remote and us) or local


def fetch_eightfold(company, cfg, source='eightfold'):
    base = cfg['base_url'].rstrip('/')
    domain = cfg['domain']
    seen = set()
    client = Client(base)
    try:
        for query in cfg.get('queries', TRACK_QUERIES):
            for location in cfg.get('locations', DEFAULT_LOCATIONS):
                for page in range(max(1, int(cfg.get('max_pages', 2)))):
                    params = urlencode(dict(domain=domain, query=query, location=location, start=page * 10))
                    data = client.get(f'{base}/api/pcsx/search?{params}')
                    items = (data.get('data') or {}).get('positions') or []
                    for item in items:
                        ident = str(item.get('id') or '')
                        if not ident or ident in seen:
                            continue
                        seen.add(ident)
                        if not any(x in ' / '.join(item.get('locations') or []).lower()
                                   for x in ('united states', 'usa', 'chicago', 'madison', 'milwaukee', 'wisconsin')):
                            continue
                        detail = (client.get(f'{base}/api/pcsx/position_details?' +
                                   urlencode(dict(position_id=ident,domain=domain,hl='en'))).get('data') or {})
                        if not eligible_location(item, detail):
                            continue
                        desc = apply.clean(detail.get('jobDescription') or '')
                        url = urljoin(base, item.get('positionUrl') or f'/careers/job/{ident}')
                        location_text = ' / '.join(item.get('locations') or [])
                        if 'remote' in str(detail.get('efcustomTextWorkSite') or '').lower():
                            location_text = 'Remote - ' + location_text
                        posted = item.get('postedTs')
                        yield dict(company=company, title=item.get('name') or '', location=location_text,
                                   description=desc, url=url, apply_url=f'{base}/careers/apply?pid={ident}',
                                   source=source, token=domain, job_id=ident,
                                   salary=apply.salary_from(desc),
                                   posted_at=dt.datetime.fromtimestamp(posted,dt.timezone.utc).date().isoformat() if posted else '')
                    if len(items) < 10:
                        break
    finally:
        client.close()
