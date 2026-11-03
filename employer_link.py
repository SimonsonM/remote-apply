"""Find an employer-owned ATS posting for manual Adzuna leads."""
import datetime as dt
import json
import re
import time
from difflib import SequenceMatcher
from urllib.parse import quote
from urllib.error import HTTPError

import apply
import state

STAFFING = ('robert half', 'insight global', 'teksystems', 'kforce', 'randstad',
            'khayainfotech', 'galaxy i technologies', 'request technology',
            'apex systems', 'motion recruitment', 'experis', 'tata consultancy services')
ATS = ('greenhouse', 'lever', 'ashby', 'smartrecruiters', 'workable', 'workday')
SUFFIX = re.compile(r'\b(incorporated|inc|llc|ltd|corp|corporation|company|co|group)\b', re.I)


def slugs(company, boards):
    base = re.sub(r'[^a-z0-9]+', ' ', SUFFIX.sub('', company.lower())).strip()
    words = base.split()
    values = []
    for name in ATS[:-1]:
        for board_company, slug in (boards.get(name) or {}).items():
            if normalized_company(board_company) == normalized_company(company):
                values.append((name, str(slug)))
    for slug in ('-'.join(words), ''.join(words)):
        if slug:
            values.extend((name, slug) for name in ATS[:-1])
    return list(dict.fromkeys(values))


def title_similarity(a, b):
    def norm(s):
        return ' '.join(sorted(re.findall(r'[a-z0-9]+', state.normalize_title(s))))
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def normalized_company(name):
    return apply.ckey(SUFFIX.sub('', name or ''))


def location_compatible(lead, posting):
    a, b = lead.lower(), posting.lower()
    if 'remote' in a:
        return 'remote' in b and not re.search(r'\b(uk|canada|europe|india)\b', b)
    city = re.sub(r'^(onsite|hybrid)\s*-\s*', '', a).split('/')[-1].split(',')[0].strip()
    return bool(city and city not in ('us', 'united states') and re.search(r'\b' + re.escape(city) + r'\b', b))


def _listings(ats, slug):
    slug = quote(slug, safe='-')
    if ats == 'greenhouse':
        data = apply.get_json(f'https://boards-api.greenhouse.io/v1/boards/{slug}/jobs')
        return [dict(title=j.get('title',''), location=(j.get('location') or {}).get('name',''),
                     url=j.get('absolute_url',''), token=slug, job_id=str(j.get('id') or '')) for j in data.get('jobs', [])]
    if ats == 'lever':
        data = apply.get_json(f'https://api.lever.co/v0/postings/{slug}?mode=json')
        return [dict(title=j.get('text',''), location=' / '.join((j.get('categories') or {}).get('allLocations') or [(j.get('categories') or {}).get('location','')]),
                     url=j.get('applyUrl') or j.get('hostedUrl',''), token=slug, job_id=str(j.get('id') or '')) for j in data]
    if ats == 'ashby':
        data = apply.get_json(f'https://api.ashbyhq.com/posting-api/job-board/{slug}')
        return [dict(title=j.get('title',''), location=('Remote - ' if j.get('isRemote') else '') + (j.get('location') or ''),
                     url=j.get('applyUrl') or j.get('jobUrl',''), token=slug, job_id=str(j.get('id') or ''))
                for j in data.get('jobs',[]) if j.get('isListed',True)]
    if ats == 'smartrecruiters':
        result = []
        base = f'https://api.smartrecruiters.com/v1/companies/{slug}/postings'
        for offset in range(0, 500, 100):
            if offset:
                time.sleep(1/3)
            data = apply.get_json(f'{base}?limit=100&offset={offset}')
            items = data.get('content', [])
            result.extend(dict(title=j.get('name',''), location=('Remote - ' if (j.get('location') or {}).get('remote') else '') +
                               ', '.join(str((j.get('location') or {}).get(k,'')) for k in ('city','region','country')),
                               url=f"https://jobs.smartrecruiters.com/{slug}/{j['id']}", token=slug,
                               job_id=str(j['id'])) for j in items if j.get('id'))
            if len(items) < 100:
                break
        return result
    if ats == 'workable':
        data = apply.get_json(f'https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true')
        result = []
        for j in data.get('jobs', []):
            if not j.get('shortcode'):
                continue
            place = j.get('location') or {}
            loc = ', '.join(str(place.get(k, '')) for k in ('city', 'region', 'country')) if isinstance(place, dict) else str(place)
            result.append(dict(title=j.get('title', ''), location=('Remote - ' if j.get('remote') or j.get('telecommuting') else '') + loc,
                               url=f"https://apply.workable.com/{slug}/j/{j['shortcode']}/apply", token=slug,
                               job_id=str(j['shortcode'])))
        return result
    return []


class Finder:
    def __init__(self, conn, boards, profile, dry=False, budget=300):
        self.conn, self.boards, self.profile, self.dry = conn, boards, profile, dry
        self.deadline = time.monotonic() + budget
        self.last_call = 0
        self.memory = {}
        self.failed_keys = set()
        self.last_unavailable = False
        self.has_cache = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='employer_link_cache'").fetchone())
        if not dry:
            conn.execute('CREATE TABLE IF NOT EXISTS employer_link_cache (ats TEXT NOT NULL, slug TEXT NOT NULL, checked_at TEXT NOT NULL, listings TEXT NOT NULL, PRIMARY KEY(ats,slug))')
            self.has_cache = True

    def listings(self, ats, slug, loader=None):
        key = ats, slug
        if key in self.memory:
            return self.memory[key]
        row = self.conn.execute('SELECT checked_at,listings FROM employer_link_cache WHERE ats=? AND slug=?', key).fetchone() if self.has_cache else None
        if row and (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(row[0])).days < 7:
            result = json.loads(row[1]); self.memory[key] = result; return result
        if time.monotonic() >= self.deadline:
            return []
        elapsed = time.monotonic() - self.last_call
        if elapsed < 1/3:
            time.sleep(1/3 - elapsed)
        self.last_call = time.monotonic()
        cacheable = True
        try:
            result = loader() if loader else _listings(ats, slug)
        except HTTPError as exc:
            result = []
            cacheable = exc.code in (400, 404)
        except Exception:
            result = []
            cacheable = False
        if not cacheable:
            self.failed_keys.add(key)
        self.memory[key] = result
        if not self.dry and cacheable:
            self.conn.execute('INSERT INTO employer_link_cache VALUES (?,?,?,?) ON CONFLICT(ats,slug) DO UPDATE SET checked_at=excluded.checked_at,listings=excluded.listings',
                              (ats,slug,dt.datetime.now(dt.timezone.utc).isoformat(),json.dumps(result)))
        return result

    def find(self, job):
        self.last_unavailable = False
        company = job['company'] or ''
        blocked = self.boards.get('staffing_agencies', self.profile.get('staffing_agencies', STAFFING))
        if any(normalized_company(name) == normalized_company(company) for name in blocked):
            return None
        choices = []
        for ats, slug in slugs(company, self.boards):
            if time.monotonic() >= self.deadline:
                break
            for item in self.listings(ats, slug):
                similarity = title_similarity(job['title'], item['title'])
                if similarity >= .85 and item['url'] and location_compatible(job['location'], item['location']):
                    choices.append((similarity, ats, item))
            if (ats, slug) in self.failed_keys:
                self.last_unavailable = True
            if choices and choices[-1][0] == 1.0:
                return choices[-1]
        wd = next((row[1:] for row in self.conn.execute('SELECT company,host,tenant,site FROM workday_tenants')
                   if normalized_company(row[0]) == normalized_company(company)), None)
        if wd and all(wd) and time.monotonic() < self.deadline:
            try:
                def workday_listings():
                    return [dict(title=j['title'], location=j['location'], url=j.get('apply_url') or j['url'],
                                 token=j.get('token') or wd[1], job_id=j.get('job_id') or '')
                            for j in apply.fetch_workday(company, dict(host=wd[0],tenant=wd[1],site=wd[2]), max_pages=2)]
                for item in self.listings('workday', wd[1], workday_listings):
                    if time.monotonic() >= self.deadline:
                        break
                    similarity = title_similarity(job['title'], item['title'])
                    if similarity >= .85 and location_compatible(job['location'], item['location']):
                        choices.append((similarity, 'workday', item))
                if ('workday', wd[1]) in self.failed_keys:
                    self.last_unavailable = True
            except Exception:
                self.last_unavailable = True
        return max(choices, key=lambda choice: choice[0], default=None)

    def run(self, limit=None):
        rows = self.conn.execute("SELECT url,company,title,location,fit FROM jobs WHERE source='adzuna' AND status='manual' AND (apply_url LIKE 'https://www.adzuna.com/details/%' OR apply_url LIKE 'https://www.adzuna.com/land/ad/%') ORDER BY fit DESC").fetchall()
        found = missed = duplicate = skipped = unavailable = 0
        for url, company, title, location, fit in rows:
            if limit is not None and found + missed + duplicate + skipped + unavailable >= limit or time.monotonic() >= self.deadline:
                break
            if fit < apply.fit_floor(location, self.profile):
                skipped += 1; continue
            match = self.find(dict(company=company,title=title,location=location))
            if not match:
                if self.last_unavailable:
                    unavailable += 1
                else:
                    missed += 1
                continue
            _, ats, item = match
            target = item['url']
            if apply.existing_ats_url(self.conn, target, exclude=url):
                duplicate += 1
                if not self.dry:
                    self.conn.execute("UPDATE jobs SET status='duplicate', notes='Employer ATS posting already in jobs' WHERE url=?", (url,))
                continue
            found += 1
            if not self.dry:
                self.conn.execute("UPDATE jobs SET apply_url=?, source=?, token=?, job_id=?, status='new', notes=?, updated=CURRENT_TIMESTAMP WHERE url=?",
                                  (target, ats, item.get('token') or '', item.get('job_id') or '',
                                   f'employer link found via {ats}', url))
        return dict(found=found, missed=missed, unavailable=unavailable, duplicate=duplicate,
                    skipped=skipped, processed=found+missed+unavailable+duplicate+skipped,
                    budget_exhausted=time.monotonic()>=self.deadline)
