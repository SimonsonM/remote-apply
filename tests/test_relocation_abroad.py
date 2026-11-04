"""Remote roles that require living in / moving to another country are dropped unless relocation is paid."""
import apply

req = lambda d, t="Program Manager": apply.requires_relocation_abroad({"title": t, "description": d})


def test_residence_or_relocation_abroad_is_dropped():
    assert req("This is a remote role. You must be based in the United Kingdom.")
    assert req("Fully remote, but candidates must reside in Germany for employment reasons.")
    assert req("Relocation to Amsterdam, Netherlands is required within 3 months.")
    assert req("Remote within Canada. Applicants must be located in Canada. No relocation assistance is provided.")
    assert apply.score({"title": "Program Manager", "location": "Remote - Worldwide", "company": "X",
                        "description": "You must live in Poland."}, {"focus_terms": [], "remote_anywhere": True}, False) is None


def test_paid_relocation_or_no_requirement_is_kept():
    assert not req("You must be based in the UK. We offer a full relocation package and visa support.")
    assert not req("Remote anywhere. We work with teammates in Germany and India.")
    assert not req("You must be based in the United States.")
    assert not req("This role is open to candidates based in North America and the United Kingdom.")
    assert not req("Open to candidates anywhere; our HQ is in London, United Kingdom.")
