"""remote_anywhere: worldwide remote roles are eligible; foreign work-authorization questions go to the user."""
import apply
import employer_link

PROFILE = {"focus_terms": [], "remote_anywhere": True}


def job(location):
    return dict(title="Program Manager", location=location, description="", company="Example")


def test_remote_anywhere_accepts_non_us_remote():
    assert apply.score(job("Remote - Germany"), PROFILE, True) is not None
    assert apply.score(job("Remote - Germany"), {**PROFILE, "remote_anywhere": False}, True) is None
    assert apply.score(job("Hybrid - London"), PROFILE, True) is None


def test_foreign_work_authorization_goes_to_user():
    assert apply.foreign_work_question("Are you legally authorized to work in the United Kingdom?")
    assert apply.foreign_work_question("Do you have the right to work in Canada?")
    assert apply.foreign_work_question("Are you authorized to work in this country?", {"location": "Remote - Poland"})
    assert not apply.foreign_work_question("Are you authorized to work in the United States?")
    assert not apply.foreign_work_question("Are you authorized to work in this country?", {"location": "Remote - United States"})
    assert apply.foreign_work_question("Will you require visa sponsorship?", {"location": "Remote - Germany"})
    assert not apply.foreign_work_question("Will you require visa sponsorship?", {"location": "Remote - Worldwide"})
    assert not apply.foreign_work_question("Years of experience in the UK market?")


def test_remote_lead_matches_same_region_posting():
    assert employer_link.location_compatible("Remote - UK", "Remote, UK")
    assert not employer_link.location_compatible("Remote - United States", "Remote - UK")


def test_foreign_work_answers_are_truthful_remote_from_us():
    prof = {"us_citizen": True}
    right = apply.foreign_work_answer("Do you have the right to work in the UK?", prof)
    assert right.endswith("|No") and "remotely from the United States" in right
    assert apply.foreign_work_answer("Will you require visa sponsorship to work in the UK?", prof).endswith("|Yes")
    assert apply.pick_option(right, ["Yes", "No"]) == "No"
    assert apply.foreign_work_answer("Right to work in the UK?", {}) == ""
    assert apply.resolve("Do you have the right to work in the UK?", prof, [], "Acme").endswith("|No")
