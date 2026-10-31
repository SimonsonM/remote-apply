import resume_import

TEXT = """**JANE DOE, MBA**

**Program Management | AI Operations**

**PROFESSIONAL EXPERIENCE**

**Manager** | Acme Aviation
May 2026 - Present | Columbus, Ohio

Managed daily drop zone operations, including safety, staff, and manifest.

**IT Consultant** | Fabrikam Partners
2021 - 2023

Gathered open-source intelligence for authorized assessments.
"""


def test_two_line_headers_and_name():
    facts = resume_import.parse_text(TEXT)
    got = {(f['key'], f['value']) for f in facts}
    assert ('name', 'Jane Doe') in got
    assert ('role', 'Manager | Acme Aviation | May 2026 - Present') in got
    assert ('role', 'IT Consultant | Fabrikam Partners | 2021 - 2023') in got
    assert any(k == 'role_bullet' and v.startswith('Manager | Acme Aviation') for k, v in got)
    assert not any(k in ('city', 'state', 'location') for k, _ in got)
