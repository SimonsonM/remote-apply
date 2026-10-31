"""Find which ATS a company uses by crawling its careers pages (two hops). Usage: ats_detect.py 'Name=domain' ..."""
import re, sys, urllib.request, urllib.parse
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
ATS = re.compile(r"https?://[a-z0-9.-]*(myworkdayjobs\.com|myworkdaysite\.com|icims\.com|ultipro\.com|ukg\.net|successfactors\.(com|eu)|"
                 r"taleo\.net|greenhouse\.io|lever\.co|ashbyhq\.com|smartrecruiters\.com|jobvite\.com|paylocity\.com|"
                 r"adp\.com|oraclecloud\.com|dayforcehcm\.com|applytojob\.com|workable\.com|bamboohr\.com|phenompeople\.com)[^\s\"'<>]*", re.I)


def get(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
            return r.geturl(), r.read(600000).decode("utf-8", "replace")
    except Exception:
        return url, ""


for arg in sys.argv[1:]:
    name, domain = arg.split("=", 1)
    hits, seen = {}, set()
    queue = [f"https://www.{domain}/careers", f"https://{domain}/careers", f"https://careers.{domain}/", f"https://jobs.{domain}/", f"https://www.{domain}/"]
    for depth in range(2):
        nxt = []
        for u in queue:
            if u in seen:
                continue
            seen.add(u)
            final, html = get(u)
            for m in ATS.finditer(final + " " + html):
                hits.setdefault(m.group(1).lower(), m.group(0)[:140])
            if depth == 0:
                for href in re.findall(r'href="([^"]+)"', html):
                    if re.search(r"career|job|join|employment|work-with|opportunit", href, re.I):
                        nxt.append(urllib.parse.urljoin(final, href))
        queue = nxt[:12]
        if hits:
            break
    print(f"{name}: " + (" | ".join(hits.values()) if hits else "none found"), flush=True)
