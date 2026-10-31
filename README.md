# Remote Apply

A self-hosted job application assistant. It finds openings, ranks them against your resume, fills out the applications, and tracks every one. It runs on your own computer with local AI models, so your resume and answers stay on your machine.

## Career tracks

A career track is a resume version with its own headline, summary, selected jobs, bullets, skills, and job-title keywords. Run `./ra tracks init` after approving your resume facts, then `./ra tracks add NAME` to create another version. See `resume_facts.example.json` for a fictional, renderable example. Your `resume_facts.json` is a private local file saved with mode 0600. Per-job tailoring only reorders your own approved lines.

It never invents qualifications or screening answers, and it never counts an application as submitted just because a button was clicked.

## What it does

- **Finds jobs** on Greenhouse, Lever, Ashby, Workday, Workable, SmartRecruiters and iCIMS boards, plus Adzuna and USAJobs.
- **Ranks them** by semantic fit: 60% job title against your target roles, 40% description against your resume (local embeddings via Ollama).
- **Fills applications** with Playwright, using only answers you have reviewed and approved. Free-text questions get drafts grounded only in your resume.
- **Flags fake or stale postings** using WARN Act layoffs, PERM labor-certification ads, posting age and reposts, and closes postings the board has removed.
- **Plans each night** with an optimizer that picks the best jobs for your time budget, and learns your taste from what you apply to or skip.
- **Tracks everything** in SQLite: attempts, field-level results, blockers, confirmations, employer responses and follow-ups.
- **Shows it all** on a local dashboard, with a "needs you" queue for anything only a person can finish.

## Requirements

- Linux (developed on Ubuntu 24.04)
- Python 3.12
- [Ollama](https://ollama.com) with `nomic-embed-text` and `qwen2.5:14b`
- Optional: an IMAP mailbox (such as Proton Mail Bridge) for security codes and confirmation emails
- Optional: free API keys for Adzuna and USAJobs

## Quick start

```bash
git clone https://github.com/SimonsonM/remote-apply
cd remote-apply
./install.sh
./ra
```

The first launch walks you through importing your resume, approving each extracted fact, and answering common application questions.

Then make it yours:

1. **Profile**: copy `profile.example.json` to `profile.json` and fill in your details, including the `home_area` block (home coordinates, nearby towns and an optional second metro) if you want local on-site or hybrid roles. Without it, only remote roles qualify. Never put passwords in this file.
2. **Employers**: copy `boards.example.json` to `boards.json` and edit the company lists and Adzuna search terms.
3. **Role filters**: the defaults target program, project, operations and AI roles. Set `title_filter` (a regex) and `exclude_titles` (a list) in `profile.json` for your field.
4. **Timezone**: set `"timezone"` in `profile.json` to `any` (default), `eastern`, `central`, `mountain`, `arizona`, `pacific`, `alaska` or `hawaii` (EST, CST, MST, PST and similar also work). Remote jobs limited to other timezones are skipped; `any` keeps them all.
5. **API keys**: put Adzuna, USAJobs and mail credentials in `.env`, never in the profile.

`./install.sh --check` runs a health check at any time.

## Everyday use

| Command | What it does |
|---|---|
| `./ra` | Interactive menu |
| `./ra setup` | Change your profile and standard answers |
| `./ra dashboard` | Local dashboard at http://127.0.0.1:8675 |
| `./ra schedule on\|off\|status` | Turn the nightly 3 AM run on or off |
| `./ra stale [--apply]` | Find (and close) postings the boards have removed |
| `python apply.py apply URL` | Prefill one application in a visible browser |
| `python apply.py mark URL applied` | Record an application you submitted yourself |
| `python apply.py doctor URL` | Inspect a form's fields without filling or submitting |
| `python apply.py followups` | See follow-ups that are due |
| `python apply.py recruiter-email URL` | Write a grounded recruiter email draft without sending it |
| `python apply.py interview URL` | Company brief and a spoken mock interview |

See the [user guide](USER_GUIDE.md) for every command and the [technical brief](TECH_BRIEF.md) for the architecture.

## Modes

Set `"mode"` in `profile.json`:

- **`stage`** (default): fills applications and leaves them open for you to review and submit.
- **`prepare`**: fills headlessly and lets you finish later with `apply.py review`.
- **`submit`**: submits on its own, within a standing authorization you define and a daily cap.

Drafted answers and cover letters always need your review unless you explicitly allow them. Sensitive categories (EEO, consent, attestations, salary) need explicit approval. Anything that would waive your rights, such as arbitration agreements or non-competes, is always sent to you instead of being answered.

## How ranking works

- **Fit**: your resume and each job are embedded locally into sqlite-vec. Jobs below `min_fit` (default 66) are parked.
- **Ghost score (0 to 100)**: posting age, PERM matches, identical reposts and recent layoffs raise it; a posted salary lowers it. Jobs at or above `max_ghost` (default 70) are never queued.
- **Nightly plan**: OR-Tools picks the set of jobs worth the most that fits `nightly_minutes`, with at most `per_company_max` per company.
- **Learning**: River learns from what you submit and skip, and starts shaping the ranking after about 10 decisions.

## Privacy

Everything runs locally. Your profile, resume, answers, employer list and tracking database are gitignored and stay on your machine. Network calls go only to job boards, your optional mail server, and public data sources (WARN and PERM filings).

## License

MIT. See [LICENSE](LICENSE).
