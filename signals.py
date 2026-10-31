"""Company and posting signals: WARN layoffs, PERM labor-certification ads, ghost-job score, hiring surges.

Sources
- WARN: state Worker Adjustment and Retraining Notification filings via biglocalnews/warn-scraper (data/warn/*.csv)
- PERM: DOL OFLC PERM disclosure data (data/perm_*.xlsx). Employers sponsoring a green card must first advertise
  the job (incl. on their own website) to show no qualified US worker is available. A posting that matches a
  company's recurring PERM titles is often a compliance ad, not an open search.
"""
import csv, datetime as dt, glob, json, re, sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SUFFIX = re.compile(r"\b(incorporated|inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|plc|gmbh|ag|sa|se|bv|n\.?a|lp|llp|"
                    r"holdings?|group|technologies|technology|labs?|usa|us|america|the)\b\.?", re.I)


def ekey(name):
    s = SUFFIX.sub(" ", (name or "").lower())
    return re.sub(r"[^a-z0-9]", "", s)


def tables(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS perm_cases (ekey TEXT, employer TEXT, title TEXT, received TEXT, status TEXT,
        web_from TEXT, web_to TEXT, state TEXT, wage_from REAL, wage_to REAL);
    CREATE INDEX IF NOT EXISTS perm_ekey ON perm_cases(ekey);
    CREATE TABLE IF NOT EXISTS warn_notices (ekey TEXT, employer TEXT, state TEXT, notice_date TEXT, employees INTEGER);
    CREATE INDEX IF NOT EXISTS warn_ekey ON warn_notices(ekey);
    CREATE TABLE IF NOT EXISTS company_counts (day TEXT, company TEXT, total INTEGER, relevant INTEGER, PRIMARY KEY(day, company));
    CREATE TABLE IF NOT EXISTS company_signals (company TEXT PRIMARY KEY, perm_cases INTEGER, perm_recent INTEGER,
        warn_count INTEGER, warn_employees INTEGER, warn_last TEXT, surge REAL, surge_note TEXT, updated TEXT);
    CREATE TABLE IF NOT EXISTS meta_files (path TEXT PRIMARY KEY, mtime REAL);
    """)


def _changed(conn, path):
    m = Path(path).stat().st_mtime
    row = conn.execute("SELECT mtime FROM meta_files WHERE path=?", (str(path),)).fetchone()
    return not row or row[0] != m, m


def _d(v):
    if v is None:
        return ""
    if isinstance(v, (dt.date, dt.datetime)):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if m:
        y = int(m.group(3)); y += 2000 if y < 100 else 0
        try:
            return dt.date(y, int(m.group(1)), int(m.group(2))).isoformat()
        except Exception:
            return ""
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d-%b-%y", "%d-%b-%Y"):
        try:
            return dt.datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return ""


# ------------------------------------------------------------- PERM
def load_perm(conn, log):
    import openpyxl
    files = sorted(DATA.glob("perm_*.xlsx"))
    status = [_changed(conn, f) for f in files]
    if not any(c for c, _ in status):
        return
    conn.execute("DELETE FROM perm_cases")
    need = ["EMP_BUSINESS_NAME", "JOB_TITLE", "RECEIVED_DATE", "CASE_STATUS", "RECR_OCC_EMP_WEBSITE_FROM",
            "RECR_OCC_EMP_WEBSITE_TO", "PRIMARY_WORKSITE_STATE", "JOB_OPP_WAGE_FROM", "JOB_OPP_WAGE_TO"]
    alt = {"EMP_BUSINESS_NAME": "EMPLOYER_NAME", "PRIMARY_WORKSITE_STATE": "WORKSITE_STATE"}

    def num(v):
        try:
            return float(v)
        except Exception:
            return None
    for f, (_, m) in zip(files, status):
        log(f"PERM: loading {f.name} (large file, a few minutes)")
        ws = openpyxl.load_workbook(f, read_only=True).worksheets[0]
        rows = ws.iter_rows(values_only=True)
        ix = {str(h or ""): i for i, h in enumerate(next(rows))}
        cols = [ix.get(n, ix.get(alt.get(n, ""), None)) for n in need]
        batch, n = [], 0
        for r in rows:
            g = lambda k: r[cols[k]] if cols[k] is not None and cols[k] < len(r) else None
            if not g(0):
                continue
            batch.append((ekey(g(0)), str(g(0)), str(g(1) or ""), _d(g(2)), str(g(3) or ""), _d(g(4)), _d(g(5)),
                          str(g(6) or ""), num(g(7)), num(g(8))))
            if len(batch) >= 20000:
                conn.executemany("INSERT INTO perm_cases VALUES (?,?,?,?,?,?,?,?,?,?)", batch); n += len(batch); batch = []
        conn.executemany("INSERT INTO perm_cases VALUES (?,?,?,?,?,?,?,?,?,?)", batch); n += len(batch)
        conn.execute("INSERT OR REPLACE INTO meta_files VALUES (?,?)", (str(f), m))
        conn.commit()
        log(f"PERM: {n} cases from {f.name}")


# ------------------------------------------------------------- WARN
def _pick(hdr, *pats):
    for p in pats:
        for i, h in enumerate(hdr):
            if re.search(p, h, re.I):
                return i
    return None


def load_warn(conn, log):
    files = sorted(glob.glob(str(DATA / "warn" / "*.csv")))
    if not files:
        return
    sig = max(Path(f).stat().st_mtime for f in files)
    row = conn.execute("SELECT mtime FROM meta_files WHERE path='warn'").fetchone()
    if row and row[0] == sig:
        return
    conn.execute("DELETE FROM warn_notices")
    n = 0
    for f in files:
        st = Path(f).stem.upper()[:2]
        try:
            with open(f, newline="", encoding="utf-8", errors="ignore") as fh:
                rd = csv.reader(fh)
                hdr = next(rd, [])
                ce = _pick(hdr, r"company|employer|business|organization|name")
                cd = _pick(hdr, r"notice.*date|received|date.*notice|warn.*date|date")
                cn = _pick(hdr, r"employees|affected|number|workers|layoff.*total|total")
                if ce is None:
                    continue
                batch = []
                for r in rd:
                    if ce >= len(r) or not r[ce].strip():
                        continue
                    cnt = None
                    if cn is not None and cn < len(r):
                        m = re.search(r"\d[\d,]*", r[cn])
                        cnt = int(m.group(0).replace(",", "")) if m else None
                    batch.append((ekey(r[ce]), r[ce].strip(), st, _d(r[cd]) if cd is not None and cd < len(r) else "", cnt))
                conn.executemany("INSERT INTO warn_notices VALUES (?,?,?,?,?)", batch)
                n += len(batch)
        except Exception as e:
            log(f"WARN: skip {Path(f).name}: {e}")
    conn.execute("INSERT OR REPLACE INTO meta_files VALUES ('warn', ?)", (sig,))
    conn.commit()
    log(f"WARN: {n} notices from {len(files)} states")


# ------------------------------------------------------------- per-company + per-job signals
def _employer_keys(conn, table, company):
    k = ekey(company)
    if len(k) < 3:
        return []
    keys = [r[0] for r in conn.execute(f"SELECT DISTINCT ekey FROM {table} WHERE ekey=? OR ekey LIKE ?", (k, k + "%"))]
    # avoid prefix collisions like 'toast' -> 'toastmasters' : keep exact or short extensions only
    return [x for x in keys if x == k or len(x) - len(k) <= 6]


def company_signals(conn, log):
    today = dt.date.today()
    y2 = (today - dt.timedelta(days=1200)).isoformat()
    y1 = (today - dt.timedelta(days=730)).isoformat()
    w180 = (today - dt.timedelta(days=180)).isoformat()
    for (company,) in conn.execute("SELECT DISTINCT company FROM jobs").fetchall():
        pk = _employer_keys(conn, "perm_cases", company)
        wk = _employer_keys(conn, "warn_notices", company)
        q = lambda sql, keys, *a: conn.execute(sql.format(",".join("?" * len(keys))), (*keys, *a)).fetchone() if keys else (0, 0, None)
        pc = q("SELECT count(*), 0, 0 FROM perm_cases WHERE ekey IN ({}) AND received>=?", pk, y2)[0]
        pr = q("SELECT count(*), 0, 0 FROM perm_cases WHERE ekey IN ({}) AND received>=?", pk, y1)[0]
        wc, we, wl = q("SELECT count(*), coalesce(sum(employees),0), max(notice_date) FROM warn_notices WHERE ekey IN ({}) AND notice_date>=?", wk, w180)
        conn.execute("INSERT INTO company_signals(company, perm_cases, perm_recent, warn_count, warn_employees, warn_last, updated) "
                     "VALUES (?,?,?,?,?,?,date('now')) ON CONFLICT(company) DO UPDATE SET perm_cases=excluded.perm_cases, "
                     "perm_recent=excluded.perm_recent, warn_count=excluded.warn_count, warn_employees=excluded.warn_employees, "
                     "warn_last=excluded.warn_last, updated=excluded.updated", (company, pc, pr, wc, we, wl))
    conn.commit()


PERM_TEXT = re.compile(r"\bperm\b|labor certification|mail (your )?resume|reference (job )?(code|number)|employer will accept|"
                       r"apply by mail|send resume to|in lieu of|any suitable combination of education", re.I)


def ghost_scores(conn, log):
    """0 (real, active search) .. 100 (likely ghost / compliance / evergreen)."""
    from rapidfuzz import fuzz
    today = dt.date.today()
    y2 = (today - dt.timedelta(days=1200)).isoformat()
    perm_titles = {}
    jobs = conn.execute("SELECT rowid, url, company, title, description, posted_at, found_at, salary FROM jobs "
                        "WHERE status NOT IN ('skip','closed')").fetchall()
    sig = {r[0]: r for r in conn.execute("SELECT company, perm_cases, warn_count, warn_employees, warn_last FROM company_signals")}
    for rid, url, company, title, desc, posted, found, salary in jobs:
        if company not in perm_titles:
            keys = _employer_keys(conn, "perm_cases", company)
            perm_titles[company] = [r[0] for r in conn.execute(
                f"SELECT title FROM perm_cases WHERE ekey IN ({','.join('?' * len(keys))}) AND received>=?", (*keys, y2))] if keys else []
        score, why = 0, []
        start = posted or found or ""
        try:
            age = (today - dt.date.fromisoformat(start[:10])).days
        except Exception:
            age = 0
        if age > 120:
            score += 35; why.append(f"open {age} days")
        elif age > 60:
            score += 20; why.append(f"open {age} days")
        elif 0 <= age <= 14:
            score -= 10
        hits = sum(1 for t in perm_titles[company] if fuzz.token_set_ratio(t.lower(), title.lower()) >= 88)
        if hits >= 3:
            score += 45; why.append(f"{hits} PERM filings for this title")
        elif hits:
            score += 30; why.append(f"{hits} PERM filing(s) for this title")
        if PERM_TEXT.search(desc or ""):
            score += 15; why.append("PERM-style ad wording")
        dupes = conn.execute("SELECT count(*) FROM jobs WHERE company=? AND title=? AND url<>?", (company, title, url)).fetchone()[0]
        if dupes >= 2:
            score += 10; why.append(f"{dupes + 1} identical postings (evergreen req)")
        s = sig.get(company)
        if s and s[2]:
            score += 15; why.append(f"WARN layoffs: {s[2]} notice(s), {s[3]} workers since {s[4] and s[4][:7]}")
        if salary:
            score -= 5
        conn.execute("UPDATE jobs SET ghost=?, ghost_why=? WHERE rowid=?", (max(0, min(100, score)), "; ".join(why), rid))
    conn.commit()


def record_counts(conn):
    day = dt.date.today().isoformat()
    for company, total, rel in conn.execute(
            "SELECT company, count(*), sum(CASE WHEN fit>=66 THEN 1 ELSE 0 END) FROM jobs WHERE status NOT IN ('closed') GROUP BY company").fetchall():
        conn.execute("INSERT OR REPLACE INTO company_counts VALUES (?,?,?,?)", (day, company, total, rel or 0))
    conn.commit()


def surges(conn, log):
    """Change-point detection on each company's daily open-role count (ruptures). Needs ~2 weeks of history."""
    import numpy as np, ruptures as rpt
    for (company,) in conn.execute("SELECT DISTINCT company FROM company_counts").fetchall():
        rows = conn.execute("SELECT day, total FROM company_counts WHERE company=? ORDER BY day", (company,)).fetchall()
        surge, note = 0.0, ""
        if len(rows) >= 10:
            y = np.array([r[1] for r in rows], dtype=float)
            try:
                bkps = rpt.Pelt(model="l2", min_size=3).fit(y).predict(pen=max(4.0, y.var() * 2))
            except Exception:
                bkps = [len(y)]
            if len(bkps) >= 2:
                cp = bkps[-2]
                before, after = y[:cp].mean(), y[cp:].mean()
                if len(y) - cp <= 21 and before > 0 and after / before >= 1.3:
                    surge = round(after / before, 2)
                    note = f"openings up {int((surge - 1) * 100)}% since {rows[cp][0]}"
        elif len(rows) >= 2 and rows[0][1] > 0:
            ratio = rows[-1][1] / rows[0][1]
            if ratio >= 1.3 and rows[-1][1] - rows[0][1] >= 3:
                surge, note = round(ratio, 2), f"openings up {int((ratio - 1) * 100)}% since {rows[0][0]}"
        conn.execute("UPDATE company_signals SET surge=?, surge_note=? WHERE company=?", (surge, note, company))
    conn.commit()


def refresh(conn, log):
    tables(conn)
    load_perm(conn, log)
    load_warn(conn, log)
    company_signals(conn, log)
    record_counts(conn)
    surges(conn, log)
    ghost_scores(conn, log)
