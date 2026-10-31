"""Optional WOTC tax-credit questionnaire: open it, opt out, return to the application (mock pages, no network)."""
import re
import pytest
from playwright.sync_api import sync_playwright

import workday

pytestmark = pytest.mark.browser

POPUP = '''<html><body><h1>Tax Credit Questionnaire</h1>
<button onclick="document.body.innerHTML='<p>Are you sure?</p><button onclick=&quot;window.close()&quot;>Yes</button>'">Opt Out</button></body></html>'''
FRAME = '''<html><body><p>Please take a moment to support Centene's participation in the Work Opportunity Tax Credit and other federal programs.</p>
<button onclick="window.open('about:blank','w').document.write(parent.POPUP)">Take Assessment</button></body></html>'''


def page_with(ctx, frame_html):
    page = ctx.new_page()
    page.set_content(f'<html><body><div data-automation-id="progressBarActiveStep">current step 5 of 6 Take Assessment</div>'
                     f'<iframe id="f" srcdoc="{frame_html.replace(chr(34), "&quot;")}"></iframe>'
                     f'<script>window.POPUP = {POPUP!r};</script></body></html>')
    page.wait_for_timeout(500)
    return page


def test_wotc_text_inside_frame_is_found_and_opted_out():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        page = page_with(b.new_context(), FRAME)
        assert workday.WOTC.search(workday._all_text(page))
        results, fields = [], []
        assert workday._wotc_opt_out(page, results, fields) == []
        assert any(r['key'] == 'wotc_opt_out' and r['status'] == 'filled' for r in results)
        b.close()


def test_no_opt_out_control_is_a_blocker():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        frame = FRAME.replace("parent.POPUP", "'<p>Answer all questions</p><button>Next</button>'")
        page = page_with(b.new_context(), frame)
        results, fields = [], []
        out = workday._wotc_opt_out(page, results, fields)
        assert out and 'opt-out control not found' in out[0]
        b.close()


def test_opt_out_pattern():
    for t in ['Opt Out', 'Opt-out', 'I do not wish to participate', 'Decline to participate', 'No thanks']:
        assert workday.OPT_OUT.search(t), t
    assert not workday.OPT_OUT.search('Continue')
