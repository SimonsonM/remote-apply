"""Discover supported public ATS boards and merge relevant ones into boards.json.

Candidates come from: company names already in the DB (all sources), a seed list, and free remote-job
feeds (Himalayas, Remotive, RemoteOK) whose apply links point at an ATS. A board is kept only if it has
at least one posting that passes the bot's own title/location filter.
Usage: board_discover.py [--dry] [--max N]"""
import concurrent.futures as cf, json, re, sqlite3, sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import apply  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (remote-apply board discovery; personal job search)"}
SEED = """Airtable Asana Atlassian Automattic Benchling Brex Calendly Canva Carta Chainalysis Checkr Cloudflare Coinbase
Coursera Datadog Deel Discord DoorDash Duolingo Figma Flexport Gitlab Grammarly Gusto HashiCorp HubSpot Instacart Intercom
Klaviyo Lattice LaunchDarkly Linear Lyft Mapbox Marqeta Mercury Miro Monzo Netlify Notion Nubank Okta OpenAI Anthropic
PagerDuty Pinterest Plaid Postman Ramp Reddit Retool Rippling Robinhood Samsara Scale AI Sentry ShipBob Shopify Snyk SoFi
Sourcegraph Squarespace Stripe Supabase Tailscale Toast Twilio Udemy Vercel Webflow Wealthfront Zapier Zendesk Zillow
Affirm Airbnb Amplitude Anduril Applied Intuition Articulate Axon Betterment Bitwarden BlockFi Braze Buffer Chime Circle
Clari Clever Cockroach Labs Coda Collective Health Confluent Contentful Cribl Crusoe Databricks dbt Labs Doximity Drata
Dropbox Elastic Faire Fivetran Fleetio Gemini Glean GoodRx Gong Grafana Labs Hims Honeycomb Hopper Huntress Imply
Instabase Iterable Jasper Kandji Komodo Health Kraken Lemonade Lob Loom Lucid Modern Health Mux Navan New Relic Nuro
Omada Health OneSignal Oscar Health Outreach Patreon Persona Podium Pulumi Quizlet Rapid7 Recurly Relativity Remote
Replit Rocket Lab Runway Salesloft SeatGeek SentinelOne Smartsheet Snowflake Sprout Social Starburst Sumo Logic Swile
Synthesia Tanium Temporal Thumbtack Tide Truebill Turo UiPath Upstart Vanta Verkada Vimeo Wiz Workato Wrike Yext Zoox
Abnormal Security Arctic Wolf Armis Axonius BigID Chainguard Cohere Coalition Cyera Dragos Expel Hugging Face Island
Material Security Mistral Netskope Obsidian Security Orca Security Perplexity Pinecone Red Canary Semgrep Socure Stytch
Teleport Together AI Torq Weights & Biases Writer Abridge Ambience Hebbia Harvey Sierra Decagon Cresta Glean Moveworks
Labelbox Snorkel AI Weaviate LangChain Modal Baseten Fireworks AI Anyscale Replicate Cerebras SambaNova
Epic Games Roblox Unity Niantic Valve Twitch Spotify SoundCloud Peloton Strava AllTrails Calm Headspace Noom Ro Cerebral
Thermo Fisher Abbott Epic Systems Exact Sciences Promega Kohler Rockwell Automation Northwestern Mutual American Family
Fiserv ManpowerGroup Generac Harley-Davidson Johnson Controls Brady Snap-on Sub-Zero Lands End Duluth Trading""".split("\n")

ATS_RE = {
    "greenhouse": re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)|greenhouse\.io/.*[?&]for=([a-z0-9_-]+)", re.I),
    "lever": re.compile(r"jobs\.lever\.co/([a-z0-9_.-]+)", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_.%-]+)", re.I),
    "workable": re.compile(r"apply\.workable\.com/(?!j(?:/|$)|api(?:/|$))([a-z0-9-]+)(?:/|$)", re.I),
    "smartrecruiters": re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/(?!oneclick-ui(?:/|$))([a-z0-9_-]+)(?:/|$)", re.I),
}


def get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def feed_items():
    """Yield (company, apply_url) from free remote feeds. Failures are logged and skipped."""
    try:
        for off in range(0, 2000, 100):
            d = get(f"https://himalayas.app/jobs/api?limit=100&offset={off}")
            jobs = d.get("jobs", [])
            if not jobs:
                break
            for j in jobs:
                yield j.get("companyName", ""), j.get("applicationLink", "") or j.get("guid", "")
            time.sleep(0.5)
    except Exception as e:
        print("himalayas:", str(e)[:100])
    try:
        for j in get("https://remotive.com/api/remote-jobs").get("jobs", []):
            yield j.get("company_name", ""), j.get("url", "")
    except Exception as e:
        print("remotive:", str(e)[:100])
    try:
        for j in get("https://remoteok.com/api")[1:]:
            yield j.get("company", ""), j.get("apply_url", "") or j.get("url", "")
    except Exception as e:
        print("remoteok:", str(e)[:100])


def slugs(name):
    n = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|technology|labs|group|holdings|the)\b\.?", "", name.lower())
    n = re.sub(r"[^a-z0-9 &-]", "", n).replace("&", "and").strip()
    out = [re.sub(r"[^a-z0-9]", "", n), re.sub(r"\s+", "-", n.strip()), (n.split() or [""])[0]]
    full = re.sub(r"[^a-z0-9]", "", name.lower())
    return [s for s in dict.fromkeys(out + [full]) if len(s) >= 3]


def probe(ats, token):
    try:
        if ats == "greenhouse":
            jobs = get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs").get("jobs", [])
            return [(j.get("title", ""), (j.get("location") or {}).get("name", "")) for j in jobs]
        if ats == "lever":
            jobs = get(f"https://api.lever.co/v0/postings/{token}?mode=json")
            return [(j.get("text", ""), ((j.get("workplaceType") or "") + " " + (j.get("categories", {}).get("location") or ""))) for j in jobs]
        if ats == "ashby":
            jobs = get(f"https://api.ashbyhq.com/posting-api/job-board/{token}").get("jobs", [])
            return [(j.get("title", ""), " ".join(filter(None, [j.get("location", ""), "Remote" if j.get("isRemote") else ""]))) for j in jobs]
        if ats == 'workable':
            jobs = apply.post_json(f'https://apply.workable.com/api/v3/accounts/{token}/jobs',
                                   {'query': '', 'location': [], 'department': [], 'worktype': [], 'remote': []}).get('results', [])
            try:
                full = get(f'https://apply.workable.com/api/v1/widget/accounts/{token}?details=true').get('jobs', [])
                jobs = list({j.get('shortcode'): j for j in full + jobs if j.get('shortcode')}.values())
            except Exception:
                pass
            return [(j.get('title', ''), ('Remote - ' if j.get('remote') or j.get('telecommuting') or j.get('workplace') == 'remote' else '') +
                     ' / '.join(filter(None, ((j.get('location') or j).get('city'), (j.get('location') or j).get('region') or j.get('state'),
                                              (j.get('location') or j).get('country'))))) for j in jobs]
        if ats == 'smartrecruiters':
            jobs = get(f'https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100').get('content', [])
            return [(j.get('name', ''), ('Remote - ' if (j.get('location') or {}).get('remote') else '') +
                     ((j.get('location') or {}).get('fullLocation') or ' / '.join(filter(None, (
                         (j.get('location') or {}).get('city'), (j.get('location') or {}).get('region'),
                         (j.get('location') or {}).get('country')))))) for j in jobs]
    except Exception:
        return None


def relevant(postings, profile):
    hits = 0
    for title, loc in postings:
        if apply.score(dict(title=title, location=loc, description=""), profile, False) is not None:
            hits += 1
    return hits


def main():
    dry = "--dry" in sys.argv
    cap = int(sys.argv[sys.argv.index("--max") + 1]) if "--max" in sys.argv else 4000
    boards = json.loads(apply.BOARDS.read_text())
    profile = json.loads(apply.CONFIG.read_text())
    have = {ats: {t.lower() for t in boards.get(ats, {}).values()} for ats in ATS_RE}

    direct, names = [], set()
    for co, url in feed_items():
        for ats, rx in ATS_RE.items():
            m = rx.search(url or "")
            if m:
                tok = next(g for g in m.groups() if g).lower()
                direct.append((ats, tok, co or tok))
                break
        else:
            if co:
                names.add(co.strip())
    print(f"feeds: {len(direct)} direct ATS links, {len(names)} other companies")

    with sqlite3.connect(ROOT / "applications.sqlite3", timeout=60) as conn:
        db_names = [r[0].strip() for r in conn.execute(
            "SELECT company FROM jobs WHERE company != '' GROUP BY company ORDER BY max(fit) DESC")]
    seed = [w for line in SEED for w in line.split()]
    tasks, seen = [], set()
    def add(t):
        if t[1] not in have[t[0]] and t[:2] not in seen:
            seen.add(t[:2]); tasks.append(t)
    for t in direct:
        add(t)
    for co in seed + sorted(names) + db_names:
        for s in slugs(co)[:3]:
            for ats in ATS_RE:
                add((ats, s, co))
    tasks = tasks[:cap]
    print(f"probing {len(tasks)} candidate boards")

    found = {}
    def work(t):
        ats, tok, co = t
        posts = probe(ats, tok)
        return t, posts
    with cf.ThreadPoolExecutor(12) as ex:
        for (ats, tok, co), posts in ex.map(work, tasks):
            if posts:
                n = relevant(posts, profile)
                key = (ats, tok)
                if n and key not in found:
                    found[key] = (co, n, len(posts))
    print(f"live boards with relevant roles: {len(found)}")
    for (ats, tok), (co, n, tot) in sorted(found.items(), key=lambda x: -x[1][1])[:60]:
        print(f"  {ats:10} {tok:28} {co[:30]:30} relevant {n}/{tot}")
    if dry or not found:
        return
    for (ats, tok), (co, n, tot) in found.items():
        name = co if co and co.lower() not in {k.lower() for k in boards.setdefault(ats, {})} else tok
        boards[ats][name] = tok
    apply.BOARDS.write_text(json.dumps(boards, indent=2))
    print('boards.json: ' + ', '.join(f'{ats} {len(boards.get(ats, {}))}' for ats in ATS_RE))


if __name__ == "__main__":
    main()
