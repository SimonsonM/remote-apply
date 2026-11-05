"""Indeed rows map to jobs; employer ATS links move into that system's queue."""
import indeed


def test_lead_and_remote():
    job = indeed.to_job({"company": "Acme", "title": "Project Manager", "location": "Madison, WI, USA", "is_remote": False,
                         "job_url": "https://www.indeed.com/viewjob?jk=1", "job_url_direct": None, "id": "in-1",
                         "min_amount": 30.0, "max_amount": 35.5, "interval": "hourly", "date_posted": "2026-11-03"})
    assert job["source"] == "indeed" and job["location"] == "Madison, WI"
    assert job["salary"] == "$30.00 - $35.50 per hour" and job["posted_at"] == "2026-11-03"
    remote = indeed.to_job({"company": "B", "title": "PM", "location": "", "job_url": "https://www.indeed.com/viewjob?jk=2"}, True)
    assert remote["location"] == "Remote - United States"


def test_direct_ats_link_promoted():
    job = indeed.to_job({"company": "Acme", "title": "PM", "location": "Remote", "is_remote": True,
                         "job_url": "https://www.indeed.com/viewjob?jk=3",
                         "job_url_direct": "https://job-boards.greenhouse.io/acme/jobs/12345"})
    assert job["source"] == "greenhouse"
