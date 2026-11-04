"""File job-application email out of the Proton inbox into Folders/Job Applications (via Bridge).
Usage: mail_sort.py [--dry] [--days N] [--reject-backfill DAYS]
Moves job mail; the only thing it trashes is Greenhouse "Security code for your application" mail older than an hour."""
import email, email.header, email.utils, imaplib, json, os, re, ssl, sys, datetime
from pathlib import Path

FOLDER = "Folders/Job Applications"
STATE = Path(__file__).with_name("mail_sort_state.json")
ATS = re.compile(r"(greenhouse|lever\.co|ashbyhq|myworkday|workday|icims|smartrecruiters|taleo|successfactors|jobvite|"
                 r"gem\.com|bamboohr|breezy|workable|recruitee|teamtailor|pinpoint|jazzhr|rippling|dover\.com|oraclecloud|"
                 r"applytojob|hire\.|careers|recruiting|talent|candidate|no-?reply.*jobs|adzuna|usajobs)", re.I)
SUBJ = re.compile(r"(thank(s| you) for (applying|your (application|interest))|application (received|submitted|confirmation|"
                  r"update|status)|your application|we received your application|candidate (home|account)|"
                  r"verify your (candidate|email)|next steps|interview|unfortunately|position|role at|job alert|"
                  r"applying to|application for|update from)", re.I)
NOT = re.compile(r"(proton|bank|invoice|receipt|statement|github|google|linkedin news|newsletter|community)", re.I)


def keys():
    env = {}
    for line in Path("~/.config/remote_apply/keys.env").expanduser().read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"\'')
    return env


def dec(v):
    return "".join(p.decode(c or "utf-8", "replace") if isinstance(p, bytes) else p for p, c in email.header.decode_header(v or ""))


def is_job(frm, subj):
    if re.search(r"community|newsletter|webinar", subj, re.I):
        return False
    if NOT.search(frm) and not ATS.search(frm):
        return False
    if re.search(r"no-?reply|recruit|talent|careers|jobs", frm, re.I) and \
            re.search(r"(thanks? (you )?for (applying|your interest)|application for|regarding (your )?application|your application|"
                      r"your .{0,60} application|update (on|regarding) your)", subj, re.I):
        return True
    if re.search(r"^(re:\s*)?(thanks?( you)? for (applying|your application)|thanks?( you)? for your interest in .{2,60}( - | role| position)|"
                 r"(an )?update on your .{0,40}application|your application (to|for|with|at) )", subj, re.I):
        return True  # recruiters writing from their own address
    return bool(ATS.search(frm) and SUBJ.search(subj)) or bool(re.search(r"(myworkday|greenhouse|lever\.co|ashbyhq|icims|smartrecruiters)", frm, re.I))


REJECT = re.compile(r"(not (to )?move forward|not moving forward with|(won\S{1,8}t|will not|not) be moving forward|"
                    r"decided to (pursue|move forward with|proceed with) (other|another)|move forward with (other|another) candidate|"
                    r"proceed with other candidates|no longer (being )?consider|not been selected|other candidates whose|"
                    r"going in a direction that better fits|regret to inform)", re.I)


def company_key(value):
    words = re.findall(r"[a-z0-9]+", value.lower())
    return " ".join(w for w in words if w not in {"inc", "llc", "labs", "corp"})


def log_rejections(m, mailbox, hits):
    """Mark applied jobs as rejected when a rejection email names the company (and title if possible)."""
    import sqlite3
    m.select(mailbox, readonly=True)
    conn = sqlite3.connect(Path(__file__).with_name("applications.sqlite3"), timeout=60)
    # include already-rejected jobs so a re-read email matches its own job, not a sibling at the same company
    jobs = conn.execute("SELECT url, company, title FROM jobs WHERE status IN ('applied','unconfirmed','rejected')").fetchall()
    n = 0
    for uid, frm, subj in hits:
        _, resp = m.uid("FETCH", uid, "(BODY.PEEK[TEXT])")
        body = b"".join(p[1] for p in resp if isinstance(p, tuple)).decode("utf-8", "replace")
        body = re.sub(r"=\r?\n", "", body)
        text = f"{frm} {subj} {re.sub(r'<[^>]+>', ' ', body)}"
        if not REJECT.search(text):
            continue
        low = f" {company_key(text)} "
        sender = f" {company_key(email.utils.parseaddr(frm)[1].split('@')[-1])} "
        cands = [j for j in jobs if j[1] and (f" {company_key(j[1])} " in low or
                 f" {company_key(j[1])} " in sender)]
        exact = [j for j in cands if j[2] and f" {company_key(j[2])} " in low]
        matched = exact if exact else cands
        if len(matched) == 1:
            url, co, title = matched[0]
            if conn.execute("UPDATE jobs SET status='rejected', updated=CURRENT_TIMESTAMP WHERE url=? AND status IN ('applied','unconfirmed')", (url,)).rowcount:
                print(f"rejected: {co} - {title}"); n += 1
    conn.commit(); conn.close()
    print(f"rejections logged {n}")
    return n


CONFIRM = re.compile(r"(thanks? (you )?for (applying|your application|submitting)|application (has been |was )?(received|submitted)|"
                     r"(we('ve| have)|have) received your application|received your application|successfully (applied|submitted))", re.I)


def log_confirmations(m, mailbox, hits):
    """Promote 'unconfirmed' jobs to 'applied' when an ATS receipt names exactly one of them.

    Same matching as log_rejections: company must appear in the text or sender domain, the title breaks
    ties, and anything other than a single match is ignored (Samsara/PointClickCare have siblings).
    Applied jobs are included as candidates so a receipt for one never lands on its sibling."""
    import sqlite3, state
    m.select(mailbox, readonly=True)
    conn = sqlite3.connect(Path(__file__).with_name("applications.sqlite3"), timeout=60)
    jobs = conn.execute("SELECT url, company, title FROM jobs WHERE status IN ('applied','unconfirmed')").fetchall()
    n = 0
    for uid, frm, subj in hits:
        _, resp = m.uid("FETCH", uid, "(BODY.PEEK[TEXT])")
        body = re.sub(r"=\r?\n", "", b"".join(p[1] for p in resp if isinstance(p, tuple)).decode("utf-8", "replace"))
        text = f"{frm} {subj} {re.sub(r'<[^>]+>', ' ', body)}"
        if not CONFIRM.search(text) or REJECT.search(text):
            continue
        low = f" {company_key(text)} "
        sender = f" {company_key(email.utils.parseaddr(frm)[1].split('@')[-1])} "
        cands = [j for j in jobs if j[1] and (f" {company_key(j[1])} " in low or f" {company_key(j[1])} " in sender)]
        exact = [j for j in cands if j[2] and f" {company_key(j[2])} " in low]
        matched = exact if exact else cands
        if len(matched) != 1:
            continue
        url, co, title = matched[0]
        if conn.execute("UPDATE jobs SET status='applied', applied_at=coalesce(applied_at, date('now','localtime')), "
                        "updated=CURRENT_TIMESTAMP WHERE url=? AND status='unconfirmed'", (url,)).rowcount:
            att = conn.execute("SELECT id FROM attempts WHERE job_url=? AND state='uncertain' ORDER BY id DESC LIMIT 1", (url,)).fetchone()
            if att:
                state.reconcile(conn, att[0], True)  # schedules the follow-up too
            print(f"confirmed by email: {co} - {title}"); n += 1
    conn.commit(); conn.close()
    print(f"confirmations logged {n}")
    return n


def confirm_backfill(m, days):
    """One-off: scan the whole Job Applications folder for receipts (does not touch the rejection cursor)."""
    m.select(f'"{FOLDER}"', readonly=True)
    since = (datetime.date.today() - datetime.timedelta(days=days)).strftime("%d-%b-%Y")
    _, data = m.uid("SEARCH", None, f"SINCE {since}")
    hits = []
    for uid in data[0].split():
        _, resp = m.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
        part = next((p for p in resp if isinstance(p, tuple)), None)
        if part:
            msg = email.message_from_bytes(part[1])
            hits.append((uid, dec(msg["From"]), dec(msg["Subject"])))
    return log_confirmations(m, f'"{FOLDER}"', hits)


def reject_backfill(m, days):
    """One-off: re-check the whole Job Applications folder for rejections (does not touch the cursor)."""
    m.select(f'"{FOLDER}"', readonly=True)
    since = (datetime.date.today() - datetime.timedelta(days=days)).strftime("%d-%b-%Y")
    _, data = m.uid("SEARCH", None, f"SINCE {since}")
    hits = []
    for uid in data[0].split():
        _, resp = m.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
        part = next((p for p in resp if isinstance(p, tuple)), None)
        if part:
            msg = email.message_from_bytes(part[1])
            hits.append((uid, dec(msg["From"]), dec(msg["Subject"])))
    return log_rejections(m, f'"{FOLDER}"', hits)


def purge_codes(m, dry=False, keep_hours=1):
    """Trash Greenhouse 'Security code for your application' mail older than keep_hours (seccode.py reads them within seconds)."""
    n = 0
    now = datetime.datetime.now(datetime.timezone.utc)
    for box in ("INBOX", f'"{FOLDER}"'):
        m.select(box)
        _, d = m.uid("SEARCH", None, 'FROM "greenhouse" SUBJECT "Security code for your application"')
        uids, old = d[0].split(), []
        for i in range(0, len(uids), 200):
            _, resp = m.uid("FETCH", b",".join(uids[i:i + 200]), "(INTERNALDATE)")
            for line in resp:
                line = line if isinstance(line, bytes) else line[0]
                u, dd = re.search(rb"UID (\d+)", line), re.search(rb'INTERNALDATE "([^"]+)"', line)
                if u and dd and now - datetime.datetime.strptime(dd.group(1).decode().strip(), "%d-%b-%Y %H:%M:%S %z") > datetime.timedelta(hours=keep_hours):
                    old.append(u.group(1))
        if old and not dry:
            for i in range(0, len(old), 100):
                s = b",".join(old[i:i + 100]).decode()
                typ, _ = m.uid("MOVE", s, "Trash")
                if typ != "OK":
                    m.uid("COPY", s, "Trash"); m.uid("STORE", s, "+FLAGS", r"(\Deleted)"); m.expunge()
        n += len(old)
    print(f"security codes {'to trash' if dry else 'trashed'} {n}")
    return n


def folder_rejections(m, days):
    m.select(f'"{FOLDER}"', readonly=True)
    validity = (m.response("UIDVALIDITY")[1] or [b""])[0].decode()
    try:
        state = json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        state = {}
    last = int(state.get("last_uid", 0)) if state.get("uidvalidity") == validity else 0
    since = (datetime.date.today() - datetime.timedelta(days=days)).strftime("%d-%b-%Y")
    _, data = m.uid("SEARCH", None, f"SINCE {since}")
    uids = [uid for uid in data[0].split() if int(uid) > last]
    hits = []
    for i in range(0, len(uids), 200):
        _, resp = m.uid("FETCH", b",".join(uids[i:i + 200]), "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
        pending = None  # Bridge may put "UID n" before or after the literal
        for part in resp:
            head = part[0] if isinstance(part, tuple) else part
            uid = re.search(rb"UID (\d+)", head or b"")
            if isinstance(part, tuple):
                pending = email.message_from_bytes(part[1])
            if uid and pending is not None:
                hits.append((uid.group(1), dec(pending["From"]), dec(pending["Subject"])))
                pending = None
    logged = log_rejections(m, f'"{FOLDER}"', hits)
    log_confirmations(m, f'"{FOLDER}"', hits)
    if uids:
        STATE.write_text(json.dumps({"uidvalidity": validity, "last_uid": int(uids[-1])}) + "\n")
    print(f"folder scanned {len(hits)} | rejections logged {logged}")


def main():
    dry = "--dry" in sys.argv
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 3650
    env = keys()
    m = imaplib.IMAP4("127.0.0.1", 1143)
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    m.starttls(ctx)
    m.login(env["BRIDGE_USER"], env["BRIDGE_PASS"])
    if not dry:
        m.create(f'"{FOLDER}"')  # harmless if it exists
    purge_codes(m, dry)
    if "--reject-backfill" in sys.argv:
        reject_backfill(m, int(sys.argv[sys.argv.index("--reject-backfill") + 1]))
    m.select("INBOX")
    since = (datetime.date.today() - datetime.timedelta(days=days)).strftime("%d-%b-%Y")
    _, data = m.uid("SEARCH", None, f"SINCE {since}")
    uids = data[0].split()
    hits = []
    for i in range(0, len(uids), 200):
        chunk = b",".join(uids[i:i + 200])
        _, resp = m.uid("FETCH", chunk, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
        pending = None
        for part in resp:
            head = part[0] if isinstance(part, tuple) else part
            u = re.search(rb"UID (\d+)", head or b"")
            if isinstance(part, tuple):
                pending = email.message_from_bytes(part[1])
            if u and pending is not None:
                frm, subj = dec(pending["From"]), dec(pending["Subject"])
                if is_job(frm, subj):
                    hits.append((u.group(1), frm, subj))
                pending = None
    # The bot reads security codes / verify links from INBOX: leave those there for an hour.
    all_hits, keep = hits, set()
    codes = [h for h in hits if re.search(r"security code|verif|one[- ]time|passcode|confirm your email|reset your password", h[2], re.I)]
    for i in range(0, len(codes), 200):
        _, resp = m.uid("FETCH", b",".join(h[0] for h in codes[i:i + 200]), "(INTERNALDATE)")
        for line in resp:
            line = line if isinstance(line, bytes) else line[0]
            u, d = re.search(rb"UID (\d+)", line), re.search(rb'INTERNALDATE "([^"]+)"', line)
            if u and d:
                when = email.utils.parsedate_to_datetime(datetime.datetime.strptime(d.group(1).decode(), "%d-%b-%Y %H:%M:%S %z").strftime("%a, %d %b %Y %H:%M:%S %z"))
                if datetime.datetime.now(datetime.timezone.utc) - when < datetime.timedelta(hours=1):
                    keep.add(u.group(1))
    hits = [h for h in hits if h[0] not in keep]
    print(f"inbox {len(uids)} | job mail {len(hits)}")
    for uid, frm, subj in hits[:400 if dry else 15]:
        print(f"  {email.utils.parseaddr(frm)[1][:40]:40} | {subj[:80]}")
    if "--confirm-backfill" in sys.argv:
        confirm_backfill(m, int(sys.argv[sys.argv.index("--confirm-backfill") + 1]))
        m.logout(); return
    if not dry:
        log_rejections(m, "INBOX", all_hits)
        log_confirmations(m, "INBOX", all_hits)
        m.select("INBOX")
    if not dry and hits:
        for i in range(0, len(hits), 100):
            s = b",".join(h[0] for h in hits[i:i + 100]).decode()
            typ, _ = m.uid("MOVE", s, f'"{FOLDER}"')
            if typ != "OK":
                m.uid("COPY", s, f'"{FOLDER}"'); m.uid("STORE", s, "+FLAGS", r"(\Deleted)"); m.expunge()
        print(f"moved {len(hits)} -> {FOLDER}")
    if not dry:
        folder_rejections(m, days if "--days" in sys.argv else 14)
    m.logout()


if __name__ == "__main__":
    main()
