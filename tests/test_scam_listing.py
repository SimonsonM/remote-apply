"""Remote listings in scam-centre regions, or aggregator leads that read like recruitment scams, are dropped."""
import apply


def job(**kw):
    return {"company": "Acme", "title": "Operations Manager", "location": "Remote", "description": "", "source": "greenhouse", **kw}


def test_scam_regions_and_language():
    assert apply.scam_listing(job(location="Remote - Phnom Penh, Cambodia"))
    assert apply.scam_listing(job(description="Remote team based in Myawaddy."))
    assert apply.scam_listing(job(source="adzuna", description="Interview via Telegram. We send a check to buy equipment."))
    assert apply.scam_listing(job(source="himalayas", company="Confidential"))


def test_legit_remote_and_onsite_kept():
    assert not apply.scam_listing(job(location="Remote - United States"))
    assert not apply.scam_listing(job(location="Remote - Spain - Barcelona / ES"))
    assert not apply.scam_listing(job(source="greenhouse", company="Confidential"))   # employer-run ATS
    assert not apply.scam_listing(job(location="Phnom Penh, Cambodia"))                # not remote: other filters handle it
    assert not apply.scam_listing(job(location="Remote - Cambodia"), {"scam_regions": ["nowhere"]})
