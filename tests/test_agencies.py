"""Agency sources: parsing and the per-agency daily cap."""
import sqlite3

import agencies

RH = '''<rhcl-job-card job-id="00320-1-usen" variant="card">
<a href="https://www.roberthalf.com/us/en/job/culver-city-ca/program-manager/00320-1-usen" slot="headline" target="_blank"> Program Manager </a>
<ul slot="job-info"><li data-subslot="location">Culver City, CA</li><li data-subslot="worksite">remote</li>
<li data-subslot="type">Temporary / Contract</li><li data-subslot="salary"><span data-subslot="salary-min">39.5865</span> -
<span data-subslot="salary-max">45.837</span> <span data-subslot="salary-currency">USD</span> / <span data-subslot="salary-period">Hourly</span></li>
<li data-subslot="copy">Lead technical initiatives.</li></ul></rhcl-job-card>'''

MP = '''<ul><li class="views-row"><div about="/job-detail/program-manager-retail/ref/jn-092026-7109491" class="job-tile search-job-tile">
<div class="job-title " id="1"><h3><a href="/job-detail/program-manager-retail/ref/jn-092026-7109491">Program Manager - Retail</a></h3></div>
<div class="job-properties"><div class="job-location"><i></i> Dallas, Texas</div><div class="job-contract-type"><i></i> Permanent</div>
<div class="job-salary"><i></i> USD81,300 - USD101,200 per year</div><div class="job-nature"><i></i> Hybrid working</div></div>
<div class="job-summary"><p>Coordinate projects.</p></div><div class="job-links"></div></div></li></ul>'''


def test_parse_roberthalf_remote_hourly():
    job = next(agencies.parse_roberthalf(RH))
    assert job["company"] == "Robert Half" and job["title"] == "Program Manager"
    assert job["location"].startswith("Remote - United States")
    assert job["salary"] == "$39.59 - $45.84 per hour"
    assert "Contract" in job["description"]


def test_parse_michaelpage_hybrid():
    job = next(agencies.parse_michaelpage(MP))
    assert job["url"] == "https://www.michaelpage.com/job-detail/program-manager-retail/ref/jn-092026-7109491"
    assert job["location"] == "Hybrid - Dallas, Texas" and job["salary"].startswith("$81,300")


def test_adecco_job_remote():
    job = agencies.adecco_job({"jobTitle": "Program Manager", "jobId": "US_EN_99_1", "jobLocation": "Raleigh, North Carolina",
                               "isRemote": True, "minsalary": 50, "maxsalary": 60, "salaryTimeScale": "Hour",
                               "contractTypeTitle": "Contract/Temporary", "postedDate": "2026-11-03T00:00:00Z"})
    assert job["location"].startswith("Remote") and job["posted_at"] == "2026-11-03"
    assert job["url"].endswith("/us_en_99_1")


def test_daily_picks_top_two_per_agency_minus_today():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE jobs (url TEXT, source TEXT, status TEXT, notes TEXT, applied_at TEXT)")
    conn.execute("INSERT INTO jobs VALUES ('x','adecco','needs_you',?, '')", (agencies.lead_note() + " | lead",))
    jobs = [dict(url=f"rh{i}", source="roberthalf", fit=i) for i in range(5)] + \
           [dict(url=f"ad{i}", source="adecco", fit=i) for i in range(5)] + \
           [dict(url="old", source="michaelpage", fit=99, notes="AGENCY LEAD 2026-01-01 | lead")]
    picks = agencies.daily_picks(jobs, conn, cap=2)
    assert [j["url"] for j in picks] == ["rh4", "ad4", "rh3"]
