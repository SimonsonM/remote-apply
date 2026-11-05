#!/usr/bin/env python3
"""Remote Apply launcher: `./ra` opens a menu; `./ra <command>` runs one thing. install.py copies this file to ./ra."""
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT=Path(__file__).resolve().parent
PYTHON=ROOT/'.venv/bin/python'
if (ROOT/'.playwright-browsers').exists():
    os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(ROOT/'.playwright-browsers')
PORT=8675
MENU={
 '1':('First-time setup',['setup']),
 '2':('Open the dashboard',['dashboard']),
 '3':('Run everything now (find jobs, apply, draft outreach)',['nightly']),
 '4':('What needs me?',['page','status']),
 '5':('Review my answers',['answers','wizard']),
 '6':('Check one job form',['doctor']),
 '7':('Employer replies and follow-ups',['followups']),
 '8':('Nightly schedule (on / off / status)',['schedule']),
 '9':('Update Remote Apply',['update']),
 'h':('Health check',['health']),
 '0':('Quit',[]),
}
MARKER='# remote-apply nightly'


def menu_action(choice,has_profile=True):
    """Pure choice mapping; None means invalid input."""
    if not has_profile: return ['setup']
    return MENU.get(str(choice).strip().lower(),(None,None))[1]


def cron_line(root=ROOT):
    return f'0 3 * * * {root / "nightly.sh"} {MARKER}'


def schedule(action,read=input,write=print):
    try:
        current=subprocess.run(['crontab','-l'],capture_output=True,text=True)
    except FileNotFoundError:
        write('crontab is unavailable; install cron to use scheduling.')
        return 1
    lines=[] if current.returncode else current.stdout.splitlines()
    wanted=cron_line()
    ours=[line for line in lines if line.endswith(MARKER) or 'nightly.sh' in line]
    if action=='status':
        write(ours[0] if ours else 'Nightly schedule is off')
        return 0
    write(f'Cron line: {wanted}')
    if read(f'Type yes to turn scheduling {action}: ').strip().lower()!='yes':
        write('No change made')
        return 1
    updated=[line for line in lines if not line.endswith(MARKER)]
    if action=='on': updated.append(wanted)
    subprocess.run(['crontab','-'],input='\n'.join(updated)+'\n',text=True,check=True)
    write(f'Nightly schedule {action}')
    return 0


def port_open(port=PORT):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(('127.0.0.1',port))==0


def dashboard(open_browser=True,write=print):
    """Start the dashboard in the background if it is not already running, then open it."""
    url=f'http://127.0.0.1:{PORT}'
    if not port_open():
        log=open(ROOT/'dashboard.log','a')
        subprocess.Popen([str(PYTHON),str(ROOT/'dashboard.py'),'--port',str(PORT)],cwd=ROOT,stdout=log,stderr=log,start_new_session=True)
        for _ in range(40):
            if port_open(): break
            time.sleep(0.25)
    write(f'Dashboard: {url}')
    if open_browser and (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        webbrowser.open(url)   # no desktop session (ssh, cron): just print the address
    return 0


def nightly():
    # Close postings the boards have removed before applying; a failed sweep never blocks the run.
    subprocess.run([str(PYTHON),str(ROOT/'stale.py'),'--apply'],cwd=ROOT)
    rc=subprocess.run([str(PYTHON),str(ROOT/'apply.py'),'run'],cwd=ROOT).returncode
    # Outreach drafts for strong fresh applications and quiet ones; drafts only, nothing is sent.
    subprocess.run([str(PYTHON),str(ROOT/'outreach.py')],cwd=ROOT)
    return rc


def update(write=print):
    """Pull the latest code, install any new packages, migrate the database. Your data files are untouched."""
    if (ROOT/'.git').exists():
        r=subprocess.run(['git','pull','--ff-only'],cwd=ROOT)
        if r.returncode:
            write('git pull failed (local changes?). Nothing else was changed.')
            return r.returncode
    return subprocess.run([sys.executable,str(ROOT/'install.py')],cwd=ROOT).returncode


def run_commands(commands):
    for command in commands:
        subprocess.run([str(PYTHON),str(ROOT/'apply.py'),command],cwd=ROOT,check=False)


def menu(read=input,write=print):
    first=True
    while True:
        has_profile=(ROOT/'profile.json').exists()
        if first and not has_profile:
            write('No profile found. First-time setup will start now.')
            run_commands(['setup'])
            first=False
            has_profile=(ROOT/'profile.json').exists()
            if not has_profile:
                write('Setup did not create a profile. Run first-time setup from the menu when ready.')
            else:
                write('Setup complete. Choose what to do next.')
            continue
        first=False
        write('')
        for number,(label,_) in MENU.items(): write(f'{number}. {label}')
        try:
            choice=read('Choose: ').strip().lower()
        except (EOFError,KeyboardInterrupt):
            return
        action=menu_action(choice,True)
        if action is None:
            write('Choose one of the listed numbers (h for health check).')
            continue
        if choice=='0': return
        try:
            if action==['health']:
                subprocess.run([str(PYTHON),str(ROOT/'install.py'),'--check'],cwd=ROOT)
            elif action==['dashboard']:
                dashboard(write=write)
            elif action==['nightly']:
                nightly()
            elif action==['update']:
                update(write=write)
            elif action==['schedule']:
                what=read('on, off or status? ').strip().lower()
                if what in ('on','off','status'):
                    schedule(what,read=read,write=write)
            elif action==['doctor']:
                url=read('Job URL (Enter to check database jobs): ').strip()
                subprocess.run([str(PYTHON),str(ROOT/'apply.py'),'doctor']+([url] if url else []),cwd=ROOT)
            else:
                run_commands(action)
        except (EOFError,KeyboardInterrupt):
            return


def main(argv=None):
    args=list(sys.argv[1:] if argv is None else argv)
    if not args:
        menu(); return 0
    if args[0]=='schedule':
        if len(args)!=2 or args[1] not in ('on','off','status'):
            print('Usage: ./ra schedule on|off|status',file=sys.stderr); return 2
        return schedule(args[1])
    if args[0]=='stale':
        return subprocess.run([str(PYTHON),str(ROOT/'stale.py'),*args[1:]],cwd=ROOT).returncode
    if args[0]=='nightly':
        return nightly()
    if args[0]=='update':
        return update()
    if args[0]=='health':
        return subprocess.run([str(PYTHON),str(ROOT/'install.py'),'--check'],cwd=ROOT).returncode
    if args[0]=='dashboard':
        if len(args)>1 and args[1]!='--open':   # e.g. --port N: run in the foreground as before
            return subprocess.run([str(PYTHON),str(ROOT/'dashboard.py'),*args[1:]],cwd=ROOT).returncode
        return dashboard(open_browser=True)
    for name,script in (('outreach','outreach.py'),('threats','threat_scan.py'),('mail','mail_sort.py')):
        if args[0]==name:
            return subprocess.run([str(PYTHON),str(ROOT/script),*args[1:]],cwd=ROOT).returncode
    return subprocess.run([str(PYTHON),str(ROOT/'apply.py'),*args],cwd=ROOT).returncode

if __name__=='__main__': raise SystemExit(main())
