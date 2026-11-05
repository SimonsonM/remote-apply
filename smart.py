"""Semantic job matching (MarkItDown + Ollama nomic-embed-text + sqlite-vec) and
answer drafting (DSPy + local LLM) for Remote Apply."""
import hashlib, json, os, re, struct, urllib.request
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESUME_MD = ROOT / "resume.md"
OLLAMA = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"
DIM = 768


# ------------------------------------------------------------- resume text
def resume_text(profile):
    src = (ROOT / profile["resume"]).resolve() if not Path(profile["resume"]).is_absolute() else Path(profile["resume"])
    if not RESUME_MD.exists() or RESUME_MD.stat().st_mtime < src.stat().st_mtime:
        from markitdown import MarkItDown
        RESUME_MD.write_text(MarkItDown().convert(str(src)).text_content)
    return RESUME_MD.read_text()


def grounded_resume_text(profile):
    """Draft evidence comes only from approved facts once a resume was reviewed."""
    import state
    path = Path(profile.get('_db_path', os.environ.get('RA_DB', ROOT / 'applications.sqlite3')))
    if path.exists():
        with sqlite3.connect(path) as conn:
            if 'profile_facts' in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
                count = conn.execute("SELECT count(*) FROM profile_facts WHERE source LIKE 'resume:%'").fetchone()[0]
                if count:
                    return '\n'.join(f'{key}: {value}' for key,value in conn.execute(
                        "SELECT key,value FROM profile_facts WHERE approved=1 AND status='approved' AND source LIKE 'resume:%' AND (review_by IS NULL OR review_by>?) ORDER BY id", (state.now(),)))
    return resume_text(profile)


def excluded_resume_claims(profile):
    """Names and dates from rejected or stale roles cannot be drafting evidence."""
    path = Path(profile.get('_db_path', os.environ.get('RA_DB', ROOT / 'applications.sqlite3')))
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        if 'profile_facts' not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            return []
        values=[]
        for key,value in conn.execute("SELECT key,value FROM profile_facts WHERE status IN ('dropped','stale','proposed') OR approved=0"):
            if key=='role':
                values.extend(part.strip() for part in value.split('|')[:2] if part.strip())
            elif key in ('certification','skill'):
                values.append(value)
        approved = grounded_resume_text(profile).casefold()
        return [value for value in values if value.casefold() not in approved]


# ------------------------------------------------------------- embeddings
def embed(text, kind):
    body = json.dumps({"model": EMBED_MODEL, "prompt": f"{kind}: {text[:8000]}"}).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/embeddings", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["embedding"]


def pack(v):
    return struct.pack(f"{len(v)}f", *v)


def attach_vec(conn):
    import sqlite_vec
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS job_vec USING vec0(embedding float[{DIM}] distance_metric=cosine)")
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS title_vec USING vec0(embedding float[{DIM}] distance_metric=cosine)")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v BLOB)")


def score_jobs(conn, profile, log):
    """Embed any un-embedded jobs, then set jobs.fit = cosine similarity to resume (0-100)."""
    attach_vec(conn)
    res = resume_text(profile)
    row = conn.execute("SELECT v FROM meta WHERE k='resume_mtime'").fetchone()
    mt = str(RESUME_MD.stat().st_mtime)
    if not row or row[0] != mt:  # resume changed -> new resume vector
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('resume_vec', ?)", (pack(embed(res, "search_query")),))
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('resume_mtime', ?)", (mt,))
    rvec = conn.execute("SELECT v FROM meta WHERE k='resume_vec'").fetchone()[0]
    target = profile.get("target_roles", "")
    row = conn.execute("SELECT v FROM meta WHERE k='target_text'").fetchone()
    if not row or row[0] != target:
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('target_vec', ?)", (pack(embed(target, "search_query")),))
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('target_text', ?)", (target,))
    tvec = conn.execute("SELECT v FROM meta WHERE k='target_vec'").fetchone()[0]
    n = 0
    for table, text_sql, kind in (("job_vec", "title || ' at ' || company || char(10) || description", "search_document"),
                                  ("title_vec", "title", "search_document")):
        todo = conn.execute(f"SELECT rowid, {text_sql} FROM jobs WHERE rowid NOT IN (SELECT rowid FROM {table})").fetchall()
        for rid, text in todo:
            try:
                conn.execute(f"INSERT INTO {table}(rowid, embedding) VALUES (?, ?)", (rid, pack(embed(text, kind))))
                n += 1
            except Exception as e:
                log(f"embed failed rowid {rid}: {e}")
        conn.commit()
    if n:
        log(f"embedded {n} new vectors")
    w = profile.get("title_weight", 0.6)
    conn.execute(f"""UPDATE jobs SET fit = (SELECT round(100 * ({w} * (1 - vec_distance_cosine(t.embedding, :t))
                                                + {1 - w} * (1 - vec_distance_cosine(d.embedding, :r))), 1)
                     FROM title_vec t JOIN job_vec d ON d.rowid = t.rowid WHERE t.rowid = jobs.rowid)""", {"t": tvec, "r": rvec})
    conn.commit()


# ------------------------------------------------------------- drafting
DRAFTABLE = re.compile(r"why|tell us|describe|share|explain|what (makes|excites|interests|about)|how would you|philosophy|approach|"
                       r"cover letter|anything else|additional information|motivat|example of|walk us", re.I)
NEVER_DRAFT = re.compile(r"salary|compensation|years|zip|postal|address|phone|date|visa|sponsor|authori[sz]|clearance|"
                         r"convicted|criminal|gender|race|veteran|disab|pronoun|arbitrat", re.I)
_lm = None


def _load_env():
    import os
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return os.environ.get("ANTHROPIC_API_KEY")


LOCAL_MODEL = "ollama_chat/qwen2.5:14b"


def _make_lm(model, local=None):
    import dspy
    if model.startswith("ollama"):
        return dspy.LM(model, max_tokens=900, temperature=0.4, api_base=OLLAMA + "/v1", timeout=300)

    class FallbackLM(dspy.LM):
        """Claude first; on any API failure (no credit, auth, rate limit, outage) switch this run to the local
        model on Ollama so the program keeps working without the Anthropic API."""
        def __call__(self, *args, **kwargs):
            global _lm
            try:
                return super().__call__(*args, **kwargs)
            except Exception as exc:
                fallback = _make_lm(local or LOCAL_MODEL)
                dspy.configure(lm=fallback)
                _lm = fallback
                print(f"Claude unavailable ({str(exc).splitlines()[0][:120]}); using {local or LOCAL_MODEL} for the rest of this run", flush=True)
                return fallback(*args, **kwargs)

    return FallbackLM(model, max_tokens=800)  # newer Claude models reject temperature


def _setup(profile, force_local=False):
    global _lm
    if _lm is None or force_local:
        import dspy
        model = profile.get("draft_model", "auto")
        if force_local:
            model = profile.get("local_model", LOCAL_MODEL)
        elif model == "auto":
            model = "anthropic/" + profile.get("claude_model", "claude-sonnet-5-5") if _load_env() else profile.get("local_model", LOCAL_MODEL)
        _lm = _make_lm(model, profile.get("local_model", LOCAL_MODEL))
        dspy.configure(lm=_lm)


def drafter():
    import dspy

    class Answer(dspy.Signature):
        """Draft the applicant's answer to one job-application question, first person.
        Use a concrete job detail and one or two specific items from the approved resume facts.
        Explain how those facts transfer to the role. Use ONLY approved facts; never invent
        employers, titles, dates, metrics, tools, certifications or outcomes. Banned words:
        innovative, passion, passionate, dynamic, excited, resonate, cutting-edge, leverage, synergy, thrilled, align,
        journey, empower, robust, seamless. No greeting, no sign-off, no exclamation marks. 70-140 words.
        Sound like a practical operator, not a cover-letter template."""
        resume: str = dspy.InputField()
        job: str = dspy.InputField(desc="company, role title and job description")
        question: str = dspy.InputField()
        answer: str = dspy.OutputField(desc="the answer text only")

    return dspy.Predict(Answer)


_pred = None


CONDITIONAL = re.compile(r"if (you )?(selected|answered|chose|yes|no|other|applicable)|if so|please specify|other.{0,20}(specify|detail)", re.I)
STRONG = re.compile(r"why|what (makes|excites|interests|about)|tell us|cover letter|describe (a|your)|walk us", re.I)


def draftable(label, kind, required=True):
    if kind not in ("textarea", "text") or NEVER_DRAFT.search(label) or CONDITIONAL.search(label) or re.search(r"cover.?letter", label, re.I):
        return False
    return bool(STRONG.search(label)) or (required and (kind == "textarea" or DRAFTABLE.search(label)))


def draft(profile, job, question):
    global _pred
    _setup(profile)
    if _pred is None:
        _pred = drafter()
    jd = f"{job['company']} - {job['title']}\n{job['description'][:5000]}"
    try:
        out = _pred(resume=grounded_resume_text(profile), job=jd, question=question).answer.strip()
    except Exception:
        if _lm.model.startswith("ollama"):
            raise
        _setup(profile, force_local=True)  # Claude failed (bad key, outage) -> local model
        out = _pred(resume=grounded_resume_text(profile), job=jd, question=question).answer.strip()
    return out


# ------------------------------------------------------------- cover letters
COVER_DIR = ROOT / "cover_letters"
_cl = None


def _cover_pred():
    import dspy

    class CoverLetter(dspy.Signature):
        """Write the applicant's cover letter for this specific job.
        Body only (no address block, no date, no greeting, no sign-off - those are added separately).
        3 short paragraphs, 200-300 words total:
        (1) the role and one or two things in the job description the applicant can credibly deliver;
        (2) concrete evidence from approved resume facts and how it connects to requirements;
        (3) a plain, honest close about areas to learn and interest in a conversation.
        Mention gaps ONLY in paragraph 3, once, briefly - never open with what he lacks. Keep paragraph 2 under 130 words; pick the 2-3 strongest proof points, not all of them.
        Hard rules: use ONLY approved facts; never invent employers, titles, dates, numbers, tools, certifications or
        outcomes. If a key requirement is not covered, do not claim it. The credential is
        Scrum Fundamentals Certificate (SFC) from SCRUMstudy, never Scrum Fundamentals Certification.
        Microsoft Learn learning paths are training, never certifications.
        Banned words: innovative, passion, passionate, dynamic, excited, thrilled, resonate, cutting-edge, leverage,
        synergy, journey, empower, robust, seamless, delighted. No exclamation marks. Practical operator voice."""
        resume: str = dspy.InputField()
        job: str = dspy.InputField(desc="company, role title and job description")
        body: str = dspy.OutputField(desc="the letter body: 3 paragraphs separated by blank lines")

    return dspy.Predict(CoverLetter)


def cover_letter(profile, job):
    """Return (text, docx_path) for a tailored cover letter."""
    global _cl
    _setup(profile)
    if _cl is None:
        _cl = _cover_pred()
    jd = f"{job['company']} - {job['title']}\n{job['description'][:6000]}"
    try:
        body = (_cl(resume=grounded_resume_text(profile), job=jd).body or '').strip()
        if not body:
            raise ValueError('empty cover letter')
    except Exception:
        try:
            _setup(profile, force_local=True)
            _cl = _cover_pred()
            body = (_cl(resume=grounded_resume_text(profile), job=jd).body or '').strip()
            if not body:
                raise ValueError('empty cover letter')
        except Exception:
            body = (f"I am writing about the {job['title']} role at {job['company']}. "
                    "I would welcome the opportunity to discuss how my documented experience relates to the work.\n\n"
                    "Thank you for considering my application.")
    body = re.sub(r'Scrum Fundamentals Certification', 'Scrum Fundamentals Certificate (SFC)', body, flags=re.I)
    body = re.sub(r'Microsoft Learn certification', 'Microsoft Learn training', body, flags=re.I)
    text = f"Dear {job['company']} Hiring Team,\n\n{body}\n\nSincerely,\n{profile.get('name', '')}"
    from docx import Document
    from docx.shared import Pt
    COVER_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9]+", "", job["company"])
    key = re.sub(r"[^A-Za-z0-9_-]+", "", str(job.get("job_id") or "")) or hashlib.sha256(str(job.get("url", "")).encode()).hexdigest()[:12]
    (COVER_DIR / f"{safe}_{key}").mkdir(parents=True, exist_ok=True)
    path = COVER_DIR / f"{safe}_{key}" / f"CoverLetter_{safe}.docx"
    d = Document()
    st = d.styles["Normal"]; st.font.name = "Calibri"; st.font.size = Pt(11)
    head = d.add_paragraph()
    head.add_run(profile.get('name', '')).bold = True
    d.add_paragraph(f"{profile.get('location', '')} | {profile['email']} | {profile['phone']} | {profile.get('linkedin', '')}")
    import datetime as _dt
    d.add_paragraph(_dt.date.today().strftime("%B %-d, %Y"))
    d.add_paragraph(f"Re: {job['title']}")
    for para in text.split("\n\n"):
        d.add_paragraph(para)
    d.save(path)
    return text, path


def recruiter_email(profile, job):
    """Generate text only; the caller checks grounding before writing a draft."""
    import dspy
    _setup(profile)

    class RecruiterEmail(dspy.Signature):
        """Write a short first-person recruiter email (80-120 words). Use only facts in the resume.
        Mention one specific relevant resume fact. Do not invent employers, titles, dates,
        numbers, tools, credentials, or outcomes. Return a Subject line, body, and name sign-off."""
        resume: str = dspy.InputField()
        job: str = dspy.InputField()
        name: str = dspy.InputField()
        email: str = dspy.OutputField()

    jd = f"{job['company']} - {job['title']}\n{job.get('description', '')[:6000]}"
    return (dspy.Predict(RecruiterEmail)(resume=grounded_resume_text(profile), job=jd,
                                         name=profile.get('name', '')).email or '').strip()


_LEADERS = {'at', 'as', 'the', 'in', 'with', 'my', 'our', 'your', 'dear', 'while', 'during', 'from', 'for', 'on',
            'this', 'that', 'since', 'when', 'after', 'before', 'through', 'across', 'by', 'a', 'an', 'and', 'but',
            'i', 'most', 'recently', 'currently', 'today', 'previously', 'before', 'also', 'together', 'both', 'if', 'because', 'although', 'though', 'once', 'whether', 'unless', 'until', 'join', 'joining', 'helping', 'at', 'so', 'yet', 'or', 'nor', 'here', 'there', 'where', 'why', 'how', 'what', 'every', 'each', 'any', 'all'}


_EMPLOYER_CUE = re.compile(r'\b(?:at|with|for|joined|join|joining|employed by|worked for)\s+$', re.I)
_CRED_WORDS = re.compile(r'\b(?:certified|certification|certificate|licensed|license|accredited)\b', re.I)
_CRED_ACRONYMS = {'PMP', 'CAPM', 'PMI', 'CSM', 'PSM', 'CSPO', 'CISSP', 'CISM', 'CISA', 'CEH', 'OSCP', 'GSEC', 'CCNA',
                  'CCNP', 'CCIE', 'ITIL', 'CPA', 'CFA', 'CMA', 'PE', 'EIT', 'SHRM', 'PHR', 'SPHR', 'LSSGB', 'LSSBB',
                  'CSSGB', 'CSSBB', 'PGMP', 'PFMP', 'SAFE', 'CKA', 'CKAD', 'RHCE', 'RHCSA', 'MCSE', 'CDMP', 'CIPP', 'CIPM'}
_OK_ACRONYMS = {'US', 'USA', 'CEO', 'CTO', 'COO', 'CFO', 'CIO', 'CISO', 'VP', 'HR', 'IT', 'API', 'APIS', 'KPI', 'KPIS',
                'OKR', 'OKRS', 'ROI', 'SLA', 'SLAS', 'B2B', 'B2C', 'UX', 'UI', 'QA', 'PM', 'PMS', 'TPM', 'CX', 'CS',
                'AI', 'ML', 'LLM', 'LLMS', 'SQL', 'ETL', 'SAAS', 'GTM', 'RFP', 'RFPS', 'SOP', 'SOPS', 'FAQ', 'EOD', 'QBR', 'QBRS', 'NPS', 'CSAT', 'ARR', 'MRR', 'SMB', 'CRM', 'PRD', 'PRDS', 'RACI', 'SME', 'SMES', 'RFI', 'PDF',
                'SDK', 'SDKS', 'ERP', 'IAM', 'SSO', 'MFA', 'NIST', 'GDPR', 'HIPAA', 'SOX', 'PMO', 'SDLC', 'CI', 'CD', 'AWS', 'GCP',
                'IOT', 'MCP', 'RAG', 'NLP', 'GPU', 'GPUS', 'CPU', 'SQL', 'BI', 'HR', 'EHR', 'EMR', 'GTM', 'ROI', 'TCO', 'SKU', 'SKUS',
                'MVP', 'POC', 'POCS', 'UAT', 'SOW', 'SOWS', 'NDA', 'NDAS', 'OEM', 'B2G', 'DEI', 'ESG', 'OSHA', 'JIT', 'HVAC'}


def _words(s):
    return set(re.findall(r"[a-z0-9][a-z0-9+#&'-]*", s.casefold()))


_NEGATION = re.compile(r"\b(?:not|never|no|without|n't)\b|n't\b", re.I)
_CLAIM_VERB = re.compile(r"\b(?:hold|holds|holding|held|have|has|earned|obtained|passed|am|certified|credentialed|licensed|as an?)\b", re.I)


def _is_credential_claim(text, tok, allowed_words):
    """A credential acronym is a claim unless its sentence is negated, or the posting
    names it and the sentence makes no possession claim (e.g. 'CPA firm practice')."""
    claim = False
    for sent in re.split(r'(?<=[.!?])\s+|\n+', text):
        if not re.search(r'\b' + re.escape(tok) + r'\b', sent):
            continue
        if _NEGATION.search(sent):
            continue
        if tok.casefold() in allowed_words and not _CLAIM_VERB.search(sent):
            continue
        claim = True
    return claim


def grounding_problems(text, resume_text, never_claim=(), profile=None, job=None):
    """Return a list of (kind, token) reasons a draft is not grounded; empty means OK.

    Conservative lexical heuristic; it cannot establish factual truth.
    - Generic capitalized phrases may come from the resume or the posting (word by word).
    - Employer slots ("at/with/for/joined X") must match the resume or this job's company/title.
    - Credentials (certification wording, credential acronyms) must come from the resume only.
    - Numbers, years, and metrics must appear in the resume or posting.
    """
    if profile is not None:
        resume_text = grounded_resume_text(profile)
        for k in ('phone', 'email', 'name', 'location'):
            if profile.get(k):
                text = text.replace(str(profile[k]), ' ')
    text = re.sub(r'(?im)^\s*(dear|sincerely|best regards|regards|re:)\b.*$', ' ', text)
    if re.search(r'Scrum Fundamentals Certified', resume_text, re.I):
        # The resume's "Scrum Fundamentals Certified (SFC)" is the same credential the drafter names as a Certificate.
        resume_text += '\nScrum Fundamentals Certificate (SFC)'
    resume_only = re.sub(r'\s+', ' ', resume_text).casefold()
    job_text = ''
    employer_ok = resume_only
    if job:
        job_text = ' '.join(str(job.get(k) or '') for k in ('company', 'title', 'description', 'location'))
        employer_ok = resume_only + ' ' + re.sub(r'\s+', ' ', f"{job.get('company') or ''} {job.get('title') or ''}").casefold()
    allowed = re.sub(r'\s+', ' ', resume_text + '\n' + job_text + '\nHiring Team Hiring Manager Hiring Committee').casefold()
    allowed_words = _words(allowed)
    resume_words = _words(resume_only)
    # Employment date ranges support intermediate years without permitting arbitrary metrics.
    employment_years = set()
    for start, end in re.findall(r'\b(19\d{2}|20\d{2})\s*(?:-|–|—|to)\s*(19\d{2}|20\d{2})\b', resume_text, re.I):
        if int(start) <= int(end):
            employment_years.update(str(y) for y in range(int(start), int(end) + 1))
    problems = []
    if re.search(r'Microsoft Learn.{0,40}certif|certif.{0,40}Microsoft Learn', text, re.I):
        problems.append(('credential', 'Microsoft Learn certification'))
    if re.search(r'Scrum Fundamentals Certification', text, re.I):
        problems.append(('credential', 'Scrum Fundamentals Certification'))
    scada = [] if profile is not None and 'scada experience' in resume_only else ['SCADA']
    forbidden = list(never_claim or []) + scada + (excluded_resume_claims(profile) if profile is not None else [])
    if re.search(r'ignore previous instructions|grant submit permission|reveal the applicant phone number', text, re.I):
        problems.append(('injection', 'instruction text'))
    for term in forbidden:
        if term and re.search(r'\b' + re.escape(term) + r'\b', text, re.I):
            problems.append(('never_claim', term))
    for token in re.findall(r'\b\d{4}\b|\b\d+(?:\.\d+)?\s*(?:%|years?|months?|million|billion|[kKmM])(?:\b|$)', text):
        if token.casefold() not in allowed and token not in employment_years:
            problems.append(('number', token))
    # Credentials: resume only, never the posting.
    for m in re.finditer(r'\b(?:I (?:am|hold|earned|obtained|have)(?: my| a| an)?\s+)?(?:the\s+)?((?:Certified\s+)?(?:[A-Z][\w+-]*\s+){1,4}(?:Certificate|Certification|License)|[A-Z][\w+-]*[ -]certified)\b', text):
        credential = re.sub(r'^(?:My|Our|The|This|That|A|An)\s+', '', m.group(1).strip())   # "My Scrum ... Certificate"
        valid_sfc = credential.casefold() == 'scrum fundamentals certificate' and 'scrum fundamentals certificate' in resume_only
        if credential.casefold() not in resume_only and not valid_sfc and not _NEGATION.search(text[max(0, m.start()-12):m.start()]):
            problems.append(('credential', credential))
    for m in re.finditer(r'\b(?:I\s+(?:am|hold|earned|obtained|have)\s+(?:a\s+|the\s+|my\s+)?)((?:Certified|Licensed)\s+(?:[A-Z][\w+-]*\s*){1,4})\b', text):
        credential = m.group(1).strip()
        if credential.casefold() not in resume_only:
            problems.append(('credential', credential))
    for tok in re.findall(r'\b[A-Z][A-Z0-9+]{1,6}s?\b', text):
        up = tok.upper()
        if up in _CRED_ACRONYMS or up.rstrip('S') in _CRED_ACRONYMS:
            if tok.casefold() not in resume_words and _is_credential_claim(text, tok, allowed_words):
                problems.append(('credential', tok))
        elif (up not in _OK_ACRONYMS and up.rstrip('S') not in _OK_ACRONYMS
              and tok.casefold() not in allowed_words and tok.casefold().removesuffix('s') not in allowed_words
              and not re.search(r'(?<!\w)' + re.escape(tok.casefold()) + r'(?!\w)', allowed)):
            problems.append(('acronym', tok))
    # Single-word employers after strong cues ("joined X", "worked at X").
    for m in re.finditer(r'\b(?:joined|worked (?:at|for)|employed (?:at|by)|at)\s+([A-Z][\w&.-]*\w)(?![ \t]+[A-Z])', text):
        if m.group(1).casefold() not in employer_ok and m.group(1).casefold() not in _LEADERS:
            problems.append(('employer', m.group(1)))
    # Capitalized multiword phrases: employer slots strict, everything else word-level.
    for m in re.finditer(r'\b[A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+)+\b', text):
        words = m.group(0).split()
        while words and words[0].casefold() in _LEADERS:
            words.pop(0)
        if not words:
            continue
        phrase = ' '.join(words).casefold()
        if phrase in allowed:
            continue
        if _EMPLOYER_CUE.search(text[:m.start()]) and len(m.group(0).split()) == len(words):
            if phrase not in employer_ok:
                problems.append(('employer', ' '.join(words)))
            continue
        missing = [w for w in words if w.casefold() not in allowed_words]
        if missing:
            problems.append(('name', ' '.join(words)))
    return problems


def check_grounding(text, resume_text, never_claim=(), profile=None, job=None):
    return not grounding_problems(text, resume_text, never_claim, profile=profile, job=job)


def format_problems(problems, limit=4):
    return '; '.join(f'{k} {v!r}' for k, v in problems[:limit])
