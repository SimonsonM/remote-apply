# Remote Apply: quick user guide

Remote Apply finds selected employers' remote job postings, ranks them against your resume, and helps prepare applications. Run commands from this folder. Review each employer's posting and every filled answer before submitting.

## Get ready

1. Use the existing `.venv` in this folder, or create one and install the Python packages used by the features you plan to run. Browser actions require Playwright and its Chromium browser (`python -m playwright install chromium`). Scanning and ranking also use MarkItDown, sqlite-vec, and a local Ollama service with `nomic-embed-text` available. Staging uses OR-Tools. Drafting uses DSPy and a configured model. This repository has no pinned dependency file.
2. Check `profile.json`: set your name, email, phone, and `resume` path. The resume path must point to a real file. `profile.example.json` shows the basic fields, but the existing `profile.json` has more options. Check `answers.json` for reusable screening answers; enter only true answers you are comfortable reusing. Keep credentials out of these files.
3. Edit `boards.json` to choose Greenhouse, Lever, and Ashby employer boards. The list is selective, not a complete job feed.

If starting from the example profile, copy it to `profile.json` and supply a resume file yourself. The example names `resume.docx`, which is not included in this folder. An existing `.venv` or Ollama installation may already be configured on this machine.

## Dashboard

Run `./ra dashboard` and open <http://127.0.0.1:8675>. The dashboard reads the current database and shows totals, recent recorded applications, and a searchable list of jobs waiting for your action. Use the status filters to focus on forms ready to submit, CAPTCHAs, other blockers, submissions to verify, or manual applications. Expand **Show application note** for the recorded reason. The page starts in dark mode; the light-mode switch remembers your choice. Refresh to see new database changes, and press Ctrl+C to stop the local server.

## Find and review jobs

```bash
.venv/bin/python apply.py scan
.venv/bin/python apply.py report
xdg-open jobs.html
```

`scan` fetches public board listings and updates `applications.sqlite3`. It needs network access, the local embedding service, and the configured resume. `report` writes `jobs.html`, a shortlist you can open in a browser. Check location eligibility, pay, travel, and whether each listing is still open on the employer site. Fit and ghost scores are estimates.

## Prepare applications

```bash
.venv/bin/python apply.py run
```

With `mode` set to `stage` (the default), `run` scans, chooses jobs within your time budget, and opens prepared forms in a visible browser. The first tab is a checklist; later tabs contain forms. Ashby jobs normally open in your own Chrome with answers on the checklist for you to paste. Read drafted answers and cover letters, complete unanswered fields, solve any CAPTCHA, and submit yourself. The program marks a browser submission as applied only if it sees a confirmation. For an Ashby or other manually completed application, mark it yourself after you have confirmed submission:

```bash
.venv/bin/python apply.py mark 'JOB_URL' applied
```

`run --dry` scans and checks forms in a headless browser without submitting. It uses the nonstaging path and updates job statuses, so it is a dry submission run, not a read-only preview. If `mode` is changed away from `stage`, `run` uses this headless path; `auto_submit: true` permits it to click submit. Review those settings before running.

To prefill one URL in a visible browser, use `.venv/bin/python apply.py apply 'APPLICATION_URL'`. Finish the form manually, then mark the tracked job URL as applied after confirmation. The `apply` command does not have a `--submit` option.

## Track and explore

```bash
.venv/bin/python apply.py export
.venv/bin/python apply.py browse
.venv/bin/python apply.py mark 'JOB_URL' interview
```

`export` rewrites `applications.csv` from the SQLite database. `browse` opens the Datasette tracker. `mark` accepts `applied`, `interview`, `rejected`, `offer`, `skip`, `needs_you`, or `new`; use a URL from the tracker. The CSV is an output file, so make status changes with `mark` rather than editing it. `run.log` records activity, and `proof/` holds staged checklists and any captured screenshots.

Optional commands: `signals` refreshes and summarizes WARN/PERM and posting-risk data; `skills` writes a skill-gap report under `reports/`; `interview 'JOB_URL' --no-voice` creates a company brief and runs a text mock interview for a tracked job. Interview prep requires an Anthropic API key; omit `--no-voice` only when the separate voice environment is set up. `--storm` uses the separate experimental STORM environment when installed.
