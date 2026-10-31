"""Verify explicit Workday configs (host tenant site) and add working ones to boards.json."""
import json, sys
sys.path.insert(0, '.')
import apply
C = {
    "ABC Supply": ("abcsupply.wd1", "abcsupply", "ABCSupplyCareers"),
    "Woodward": ("woodward.wd5", "woodward", "woodward"),
    "Travelers": ("travelers.wd5", "travelers", "External"),
    "Allstate": ("allstate.wd5", "allstate", "allstate_careers"),
    "Kohl's": ("kohls.wd504", "kohls", "kohlscareers"),
    "Cigna": ("cigna.wd5", "cigna", "cignacareers"),
    "Nationwide": ("nationwide.wd1", "nationwide", "Nationwide_Career"),
    "Elevance Health": ("elevancehealth.wd1", "elevancehealth", "ANT"),
    "Centene": ("centene.wd5", "centene", "centene_external"),
    "Sentry Insurance": ("sentryinsurance.wd1", "sentryinsurance", "SentryCareers"),
    "Rockwell Automation": ("rockwellautomation.wd1", "rockwellautomation", "External_Rockwell_Automation"),
    "Humana": ("humana.wd5", "humana", "Humana_External_Career_Site"),
    "CVS Health": ("cvshealth.wd1", "cvshealth", "CVS_Health_Careers"),
    "Fiserv": ("fiserv.wd5", "fiserv", "EXT"),
}
boards = json.loads(apply.BOARDS.read_text())
wd = boards.setdefault("workday", {})
for name, (host, tenant, site) in C.items():
    cfg = dict(host=host, tenant=tenant, site=site)
    try:
        n = sum(1 for _ in apply.fetch_workday(name, cfg, max_pages=1))
    except Exception as e:
        n = 0; print("fail", name, str(e)[:90])
    print(("OK   " if n else "MISS ") + name, n, flush=True)
    if n:
        wd[name] = cfg
apply.BOARDS.write_text(json.dumps(boards, indent=2))
print("workday tenants:", len(wd))
