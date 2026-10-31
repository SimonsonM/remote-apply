#!/usr/bin/env python3
"""Remote Apply launcher and scheduler."""
import os
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent
PYTHON=ROOT/'.venv/bin/python'
if (ROOT/'.playwright-browsers').exists():
    os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(ROOT/'.playwright-browsers')
MENU={
 '1':('First-time setup',['setup']),
 '2':('Review my answers',['answers','wizard']),
 '3':('Find jobs and prepare applications',['run']),
 '4':('Review and submit (you press Submit)',['review']),
 '5':('What needs me?',['page','status']),
 '6':('Check one job form',['doctor']),
 '7':('Employer replies and follow-ups',['followups']),
 '8':('Health check',['health']),
 '0':('Quit',[]),
}
MARKER='# remote-apply nightly'


def menu_action(choice,has_profile=True):
    """Pure choice mapping; None means invalid input."""
    if not has_profile: return ['setup']
    return MENU.get(str(choice),(None,None))[1]


def cron_line(root=ROOT):
    return f'0 4 * * * {root / "nightly.sh"} {MARKER}'


def schedule(action,read=input,write=print):
    try:
        current=subprocess.run(['crontab','-l'],capture_output=True,text=True)
    except FileNotFoundError:
        write('crontab is unavailable; install cron to use scheduling.')
        return 1
    lines=[] if current.returncode else current.stdout.splitlines()
    wanted=cron_line()
    ours=[line for line in lines if line.endswith(MARKER)]
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
        for number,(label,_) in MENU.items(): write(f'{number}. {label}')
        try:
            choice=read('Choose a number: ').strip()
        except (EOFError,KeyboardInterrupt):
            return
        action=menu_action(choice,True)
        if action is None:
            write('Choose a number from 0 to 8.')
            continue
        if choice=='0': return
        if choice=='8':
            subprocess.run([str(PYTHON),str(ROOT/'install.py'),'--check'],cwd=ROOT)
        elif choice=='6':
            try:
                url=read('Job URL (Enter to check database jobs): ').strip()
            except (EOFError,KeyboardInterrupt):
                return
            subprocess.run([str(PYTHON),str(ROOT/'apply.py'),'doctor']+([url] if url else []),cwd=ROOT)
        else: run_commands(action)


def main(argv=None):
    args=list(sys.argv[1:] if argv is None else argv)
    if not args:
        menu(); return 0
    if args[0]=='schedule':
        if len(args)!=2 or args[1] not in ('on','off','status'):
            print('Usage: ./ra schedule on|off|status',file=sys.stderr); return 2
        return schedule(args[1])
    if args[0]=='nightly':
        args=['run']
    if args[0]=='health':
        return subprocess.run([str(PYTHON),str(ROOT/'install.py'),'--check'],cwd=ROOT).returncode
    return subprocess.run([str(PYTHON),str(ROOT/'apply.py'),*args],cwd=ROOT).returncode

if __name__=='__main__': raise SystemExit(main())
