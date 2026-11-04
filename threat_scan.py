"""Scan job-related email for recruitment scams and record what was found (monitor only; nothing is moved or deleted).

Signals, each with points (score >= 60 high, >= 30 suspicious):
  authentication   SPF / DKIM / DMARC results Proton recorded in Authentication-Results
  sender           look-alike company domains (cvshealthsinfo.com), free-mail "recruiters", Reply-To / Return-Path on another domain
  domain age       registration date from RDAP (new domains are a classic scam tell)
  infrastructure   sending IP, its reverse DNS, country and network owner
  content          off-platform chat interviews, equipment checks, payment / banking asks, offers with no interview

Usage: threat_scan.py [--days N] [--rescan]      stdlib only (mail_sort runs it under the system python)
"""
import datetime as dt
import email
import email.utils
import imaplib
import ipaddress
import json
import re
import socket
import sqlite3
import ssl
import sys
import time
import urllib.request
from pathlib import Path

import mail_sort

ROOT = Path(__file__).resolve().parent
DB = ROOT / "applications.sqlite3"
BOXES = ("INBOX", f'"{mail_sort.FOLDER}"', "Spam")
FREE_MAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "aol.com", "icloud.com",
             "me.com", "proton.me", "protonmail.com", "gmx.com", "mail.com", "zoho.com", "yandex.com", "msn.com"}
ATS_DOMAINS = ("greenhouse", "lever.co", "ashbyhq", "myworkday", "workday", "icims", "smartrecruiters", "taleo", "jobvite",
               "successfactors", "bamboohr", "workable", "recruitee", "teamtailor", "rippling", "gem.com", "paylocity",
               "insperity", "adp.com", "ultipro", "oraclecloud", "breezy", "jazzhr", "linkedin.com", "indeed.com", "adzuna")
LOOKALIKE_TAIL = re.compile(r"-?s?(info|careers?|jobs?|hr|hiring|recruit\w*|talent|team|group|usa|us|corp|inc|mail|online|"
                            r"global|portal|staff\w*|onboard\w*|employ\w*)$")
RED_FLAGS = [
    (r"\b(telegram|whats\s?app|signal app|wire app|wickr|google hangouts?|hangouts chat)\b", 20, "moves the conversation to a chat app"),
    (r"\b(text|chat|instant messag\w+)[- ](based )?interview\b|\binterview (via|over|through) (chat|text|telegram|teams chat)", 20, "chat-only interview"),
    (r"\b(purchase|buy) (your )?(own )?(work )?(equipment|laptop|computer|software) (from|through|via) (our|the|a) (approved |designated |certified )?vendor|"
     r"\b(send|mail|deposit)\w* (you )?a check\b.{0,80}\b(equipment|laptop|office)|\bequipment\b.{0,60}\b(vendor|supplier)\b.{0,60}\b(check|payment|pay)\b", 25, "equipment purchase or check"),
    (r"\b(cashier'?s? check|mobile deposit|deposit (the|a) check|zelle|cash ?app|venmo|gift ?cards?|bitcoin|crypto(currency)?|wire transfer|western union)\b", 25, "money movement / payment app"),
    (r"\b(social security number|ssn|bank (account|routing)|routing number|driver'?s licen[cs]e (photo|copy|front))\b", 20, "asks for SSN or banking details"),
    (r"\bcongratulations\b.{0,80}\b(selected|offer|hired|position)\b|(?<!if )(?<!when )\byou (have been|were) (selected|hired|shortlisted)\b(?! for (an? )?(interview|assessment|next))", 15, "offer with no interview"),
    (r"\b(no experience (needed|required)|work from home and earn|earn \$\d+ ?(per|an?) (hour|week|day)|weekly pay of \$)", 15, "too-good pay pitch"),
    (r"\b(data entry|personal assistant|package (reshipp|forward)|reshipping|payment processing agent)\b", 10, "classic scam role type"),
]
RECRUITING = re.compile(r"application|applied|interview|position|role|recruit|hiring|candidate|job|offer|opportunit|resume", re.I)


def db():
    conn = sqlite3.connect(DB, timeout=60)
    conn.execute("""CREATE TABLE IF NOT EXISTS mail_threats (
        msg_key TEXT PRIMARY KEY, mailbox TEXT, received_at TEXT, from_addr TEXT, from_domain TEXT, reply_to TEXT,
        subject TEXT, ip TEXT, ip_host TEXT, ip_country TEXT, ip_org TEXT, spf TEXT, dkim TEXT, dmarc TEXT,
        domain_age_days INTEGER, score INTEGER, level TEXT, reasons TEXT, scanned_at TEXT)""")
    conn.execute("CREATE TABLE IF NOT EXISTS threat_cache (k TEXT PRIMARY KEY, v TEXT, ts TEXT)")
    return conn


def _cached(conn, key, fetch, max_age_days=30):
    row = conn.execute("SELECT v, ts FROM threat_cache WHERE k=?", (key,)).fetchone()
    if row and row[1] >= (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=max_age_days)).isoformat():
        return json.loads(row[0])
    try:
        value = fetch()
    except Exception:
        return None  # lookup failed: try again next scan rather than caching the miss
    conn.execute("INSERT OR REPLACE INTO threat_cache VALUES (?,?,?)", (key, json.dumps(value), dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat()))
    return value


def _get_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "remote-apply-threat-scan/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def registered_domain(domain):
    parts = (domain or "").lower().strip(".").split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def domain_age_days(conn, domain):
    def fetch():
        data = _get_json(f"https://rdap.org/domain/{domain}")
        for ev in data.get("events") or []:
            if ev.get("eventAction") == "registration":
                return ev.get("eventDate", "")[:10]
        return None
    created = _cached(conn, f"rdap:{domain}", fetch, 90)
    if not created:
        return None
    try:
        return (dt.date.today() - dt.date.fromisoformat(created)).days
    except ValueError:
        return None


def ip_info(conn, ip):
    def fetch():
        try:
            host = socket.gethostbyaddr(ip)[0]
        except Exception:
            host = ""
        time.sleep(1.4)  # ip-api's free tier allows 45 lookups a minute
        geo = _get_json(f"http://ip-api.com/json/{ip}?fields=status,country,countryCode,org,as")
        return {"host": host, "country": geo.get("countryCode") or "", "org": geo.get("org") or geo.get("as") or ""}
    return _cached(conn, f"ip:{ip}", fetch, 30) or {"host": "", "country": "", "org": ""}


def auth_results(msg):
    out = {"spf": "", "dkim": "", "dmarc": ""}
    for value in msg.get_all("Authentication-Results") or []:
        for kind in out:
            m = re.search(rf"\b{kind}=(\w+)", str(value))
            if m and not out[kind]:
                out[kind] = m.group(1).lower()
    if not out["spf"]:
        m = re.search(r"^(\w+)", str(msg.get("Received-SPF") or ""))
        out["spf"] = m.group(1).lower() if m else ""
    return out


def sending_ip(msg):
    for value in msg.get_all("Authentication-Results") or []:
        m = re.search(r"smtp\.remote-ip=([0-9a-fA-F:.]+)", str(value))
        if m:
            return m.group(1)
    for value in msg.get_all("Received") or []:
        for cand in re.findall(r"\[([0-9a-fA-F:.]{7,})\]", str(value)):
            try:
                if ipaddress.ip_address(cand).is_global:
                    return cand
            except ValueError:
                continue
    return ""


def company_keys(conn):
    keys = set()
    for (company,) in conn.execute("SELECT DISTINCT company FROM jobs WHERE company != ''"):
        k = re.sub(r"[^a-z0-9]", "", company.lower())
        k = re.sub(r"(inc|llc|corp|corporation|company|group|labs|technologies|holdings)$", "", k)
        if len(k) >= 4:
            keys.add(k)
    return keys


def lookalike(domain, keys):
    """Company name plus filler in a domain that isn't the company's own (cvshealthsinfo.com, stripe-careers.net)."""
    label = re.sub(r"[^a-z0-9]", "", registered_domain(domain).split(".")[0])
    for k in keys:
        if label in keys:
            return ""  # the domain is itself a company we know (axonius.com is not axon)
        if label != k and label.startswith(k) and LOOKALIKE_TAIL.fullmatch(label[len(k):] or "x"):
            return k
        if label != k and label.endswith(k) and re.match(r"^(the|my|get|join|apply|hr|jobs?|careers?)", label[:-len(k)] or "x"):
            return k
    return ""


def assess(msg, body, conn, keys):
    reasons, score = [], 0
    frm_name, frm = email.utils.parseaddr(mail_sort.dec(msg.get("From")))
    frm_domain = frm.split("@")[-1].lower()
    reply = email.utils.parseaddr(mail_sort.dec(msg.get("Reply-To") or ""))[1].lower()
    ret = email.utils.parseaddr(str(msg.get("Return-Path") or ""))[1].lower()
    subject = mail_sort.dec(msg.get("Subject"))
    auth = auth_results(msg)
    ats = any(a in frm_domain for a in ATS_DOMAINS)

    def add(points, why):
        nonlocal score
        score += points
        reasons.append(f"+{points} {why}")

    if auth["spf"] in ("fail", "softfail", "permerror"):
        add(20, f"SPF {auth['spf']}: the sending server isn't authorised for {frm_domain}")
    if auth["dkim"] in ("fail", "none", ""):
        add(10 if auth["dkim"] == "" else 15, f"DKIM {auth['dkim'] or 'missing'}")
    if auth["dmarc"] == "fail":
        add(25, "DMARC fail")
    elif auth["dmarc"] in ("none", "") and not ats:
        add(5, "no DMARC policy")
    reg = registered_domain(frm_domain)
    if reg in FREE_MAIL and RECRUITING.search(subject):
        add(25, f"recruiting from a free-mail address ({reg})")
    claimed = {re.sub(r"[^a-z0-9]", "", c.lower()) for c in re.findall(
        r"\b(?:from|at|with|to|for|join)\s+([A-Z][A-Za-z0-9&.'-]+(?:\s+[A-Z][A-Za-z0-9&.'-]+){0,3})", subject)}
    claimed = {c for c in claimed if len(c) >= 3}
    k = "" if ats else (lookalike(frm_domain, claimed) or lookalike(frm_domain, keys))
    if k:
        add(35, f"look-alike domain: '{reg}' wraps company name '{k}'")
    for other, label in ((reply, "Reply-To"), (ret, "Return-Path")):
        od = registered_domain(other.split("@")[-1]) if other else ""
        if od and od != reg and not ats and not any(a in od for a in ATS_DOMAINS) and not (label == "Return-Path" and od in ("amazonses.com", "sendgrid.net", "mailgun.org", "mcsv.net")):
            add(20 if label == "Reply-To" else 5, f"{label} goes to another domain ({od})")
    age = None
    if not ats and reg and reg not in FREE_MAIL:
        age = domain_age_days(conn, reg)
        if age is not None and age < 90:
            add(35, f"domain registered {age} days ago")
        elif age is not None and age < 365:
            add(15, f"domain under a year old ({age} days)")
    ip = sending_ip(msg)
    info = ip_info(conn, ip) if ip else {"host": "", "country": "", "org": ""}
    if info.get("country") and info["country"] not in ("US", "CA", "IE", "GB", "DE", "NL") and not ats:
        add(10, f"sent from {info['country']} network ({info.get('org') or 'unknown'})")
    if ip and not info.get("host"):
        add(5, f"sending IP {ip} has no reverse DNS")
    text = f"{subject}\n{body}"
    flagged = 0
    trusted = ats and auth["spf"] == "pass" and auth["dkim"] == "pass"   # authenticated ATS mail: skip content heuristics
    for pattern, points, why in ([] if trusted else RED_FLAGS):
        if re.search(pattern, text, re.I) and flagged < 50:
            add(points, why)
            flagged += points
    try:
        spam = int(str(msg.get("X-Pm-Spamscore") or "0").strip() or 0)
    except ValueError:
        spam = 0
    if spam > 0:
        add(min(20, spam * 5), f"Proton spam score {spam}")
    level = "high" if score >= 60 else "suspicious" if score >= 30 else "clean"
    return dict(from_addr=frm, from_domain=frm_domain, reply_to=reply, subject=subject[:200], ip=ip,
                ip_host=info.get("host", ""), ip_country=info.get("country", ""), ip_org=info.get("org", ""),
                spf=auth["spf"], dkim=auth["dkim"], dmarc=auth["dmarc"], domain_age_days=age,
                score=score, level=level, reasons=json.dumps(reasons))


def _body_text(raw):
    msg = email.message_from_bytes(raw)
    parts = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() in ("text/plain", "text/html"):
            try:
                parts.append(part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace"))
            except Exception:
                continue
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", " ".join(parts)))[:20000]


def scan(days=14, rescan=False, log=print):
    env = mail_sort.keys()
    m = imaplib.IMAP4("127.0.0.1", 1143)
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    m.starttls(ctx)
    m.login(env["BRIDGE_USER"], env["BRIDGE_PASS"])
    conn = db()
    keys = company_keys(conn)
    since = (dt.date.today() - dt.timedelta(days=days)).strftime("%d-%b-%Y")
    counts = {"scanned": 0, "suspicious": 0, "high": 0}
    for box in BOXES:
        typ, _ = m.select(box, readonly=True)
        if typ != "OK":
            continue
        _, data = m.uid("SEARCH", None, f"SINCE {since}")
        uids = data[0].split()
        for i in range(0, len(uids), 200):
            _, resp = m.uid("FETCH", b",".join(uids[i:i + 200]), "(UID BODY.PEEK[HEADER])")
            heads, pending = [], None   # Bridge may put "UID n" before or after the header literal
            for part in resp:
                head = part[0] if isinstance(part, tuple) else part
                u = re.search(rb"UID (\d+)", head or b"")
                if isinstance(part, tuple):
                    pending = part[1]
                if u and pending is not None:
                    heads.append((u.group(1), email.message_from_bytes(pending)))
                    pending = None
            for uid, msg in heads:
                key = str(msg.get("Message-Id") or f"{box}:{uid.decode()}").strip()
                if not rescan and conn.execute("SELECT 1 FROM mail_threats WHERE msg_key=?", (key,)).fetchone():
                    continue
                frm, subj = mail_sort.dec(msg.get("From")), mail_sort.dec(msg.get("Subject"))
                if box != f'"{mail_sort.FOLDER}"' and not (mail_sort.is_job(frm, subj) or RECRUITING.search(subj)):
                    continue
                _, body = m.uid("FETCH", uid, "(BODY.PEEK[])")
                full = next((p[1] for p in body if isinstance(p, tuple)), b"")
                row = assess(msg, _body_text(full), conn, keys)
                try:
                    when = email.utils.parsedate_to_datetime(msg.get("Date")).isoformat() if msg.get("Date") else ""
                except (TypeError, ValueError):
                    when = ""
                conn.execute("INSERT OR REPLACE INTO mail_threats VALUES (:k,:box,:when,:from_addr,:from_domain,:reply_to,:subject,:ip,"
                             ":ip_host,:ip_country,:ip_org,:spf,:dkim,:dmarc,:domain_age_days,:score,:level,:reasons,:now)",
                             {**row, "k": key, "box": box.strip('"'), "when": when, "now": dt.datetime.now().isoformat(timespec="seconds")})
                counts["scanned"] += 1
                if row["level"] != "clean":
                    counts[row["level"]] += 1
                    log(f"threat {row['level']:10} {row['score']:3} {row['from_addr'][:40]} | {row['subject'][:60]}")
            conn.commit()
        conn.commit()
    conn.close()
    m.logout()
    log(f"threat scan: {counts['scanned']} new messages, {counts['suspicious']} suspicious, {counts['high']} high")
    return counts


if __name__ == "__main__":
    scan(int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 14, "--rescan" in sys.argv)
