"""Interview invitations are told apart from 'if selected, we will schedule' receipts."""
import mail_sort


def is_invite(text, subject="Next steps"):
    hit = mail_sort.INTERVIEW.search(text)
    return bool(hit and not mail_sort.REJECT.search(text) and not mail_sort.CONFIRM.search(subject)
                and not mail_sort.CONDITIONAL.search(text[max(0, hit.start() - 200):hit.end()]))


def test_real_invites():
    assert is_invite("Hi Mike, we'd like to invite you to a 30 minute phone screen. Please use this link to book a time.")
    assert is_invite("Please share your availability for next week: calendly.com/recruiter/30min")


def test_receipts_are_not_invites():
    assert not is_invite("If you are among the qualified candidates, a member of Talent Acquisition will contact you to schedule an initial phone screen.")
    assert not is_invite("If your experience aligns with the role, one of our recruiters will reach out to schedule a conversation.")
    assert not is_invite("We'd like to schedule a call", subject="Thank you for applying to Acme")
