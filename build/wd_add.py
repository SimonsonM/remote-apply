"""Probe Workday tenants for named employers and add hits to boards.json. Usage: wd_add.py 'Company' ['Company=tenant'] ..."""
import json, sys
sys.path.insert(0, '.')
import apply, workday
boards = json.loads(apply.BOARDS.read_text())
wd = boards.setdefault('workday', {})
added = []
with apply.db() as conn:
    for arg in sys.argv[1:]:
        name, _, alias = arg.partition('=')
        if name in wd:
            print('have', name); continue
        cfg = None
        for probe in filter(None, [alias, name]):
            try:
                cfg = workday.discover_tenant(probe, conn)
            except Exception as e:
                print('err', probe, str(e)[:80])
            if cfg:
                break
        print(('FOUND ' if cfg else 'miss  ') + name, cfg or '', flush=True)
        if cfg:
            wd[name] = cfg; added.append(name)
apply.BOARDS.write_text(json.dumps(boards, indent=2))
print('added', len(added), added)
