"""Interview prep for a job that called back:
  1) a cited company brief (Claude with live web search; STORM if installed and --storm)
  2) a spoken mock interview: Claude plays the hiring manager from the real job posting, Kokoro reads each
     question aloud, you type your answer, and every answer is graded against your resume."""
import json, os, re, subprocess, urllib.request, datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "interviews"
MODEL = "claude-sonnet-5-5"


def _key():
    import smart
    return smart._load_env()


def claude(messages, system="", tools=None, max_tokens=2500):
    body = {"model": MODEL, "max_tokens": max_tokens, "messages": messages}
    if system:
        body["system"] = system
    if tools:
        body["tools"] = tools
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                 headers={"x-api-key": _key(), "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


def text_of(resp):
    out, cites = [], []
    for b in resp.get("content", []):
        if b.get("type") == "text":
            out.append(b["text"])
            for c in b.get("citations") or []:
                if c.get("url") and c["url"] not in cites:
                    cites.append(c["url"])
    return "".join(out).strip(), cites


def brief(job, use_storm=False):
    OUT.mkdir(exist_ok=True)
    slug = re.sub(r"\W+", "_", job["company"]).strip("_")
    path = OUT / f"{slug}_brief.md"
    if use_storm and (ROOT / ".venv-storm").exists():
        r = subprocess.run([str(ROOT / ".venv-storm/bin/python"), str(ROOT / "storm_brief.py"), job["company"], str(path)],
                           capture_output=True, text=True, timeout=1800)
        if r.returncode == 0 and path.exists():
            return path.read_text()
        print("STORM failed, using Claude web search instead:", r.stderr[-300:])
    prompt = (f"Research {job['company']} for a candidate interviewing for '{job['title']}'. Use web search. Cover: what the company "
              f"sells and to whom, business model and scale, recent news from the last 12 months (funding, launches, layoffs, leadership), "
              f"how the team behind this role likely fits in, competitors, and 5 sharp questions the candidate could ask. "
              f"Be factual and concise; cite sources.\n\nJob posting:\n{job['description'][:5000]}")
    resp = claude([{"role": "user", "content": prompt}], tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
                  max_tokens=4000)
    txt, cites = text_of(resp)
    md = f"# {job['company']} - brief for {job['title']}\n_{dt.date.today()}_\n\n{txt}\n\n## Sources\n" + "\n".join(f"- {c}" for c in cites)
    path.write_text(md)
    return md


def speak(text, voice=True):
    print(f"\n\033[1;36mInterviewer:\033[0m {text}\n")
    if voice and (ROOT / ".venv-voice").exists():
        try:
            subprocess.run([str(ROOT / ".venv-voice/bin/python"), str(ROOT / "say.py"), text], timeout=120,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def mock(job, profile, company_brief, n=7, voice=True):
    import smart
    resume = smart.resume_text(profile)
    qs = claude([{"role": "user", "content":
                  f"You are the hiring manager for '{job['title']}' at {job['company']}. Write {n} interview questions you would really ask: "
                  f"2 behavioral, 3 role-specific from the posting, 1 about a gap between this resume and the posting, 1 about the company. "
                  f"Return ONLY a JSON list of strings.\n\nPOSTING:\n{job['description'][:5000]}\n\nRESUME:\n{resume}\n\nCOMPANY BRIEF:\n{company_brief[:3000]}"}])
    raw, _ = text_of(qs)
    questions = json.loads(re.search(r"\[.*\]", raw, re.S).group(0))
    slug = re.sub(r"\W+", "_", job["company"]).strip("_")
    log = OUT / f"{slug}_mock_{dt.datetime.now():%Y%m%d_%H%M}.md"
    lines = [f"# Mock interview - {job['company']} - {job['title']}\n"]
    print("\nType your answer as you would say it. Finish with an empty line. Type 'quit' to stop.\n")
    for i, q in enumerate(questions, 1):
        speak(q, voice)
        buf = []
        while True:
            line = input()
            if not line.strip():
                break
            buf.append(line)
        ans = "\n".join(buf).strip()
        if ans.lower() == "quit":
            break
        grade, _ = text_of(claude([{"role": "user", "content":
            f"Grade this interview answer for '{job['title']}' at {job['company']}.\nQUESTION: {q}\nANSWER: {ans}\n\nRESUME (ground truth):\n{resume}\n\n"
            f"Reply in this format, plain text, under 180 words:\nScore: x/5\nWorked: ...\nMissing: ...\nUnsupported by resume: (any claim the resume "
            f"does not back up, or 'none')\nStronger version (60 seconds, STAR, only resume facts): ..."}], max_tokens=700))
        print(f"\033[1;33m{grade}\033[0m")
        lines += [f"## Q{i}. {q}\n", f"**Your answer:** {ans}\n", f"{grade}\n"]
        log.write_text("\n".join(lines))
    print(f"\nSaved: {log}")
