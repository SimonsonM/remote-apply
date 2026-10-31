"""Nightly plan: value each job, learn from the user's choices (River), and pick a set that fits the time budget (OR-tools)."""
import pickle, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL = ROOT / "data" / "river_model.pkl"
STOP = {"and", "of", "the", "a", "-", "&", "|", "for", "to", "in", "ii", "iii", "iv", "sr", "senior", "staff", "lead", "us", "remote", "usa"}


def features(job, sig):
    f = {"fit": (job["fit"] or 0) / 100, "ghost": (job.get("ghost") or 0) / 100, "eu": 1.0 if job.get("hq") == "Europe" else 0.0, "ca": 1.0 if job.get("hq") == "Canada" else 0.0,
         "src_" + (job.get("source") or "x"): 1.0, "salary_posted": 1.0 if job.get("salary") else 0.0,
         "surge": float((sig or {}).get("surge") or 0), "co_" + re.sub(r"\W", "", job["company"].lower()): 1.0}
    for w in re.findall(r"[a-z]+", job["title"].lower()):
        if w not in STOP and len(w) > 2:
            f["t_" + w] = 1.0
    return f


def _model():
    if MODEL.exists():
        return pickle.loads(MODEL.read_bytes())
    from river import compose, linear_model, preprocessing
    return {"n": 0, "m": compose.Pipeline(preprocessing.StandardScaler(), linear_model.LogisticRegression())}


def learn(job, sig, submitted):
    """Call when the user submits (True) or deliberately closes / skips (False) a job."""
    st = _model()
    st["m"].learn_one(features(job, sig), bool(submitted))
    st["n"] += 1
    MODEL.parent.mkdir(exist_ok=True)
    MODEL.write_bytes(pickle.dumps(st))


def preference(job, sig):
    st = _model()
    if st["n"] < 10:
        return None
    return st["m"].predict_proba_one(features(job, sig)).get(True, 0.5)


def value(job, sig, prof):
    """Expected value of applying tonight: fit, minus ghost risk, plus Europe and hiring-surge bonuses, shaped by learned taste."""
    v = max(0.0, (job["fit"] or 0) - prof.get("min_fit", 66) + 5)  # 0..~15 for in-pool jobs
    v *= 1 - min(0.8, (job.get("ghost") or 0) / 125)
    if job.get("hq") in ("Europe", "Canada"):
        v *= 1.15
    if (sig or {}).get("surge"):
        v *= 1 + min(0.5, (sig["surge"] - 1))
    p = preference(job, sig)
    if p is not None:
        v *= 0.5 + p
    return round(v, 3)


def minutes(job):
    m = {"greenhouse": 5, "lever": 5, "ashby": 9}.get(job.get("source"), 6)
    return m + 2  # cover letter / draft review


def pick(candidates, prof, sigs):
    """Knapsack with CP-SAT: maximize total value within nightly_minutes, at most per_company_max per company."""
    from ortools.sat.python import cp_model
    budget = prof.get("nightly_minutes", 60)
    cap = prof.get("stage_cap", 12)
    per_co = prof.get("per_company_max", 2)
    auto = {'greenhouse', 'lever', 'ashby', 'workday', 'workable', 'smartrecruiters', 'icims'}
    pool = sorted(candidates, key=lambda j: (j.get('source') not in auto, -j["value"]))[:80]
    if not pool:
        return []
    md = cp_model.CpModel()
    x = [md.NewBoolVar(f"x{i}") for i in range(len(pool))]
    md.Add(sum(minutes(j) * x[i] for i, j in enumerate(pool)) <= budget)
    md.Add(sum(x) <= cap)
    for co in {j["company"] for j in pool}:
        md.Add(sum(x[i] for i, j in enumerate(pool) if j["company"] == co) <= per_co)
    md.Maximize(sum(int(j["value"] * 1000) * x[i] for i, j in enumerate(pool)))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10
    solver.Solve(md)
    chosen = [j for i, j in enumerate(pool) if solver.Value(x[i])]
    return sorted(chosen, key=lambda j: (j.get('source') not in auto, -j["value"]))
