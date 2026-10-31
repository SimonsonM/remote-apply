#!/usr/bin/env python3
"""Install and inspect a local Remote Apply checkout using only the standard library."""
import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parent
FOLDERS=('backups','reports','proof','cover_letters','followups')
DESKTOP=Path.home()/'.local/share/applications/remote-apply.desktop'
MARKER='# remote-apply nightly'


def installation_plan(root=ROOT,desktop=False):
    steps=[
        'Check Python 3.10 or newer',
        f'Create {root / ".venv"} if missing',
        f'Install pinned packages from {root / "requirements.txt"}',
        'Install Playwright Chromium if missing',
        'Create private backups, reports, proof, cover_letters, and followups folders',
        'Back up and migrate applications.sqlite3',
        f'Make {root / "ra"} executable',
    ]
    if desktop: steps.append(f'Write {DESKTOP}')
    return steps


def run(args,**kwargs):
    subprocess.run(args,check=True,**kwargs)


def chromium_path(python,root=ROOT):
    env=os.environ.copy()
    local=root/'.playwright-browsers'
    if local.exists(): env['PLAYWRIGHT_BROWSERS_PATH']=str(local)
    result=subprocess.run([str(python),'-c','from playwright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()'],capture_output=True,text=True,env=env)
    return Path(result.stdout.strip()) if result.returncode==0 and result.stdout.strip() else None


def check(root=ROOT):
    python=root/'.venv/bin/python'
    ok=True
    print(f'Python: {sys.version_info.major}.{sys.version_info.minor} (>=3.10: {sys.version_info >= (3,10)})')
    ok &= sys.version_info >= (3,10)
    if python.exists():
        path=chromium_path(python,root)
        if path and path.exists():
            env=os.environ.copy()
            if (root/'.playwright-browsers').exists(): env['PLAYWRIGHT_BROWSERS_PATH']=str(root/'.playwright-browsers')
            result=subprocess.run([str(python),'-c','from playwright.sync_api import sync_playwright\nwith sync_playwright() as p:\n b=p.chromium.launch(headless=True)\n b.close()'],capture_output=True,text=True,timeout=30,env=env)
            print(f'Chromium launch: {"ok" if result.returncode==0 else "failed"}')
            ok &= result.returncode==0
        else:
            print('Chromium launch: unavailable')
            ok=False
    else:
        print('Virtual environment: missing')
        ok=False
    free=shutil.disk_usage(root).free
    print(f'Disk free: {free // (1024*1024)} MiB')
    ok &= free >= 500*1024*1024
    db=root/'applications.sqlite3'
    writable=os.access(db if db.exists() else root,os.W_OK)
    print(f'Database writable: {writable}')
    ok &= writable
    try:
        with urllib.request.urlopen('http://127.0.0.1:11434/api/tags',timeout=2):
            print('Ollama reachable: yes')
    except Exception:
        print('Ollama reachable: no (optional)')
    env=root/'.env'
    key=bool(os.environ.get('ANTHROPIC_API_KEY')) or (env.exists() and any(line.startswith('ANTHROPIC_API_KEY=') and len(line)>18 for line in env.read_text().splitlines()))
    print(f'Anthropic API key present: {bool(key)}')
    try:
        cron=subprocess.run(['crontab','-l'],capture_output=True,text=True)
        present=cron.returncode==0 and any(line.endswith(MARKER) for line in cron.stdout.splitlines())
    except FileNotFoundError:
        present=False
    print(f'Nightly cron entry present: {present}')
    return 0 if ok else 1


def remove_cron():
    try: current=subprocess.run(['crontab','-l'],capture_output=True,text=True)
    except FileNotFoundError: return
    if current.returncode: return
    old=current.stdout.splitlines()
    new=[line for line in old if not line.endswith(MARKER)]
    if len(new)!=len(old): run(['crontab','-'],input='\n'.join(new)+'\n',text=True)


def uninstall(root=ROOT):
    (root/'ra').unlink(missing_ok=True)
    if DESKTOP.exists():
        content=DESKTOP.read_text()
        if f'Exec={root / "ra"}' in content: DESKTOP.unlink()
    remove_cron()
    print('Launcher, owned desktop entry, and nightly cron entry removed. Data preserved.')


def install(root=ROOT,desktop=False):
    if sys.version_info < (3,10): raise SystemExit('Python 3.10 or newer is required')
    venv=root/'.venv'
    if not venv.exists(): run([sys.executable,'-m','venv',str(venv)])
    python=venv/'bin/python'
    run([str(python),'-m','pip','install','--no-cache-dir','-r',str(root/'requirements.txt')])
    browser=chromium_path(python,root)
    if not browser or not browser.exists():
        env=os.environ.copy()
        env['PLAYWRIGHT_BROWSERS_PATH']=str(root/'.playwright-browsers')
        run([str(python),'-m','playwright','install','chromium'],env=env)
    for name in FOLDERS:
        folder=root/name
        folder.mkdir(exist_ok=True)
        folder.chmod(0o700)
    sys.path.insert(0,str(root))
    import state
    db=root/'applications.sqlite3'
    if not db.exists():
        fd=os.open(db,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.close(fd)
    with sqlite3.connect(db) as conn: state.migrate(conn)
    shutil.copyfile(root/'launcher.py',root/'ra')
    (root/'ra').chmod(0o755)
    if desktop:
        DESKTOP.parent.mkdir(parents=True,exist_ok=True)
        DESKTOP.write_text('[Desktop Entry]\nType=Application\nName=Remote Apply\n'+f'Exec={root / "ra"}\nPath={root}\nTerminal=true\n')
    print('Installed. Next: ./ra')


def main(argv=None):
    parser=argparse.ArgumentParser()
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--dry-run',action='store_true')
    group.add_argument('--check',action='store_true')
    group.add_argument('--uninstall',action='store_true')
    parser.add_argument('--desktop-entry',action='store_true')
    args=parser.parse_args(argv)
    if args.dry_run:
        print('\n'.join(installation_plan(desktop=args.desktop_entry)))
        return 0
    if args.check: return check()
    if args.uninstall: uninstall(); return 0
    install(desktop=args.desktop_entry)
    return 0

if __name__=='__main__': raise SystemExit(main())
