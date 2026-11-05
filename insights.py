"""Skill-gap report: what the jobs you match actually ask for, what your resume covers, and which skills travel together."""
import datetime as dt, html, json, re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"


def _lm(profile):
    import dspy, smart
    smart._load_env()
    if str(profile.get("draft_model", "auto")).startswith("ollama"):
        model = profile["draft_model"]   # local-only mode: no Anthropic API calls
    else:
        model = "anthropic/" + profile.get("extract_model", "claude-haiku-4-5-20251001") if smart._load_env() else "ollama_chat/qwen2.5:14b"
    return smart._make_lm(model)


def extract(conn, profile, log, limit=150):
    import dspy

    class Reqs(dspy.Signature):
        """List the concrete skills, tools, certifications, domains and experience requirements this job asks for.
        Normalize each to a short lowercase canonical name (1-4 words), e.g. 'jira', 'sql', 'agile delivery', 'pmp',
        'stakeholder management', 'saas implementation', 'python', 'llm applications', 'security+'.
        Mark each as required (must-have) or preferred. Skip perks, values and generic traits like 'communication'."""
        job: str = dspy.InputField()
        required: list[str] = dspy.OutputField()
        preferred: list[str] = dspy.OutputField()

    conn.execute("CREATE TABLE IF NOT EXISTS job_skills (url TEXT, skill TEXT, kind TEXT)")
    done = {r[0] for r in conn.execute("SELECT DISTINCT url FROM job_skills")}
    jobs = conn.execute("SELECT url, company, title, description FROM jobs WHERE fit >= ? AND status NOT IN ('skip','closed','low_fit') "
                        "ORDER BY fit DESC LIMIT ?", (profile.get("min_fit", 66), limit)).fetchall()
    todo = [j for j in jobs if j[0] not in done]
    if todo:
        lm = _lm(profile)
        pred = dspy.Predict(Reqs)
        log(f"skills: extracting requirements from {len(todo)} jobs")
        for url, co, title, desc in todo:
            try:
                with dspy.context(lm=lm):
                    out = pred(job=f"{title} at {co}\n{desc[:7000]}")
                rows = [(url, s.strip().lower()[:60], "required") for s in out.required if s.strip()]
                rows += [(url, s.strip().lower()[:60], "preferred") for s in out.preferred if s.strip()]
                conn.executemany("INSERT INTO job_skills VALUES (?,?,?)", rows or [(url, "_none", "none")])
                conn.commit()
            except Exception as e:
                log(f"skills: failed {co} - {title}: {str(e)[:100]}")
    return [j[0] for j in jobs]


def coverage(skills, profile):
    """Ask the model, once, which skills the resume covers."""
    import dspy, smart

    class Cover(dspy.Signature):
        """For each skill, judge from the resume ONLY: 'yes' (clearly shown), 'partial' (adjacent or implied), or 'no'.
        Be strict - do not give credit for things the resume does not state."""
        resume: str = dspy.InputField()
        skills: list[str] = dspy.InputField()
        verdicts: dict[str, str] = dspy.OutputField(desc="skill -> yes|partial|no")

    with dspy.context(lm=_lm(profile)):
        out = dspy.Predict(Cover)(resume=smart.resume_text(profile), skills=skills)
    return {k.lower(): v.lower() for k, v in out.verdicts.items()}


def report(conn, profile, log):
    urls = extract(conn, profile, log)
    if not urls:
        log("skills: no matching jobs")
        return None
    q = ",".join("?" * len(urls))
    rows = conn.execute(f"SELECT url, skill, kind FROM job_skills WHERE url IN ({q}) AND skill <> '_none'", urls).fetchall()
    njobs = len({r[0] for r in rows}) or 1
    req, anyk = Counter(), Counter()
    per_job = {}
    for url, s, kind in rows:
        anyk[s] += 1
        if kind == "required":
            req[s] += 1
        per_job.setdefault(url, set()).add(s)
    top = [s for s, _ in anyk.most_common(60)]
    cov = coverage(top, profile)

    # HyperNetX: jobs are hyperedges over skills -> which skills travel together
    bundles = []
    try:
        import hypernetx as hnx
        keep = set(top[:40])
        edges = {u: sorted(s & keep) for u, s in per_job.items() if len(s & keep) >= 2}
        H = hnx.Hypergraph(edges)
        pairs = Counter()
        for sk in edges.values():
            for i in range(len(sk)):
                for j in range(i + 1, len(sk)):
                    pairs[(sk[i], sk[j])] += 1
        for (a, b), c in pairs.most_common(15):
            bundles.append((a, b, c))
        log(f"skills: hypergraph {len(H.nodes)} skills x {len(H.edges)} jobs")
    except Exception as e:
        log(f"skills: hypergraph skipped: {e}")

    gaps = [(s, anyk[s], req[s]) for s in top if cov.get(s, "no") == "no"]
    partial = [(s, anyk[s], req[s]) for s in top if cov.get(s) == "partial"]
    have = [(s, anyk[s], req[s]) for s in top if cov.get(s) == "yes"]
    e = html.escape

    def tbl(items, cls):
        return "".join(f"<tr class={cls}><td>{e(s)}</td><td>{round(100 * n / njobs)}%</td><td>{round(100 * r / njobs)}%</td>"
                       f"<td><div class=bar style='width:{round(100 * n / njobs)}%'></div></td></tr>" for s, n, r in items)
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"skill_gap_{dt.date.today()}.html"
    out.write_text(f"""<!doctype html><meta charset=utf-8><title>Skill Gap Report</title><style>
body{{font:15px system-ui;background:#111418;color:#d8dde3;max-width:1000px;margin:2rem auto;padding:1rem}}
h1,h2{{font-weight:600}} table{{border-collapse:collapse;width:100%;margin-bottom:2rem}} td,th{{padding:.4rem;border-bottom:1px solid #2a3038;text-align:left}}
.bar{{height:8px;background:#4f7cff;border-radius:4px}} tr.gap td:first-child{{color:#ff9b8a}} tr.part td:first-child{{color:#f0c674}}
tr.have td:first-child{{color:#8fd19e}} .m{{color:#9fb3c8}}</style>
<h1>Skill gap - {njobs} matching jobs</h1><p class=m>% of jobs mentioning the skill / % listing it as required. Coverage judged strictly from your resume.</p>
<h2>Gaps - asked for, not on your resume</h2><table><tr><th>Skill</th><th>Any</th><th>Required</th><th></th></tr>{tbl(gaps[:20], 'gap')}</table>
<h2>Partial - adjacent experience, make it explicit</h2><table><tr><th>Skill</th><th>Any</th><th>Required</th><th></th></tr>{tbl(partial[:20], 'part')}</table>
<h2>Covered</h2><table><tr><th>Skill</th><th>Any</th><th>Required</th><th></th></tr>{tbl(have[:20], 'have')}</table>
<h2>Skills that travel together</h2><table><tr><th>Pair</th><th>Jobs</th></tr>{''.join(f'<tr><td>{e(a)} + {e(b)}</td><td>{c}</td></tr>' for a, b, c in bundles)}</table>
""")
    log(f"skills: report -> {out}")
    return out
