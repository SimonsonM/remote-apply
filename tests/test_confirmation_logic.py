"""Pure-logic checks for submission confirmation after a click."""
import hashlib

import pytest

import apply


class FakePage:
    def __init__(self, url, body, form_present):
        self.url = url
        self.body = body
        self.form_present = form_present

    def wait_for_function(self, *args, **kwargs):
        # Let confirm_submission's final Python predicate make the decision.
        pass

    def inner_text(self, selector):
        assert selector == 'body'
        return self.body

    def locator(self, selector):
        assert selector == 'form'
        return self

    def count(self):
        return int(self.form_present)


@pytest.mark.parametrize('path_changed,form_present,new_text,expected', [
    (True, True, True, True),
    (True, True, False, False),
    (False, False, True, True),
    (False, False, False, False),
    (False, True, True, False),
])
def test_confirm_submission_requires_new_text_and_changed_path_or_gone_form(
        path_changed, form_present, new_text, expected):
    baseline_url = 'http://localhost/apply'
    baseline_text = ('Job details' if new_text else
                     'Thank you for applying\nJob details')
    post_text = ('Your application has been received' if new_text else
                 'Thank you for applying\nThe form has closed')
    page = FakePage('http://localhost/done' if path_changed else baseline_url,
                    post_text, form_present)
    baseline_hash = hashlib.sha256(baseline_text.encode()).hexdigest()

    assert apply.confirm_submission(page, baseline_url, baseline_hash, baseline_text,
                                    timeout=1) is expected


def test_maven_style_thank_you_counts_as_confirmation():
    assert apply.confirmation_text("Thank you for your interest in working with Maven Clinic! We're looking forward to learning more about you.")
    assert not apply.confirmation_text("Thank you for your interest in Maven Clinic. Apply below.")


def test_launchdarkly_and_instacart_confirmations():
    assert apply.confirmation_text("Thank you for your interest in LaunchDarkly. We are delighted that you would consider joining our growing team.")
    assert apply.confirmation_text("Kale Yeah! Thanks for applying to Instacart, we've got it from here! Our team is reviewing your application and will be in touch.")
