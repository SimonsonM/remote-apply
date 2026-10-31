# Remote Apply: technical brief

Remote Apply is a local Python CLI (`apply.py`) backed by SQLite (`applications.sqlite3`). It fetches public Greenhouse, Lever, and Ashby board APIs listed in `boards.json`, keeps postings by URL, and exports human-readable HTML and CSV views. `profile.json` and `answers.json` hold applicant details and reusable answers.

## Processing path

1. `scan` filters for remote, US-eligible locations and excludes selected job titles. `smart.py` converts the configured resume to `resume.md` with MarkItDown, embeds resume, title, and description text through local Ollama (`nomic-embed-text`), stores vectors with sqlite-vec, and computes a weighted fit score. The resume and title weights are configurable.
2. `signals.py` imports local WARN and PERM data when available, estimates ghost risk from posting age, repeated titles, wording, salary disclosure, and company signals, and tracks hiring surges. These are heuristics, not proof that a posting is genuine or closed.
3. In staging mode, `plan.py` discounts risky jobs, applies regional and surge bonuses, and uses OR-Tools to select work within `nightly_minutes`, `stage_cap`, and `per_company_max`. River learns from confirmed submissions and deliberate skips after enough feedback.
4. `apply.py` uses Playwright to inspect and fill forms. It resolves known profile and answer-bank fields; `smart.py` may draft free-text answers and cover letters through DSPy using either local Ollama or Anthropic, depending on configuration. Unknown required answers remain for the user. Ashby defaults to manual Chrome staging.
5. The staged browser session writes a checklist in `proof/`, watches for confirmation text after manual submission, and updates job status. The separate headless path can submit only when `mode` is not `stage` and `auto_submit` is true. A click without confirmation is `unconfirmed`.

## Other modules and outputs

`insights.py` generates HTML skill-gap reports. `interview.py` produces company briefs and mock interviews through Anthropic; `storm_brief.py` and `say.py` support optional STORM research and Kokoro voice. `browse` launches Datasette with `datasette.json` and `static/dark.css`. `jobs.html`, `applications.csv`, `reports/`, `interviews/`, `proof/`, and `run.log` are generated artifacts. `nightly.sh` and `warn_weekly.sh` are machine-specific scripts with a hard-coded folder path and local environment assumptions.

## Operating limits

The repository has no pinned package manifest or automated tests. A fresh installation needs the relevant Python packages, Playwright browser, Ollama models, and optional services configured separately. External board layouts and APIs can change; form filling is best effort. Resume matching, company signals, generated text, and confirmation detection need human review before treating an application as complete. The local database and profile files contain applicant data and should be handled accordingly.
