"""Google Jobs (SerpApi) results map to jobs; the employer's own ATS link wins."""
import google_jobs


def test_to_job_prefers_ats_link():
    item = {"title": "Program Manager", "company_name": "Acme", "location": "Madison, WI", "description": "Run programs",
            "job_id": "x1", "detected_extensions": {"posted_at": "2 days ago", "salary": "$70K-$85K a year"},
            "apply_options": [{"title": "LinkedIn", "link": "https://www.linkedin.com/jobs/view/1"},
                              {"title": "Acme", "link": "https://job-boards.greenhouse.io/acme/jobs/123"}]}
    job = google_jobs.to_job(item)
    assert job["source"] == "greenhouse" and "greenhouse" in job["apply_url"]
    assert job["salary"] == "$70K-$85K a year" and job["posted_at"]


def test_lead_and_remote():
    item = {"title": "PM", "company_name": "B", "location": "Anywhere", "detected_extensions": {"work_from_home": True},
            "apply_options": [{"title": "Indeed", "link": "https://www.indeed.com/viewjob?jk=1"},
                              {"title": "B careers", "link": "https://careers.b.com/jobs/9"}]}
    job = google_jobs.to_job(item)
    assert job["source"] == "google" and job["apply_url"] == "https://careers.b.com/jobs/9"
    assert job["location"] == "Remote - United States"
