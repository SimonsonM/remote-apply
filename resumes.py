"""Track-specific resumes  with per-job reordering.

The fact base lives in resume_facts.json (gitignored, PII). Per-job tailoring is deterministic and can only
REORDER approved lines (bullets within a job, skill lines) by overlap with the posting's wording; it never
adds, removes or rewrites text, so nothing unapproved can reach an employer.
"""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FACTS = ROOT / "resume_facts.json"
OUT = ROOT / "resumes"
def _filename():
    """Uploaded resume filename: profile 'resume_filename', else '<Name>_Resume.docx' (employers see this name)."""
    try:
        prof = json.loads((ROOT / "profile.json").read_text())
    except Exception:
        prof = {}
    if prof.get("resume_filename"):
        return prof["resume_filename"]
    name = re.sub(r"[^A-Za-z0-9]+", "", prof.get("name", "")) or "Resume"
    return f"{name}_Resume.docx"


FILENAME = _filename()

TRACKS = {
    "Marketing": r"\b(marketing|brand|content (marketing|strategy|creat\w*)|demand gen|growth marketing|campaigns?|social media|marketing communications|go-to-market|gtm|messaging|positioning|copywrit\w*)\b",
    "ITSecurity": r"\b(security|cyber\w*|iam|pam|identity|access management|soc|grc|infosec|threat|vulnerabilit\w*|it manager|it specialist|systems? administrat\w*|infrastructure|network|endpoint|help ?desk|it support|it operations)\b",
    "AIOps": r"\b(ai|a\.i\.|artificial intelligence|machine learning|ml|mlops|llms?|gen ?ai|generative|agents?|agentic|automation|data science|analytics engineer\w*)\b",
    "ProgramMgmt": r"\b(program|project|operations|ops|implementation|pmo|delivery|scrum|agile|portfolio|business operations|strategy)\b",
}
STOP = set("the and for with you your our are will this that from have has into who what about their they them can all not but more than per job role team work working including using use across within such other also may must able new well both each any".split())


def facts():
    return json.loads(FACTS.read_text())


def pick_variant(title, description=""):
    """Highest weighted match; title counts 5x the first 4000 chars of the description. Ties go to ProgramMgmt."""
    title, desc = title or "", (description or "")[:4000]
    scores = {}
    for name, pat in TRACKS.items():
        rx = re.compile(pat, re.I)
        scores[name] = 5 * len(rx.findall(title)) + len(rx.findall(desc))
    best = max(scores.values())
    if best == 0:
        return "ProgramMgmt"
    order = ["ProgramMgmt", "AIOps", "ITSecurity", "Marketing"]
    title_hits = {n: len(re.findall(TRACKS[n], title, re.I)) for n in order}
    # A clear title signal wins over description volume (e.g. "Product Marketing Manager" at an AI company).
    if max(title_hits.values()):
        top = max(title_hits.values())
        return next(n for n in ["Marketing", "ITSecurity", "AIOps", "ProgramMgmt"] if title_hits[n] == top)
    return next(n for n in order if scores[n] == best)


def _words(text):
    return {w for w in re.findall(r"[a-z][a-z0-9+#/.-]{2,}", (text or "").lower()) if w not in STOP}


def _rank(items, key, words):
    """Stable sort by overlap with the posting; ties keep the approved order."""
    return [x for _, _, x in sorted(((-len(_words(key(x)) & words), i, x) for i, x in enumerate(items)))]


def plan(variant, job_text=None):
    """The exact lines that will be rendered, in order (for tests and audit)."""
    f = facts(); v = f["variants"][variant]
    words = _words(job_text) if job_text else set()
    skills = [f["skills"][k] for k in v["skills"]]
    order = []
    for job, picks in v["order"]:
        lines = [f["jobs"][job][4][p] for p in picks]
        order.append((job, _rank(lines, lambda s: s, words) if words else lines))
    if words:
        skills = _rank(skills, lambda s: s[0] + " " + s[1], words)
    return dict(name=f["name"], contact=f["contact"], headline=v["headline"], summary=v["summary"],
                skills=skills, jobs=order, education=f["education"], certs=f["certs"])


def render(p, path):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, Inches, RGBColor
    f = facts(); ink = RGBColor(0x22, 0x22, 0x22)
    doc = Document()
    st = doc.styles["Normal"]; st.font.name = "Calibri"; st.font.size = Pt(10)
    for s in doc.sections:
        s.top_margin = s.bottom_margin = Inches(0.5); s.left_margin = s.right_margin = Inches(0.6)

    def para(text="", bold=False, size=10, align=None, after=2, italic=False):
        q = doc.add_paragraph(); q.paragraph_format.space_after = Pt(after); q.paragraph_format.space_before = Pt(0)
        if align is not None:
            q.alignment = align
        if text:
            r = q.add_run(text); r.bold = bold; r.italic = italic; r.font.size = Pt(size); r.font.color.rgb = ink
        return q

    def heading(text):
        q = para(text.upper(), bold=True, size=10.5, after=3); q.paragraph_format.space_before = Pt(8)
        bdr = OxmlElement("w:pBdr"); b = OxmlElement("w:bottom")
        for k, val in (("w:val", "single"), ("w:sz", "6"), ("w:space", "1"), ("w:color", "555555")):
            b.set(qn(k), val)
        bdr.append(b); q._p.get_or_add_pPr().append(bdr)

    def bullet(text):
        q = doc.add_paragraph(style="List Bullet"); q.paragraph_format.space_after = Pt(1)
        q.add_run(text).font.size = Pt(10)

    para(p["name"], bold=True, size=16, align=WD_ALIGN_PARAGRAPH.CENTER, after=1)
    para(p["headline"], bold=True, size=10.5, align=WD_ALIGN_PARAGRAPH.CENTER, after=1)
    para(p["contact"], size=9, align=WD_ALIGN_PARAGRAPH.CENTER, after=4)
    heading("Summary"); para(p["summary"])
    heading("Skills")
    for label, text in p["skills"]:
        q = para(after=1); r = q.add_run(label + ": "); r.bold = True; q.add_run(text)
    heading("Experience")
    for job, lines in p["jobs"]:
        title, org, loc, dates, _ = f["jobs"][job]
        q = para(after=0); q.paragraph_format.space_before = Pt(4)
        q.add_run(title).bold = True
        q.add_run(" | " + org + (" | " + loc if loc else ""))
        para(dates, italic=True, size=9.5, after=1)
        for line in lines:
            bullet(line)
    heading("Education")
    for e in p["education"]:
        para(e, after=1)
    heading("Certifications and Training")
    for c in p["certs"]:
        bullet(c)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path


def build_variants():
    return [render(plan(n), OUT / n / FILENAME) for n in facts()["variants"]]


def for_job(job):
    """(path, variant) of the tailored resume for this job; cached by content."""
    variant = pick_variant(job.get("title"), job.get("description"))
    text = f'{job.get("title", "")}\n{(job.get("description") or "")[:6000]}'
    p = plan(variant, text)
    key = hashlib.sha256((FACTS.read_text() + variant + json.dumps(p, sort_keys=True)).encode()).hexdigest()[:16]
    path = OUT / "tailored" / key / FILENAME
    if not path.exists():
        render(p, path)
    return path, variant


if __name__ == "__main__":
    for x in build_variants():
        print(x)
