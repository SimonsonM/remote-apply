"""Scam scoring: authentication, look-alike domains, content red flags; trusted ATS mail stays clean."""
import email
import sqlite3

import threat_scan


def conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE threat_cache (k TEXT PRIMARY KEY, v TEXT, ts TEXT)")
    return c


def msg(headers):
    return email.message_from_string("".join(f"{k}: {v}\n" for k, v in headers) + "\nbody")


def test_lookalike_scam_scores_high(monkeypatch):
    monkeypatch.setattr(threat_scan, "domain_age_days", lambda c, d: 40)
    monkeypatch.setattr(threat_scan, "ip_info", lambda c, ip: {"host": "", "country": "NG", "org": "Example ISP"})
    m = msg([("From", '"Renee" <renee@cvshealthsinfo.com>'), ("Subject", "An update on your application from Cvs Health"),
             ("Authentication-Results", "mx; spf=fail smtp.mailfrom=cvshealthsinfo.com"),
             ("Authentication-Results", "mx; dkim=pass header.d=cvshealthsinfo.com"),
             ("Authentication-Results", "mx; dmarc=none"), ("Received", "from x (x [203.0.113.9])")])
    row = threat_scan.assess(m, "Please download Telegram for your interview. We will mail you a check to buy equipment from our vendor.", conn(), set())
    assert row["level"] == "high"
    assert any("look-alike" in r for r in row["reasons"]) or "look-alike" in row["reasons"]


def test_authenticated_ats_mail_is_clean(monkeypatch):
    monkeypatch.setattr(threat_scan, "ip_info", lambda c, ip: {"host": "mail.greenhouse.io", "country": "US", "org": "Greenhouse"})
    m = msg([("From", "<no-reply@us.greenhouse-mail.io>"), ("Reply-To", "<recruiting@samsara.com>"),
             ("Subject", "Thank you for applying to Samsara"),
             ("Authentication-Results", "mx; spf=pass"), ("Authentication-Results", "mx; dkim=pass"),
             ("Authentication-Results", "mx; dmarc=pass")])
    row = threat_scan.assess(m, "If you are selected for an interview we will contact you. Equipment provided.", conn(), {"greenhouse", "samsara"})
    assert row["level"] == "clean", row["reasons"]


def test_lookalike_rules():
    assert threat_scan.lookalike("stripe-careers.net", {"stripe"}) == "stripe"
    assert threat_scan.lookalike("axonius.com", {"axon", "axonius"}) == ""
    assert threat_scan.lookalike("chime.com", {"chime"}) == ""
