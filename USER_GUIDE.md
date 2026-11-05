# Remote Apply: user guide

Remote Apply finds job postings, ranks them against your resume, fills and (if you allow it) submits applications, and tracks every one. Run commands from this folder.

## Install

```bash
./install.sh                  # .venv, requirements.txt, Playwright Chromium, Ollama models, boards.json
./install.sh --desktop-entry  # also adds "Remote Apply" and "Remote Apply Dashboard" to the app menu
./install.sh --extras         # also installs requirements-optional.txt (Indeed, voice, STORM)
./install.sh --check          # health check at any time
./ra update                   # git pull, then re-run the installer
```

- **Required packages** are pinned in `requirements.txt`. Optional extras (voice mock interviews, STORM research briefs, the Indeed source) are in `requirements-optional.txt`: `.venv/bin/pip install -r requirements-optional.txt`.
- **Ollama** must be running with `nomic-embed-text` (ranking) and `qwen2.5:14b` (local drafting).
- **Keys** go in `~/.config/remote_apply/keys.env` or `.env`, never in `profile.json`: `ANTHROPIC_API_KEY` (optional, see Drafting), `ADZUNA_APP_ID`/`ADZUNA_APP_KEY`, `USAJOBS_API_KEY`/`USAJOBS_EMAIL`, `SERPAPI_KEY` (Google Jobs), and `BRIDGE_USER`/`BRIDGE_PASS` for an IMAP mailbox such as Proton Mail Bridge.

## Set up

1. `./ra` the first time walks you through importing your resume, approving each extracted fact, and answering standard questions.
2. Copy `profile.example.json` to `profile.json` and fill it in. The settings people change most:

| Setting | What it does |
|---|---|
| `mode` | `stage` (you submit), `prepare`, or `submit` (submits on its own within `daily_cap`) |
| `min_fit`, `remote_min_fit` | Match-score floors for local and remote roles |
| `min_salary`, `local_min_salary`, `short_commute_miles` | Pay floor; a lower floor for on-site roles within a short commute |
| `remote_regions` | e.g. `["us","ca","eu","uk"]`; empty = US only (`remote_anywhere: true` = any country) |
| `scam_regions` | Remote listings tied to these places are dropped (defaults cover known scam-centre regions) |
| `cover_letters` | `required` (default: only when the form requires one), `always`, or `false` |
| `draft_model`, `claude_model`, `local_model` | Drafting model; see below |
| `company_per_night`, `company_per_window` | Per-company application caps |
| `agency_daily_cap` | Top matches worked per staffing agency per day (default 2) |
| `captcha_circuit_breaker` | Stop trying a system for the night after this many CAPTCHAs in a row (default 5) |
| `timezone` | `any` or a US zone; remote jobs limited to other zones are skipped |

3. Copy `boards.example.json` to `boards.json` and choose sources: company boards (Greenhouse, Lever, Ashby, Workday, Workable, SmartRecruiters, iCIMS), plus Adzuna, USAJobs, Remotive, Himalayas, Indeed, Google Jobs and staffing agencies (Robert Half, Michael Page, Adecco).

## Drafting (with or without the Anthropic API)

Written answers and cover letters are drafted from your resume only and checked against it before use; anything the check cannot ground goes to you. With `draft_model: "auto"` the program uses Claude (`claude_model`) when an API key with credit is present and switches to the local model on Ollama for the rest of the run if the API fails. Set `draft_model` to an `ollama_chat/...` model to never call the API.

## Nightly run

`./ra schedule on` installs a 3 AM cron job (`./ra nightly`): it closes removed postings, discovers jobs, applies in order of match (fresh postings nudged up), then writes outreach drafts. Only committed code runs from the `ra_run` worktree. The hourly mail sort (`mail_sort.py`) files job email, records confirmations, rejections, interview invitations and employer application limits, trashes old Greenhouse security-code emails and runs the scam monitor.

What the program never does on its own: agree to arbitration or other waivers of your rights, make statements about your own AI use, claim work authorization it has no record of, solve CAPTCHAs, or send messages.

## Dashboard

`./ra dashboard` starts the server if needed and opens <http://127.0.0.1:8675> in your browser. Pages:

- **Overview**: key numbers, outcomes (total applied, rejected, response rate, interviews, awaiting reply), application activity and the pipeline.
- **Action queue**: everything that needs you (forms ready to submit, CAPTCHAs, unanswered questions, submissions to verify, manual leads), with links.
- **Market insights**: the funnel, response rates by role, seniority, application system, match score, remote vs local and resume version, days to rejection and from posting to applying, and phrases postings use that your resume never does.
- **Outreach**: draft notes to recruiters and hiring managers for strong recent applications, and follow-ups for quiet ones. Nothing is sent: copy, send it yourself, mark it sent or skipped.
- **Threats**: the recruitment-scam monitor. Each job-related email is checked for sender authentication (SPF, DKIM, DMARC), its sending IP and network, look-alike or newly registered domains and scam language. High-risk mail moves to `Folders/Suspected Scams`.

## Commands

| Command | What it does |
|---|---|
| `./ra` | Menu: setup, dashboard, run now, what needs me, answers, check a form, replies, schedule, update, health |
| `./ra update` | Pull the latest version and re-run the installer |
| `./ra outreach` / `./ra threats` / `./ra mail` | Outreach drafts, scam monitor, mail sort |
| `./ra nightly` | Everything the nightly run does, now |
| `./ra stale [--apply]` | Find (and close) postings the boards have removed |
| `./ra tracks init\|list\|add\|edit` | Career-track resume versions |
| `apply.py scan` / `apply.py run [--dry]` | Discover and rank / apply (`--dry` never submits) |
| `apply.py apply URL` | Prefill one application in a visible browser |
| `apply.py mark URL applied\|interview\|rejected\|offer\|skip` | Record an outcome yourself |
| `apply.py doctor URL` | Inspect a form without filling it |
| `apply.py answers ...` | Review and approve stored answers |
| `apply.py followups` / `followup-draft URL` / `recruiter-email URL` | Follow-up and outreach drafts |
| `apply.py interview URL [--no-voice]` | Company brief and mock interview (needs the API key) |
| `outreach.py` | Write tonight's outreach drafts |
| `threat_scan.py [--days N]` | Run the scam monitor over recent mail |
| `mail_sort.py [--dry] [--reject-backfill DAYS]` | File job mail now |
| `apply.py export` / `browse` | Rewrite `applications.csv` / open the Datasette tracker |

`run.log` records activity; `proof/` holds screenshots of each submission attempt.
