"""Write needs_you.html: the user's to-do list of jobs the bot couldn't finish, ranked by fit (dark mode)."""
import html, re, sqlite3, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "needs_you.html"


def kind(status, notes):
    n = (notes or "").lower()
    if status == "captcha" or "captcha" in n:
        return "CAPTCHA", "Open it, solve the CAPTCHA and submit; the form may need refilling."
    if "lead" in n or "apply yourself" in n:
        return "Apply yourself", "Job board lead; apply on the employer's site."
    if "arbitration" in n or "waiver" in n:
        return "Your call", "Contains an arbitration or rights waiver; the bot never accepts these."
    if "draft_rejected" in n or "cover" in n:
        return "Write an answer", "A written answer or cover letter is needed."
    if "no approved answer" in n:
        return "Answer a question", "A question only you can answer."
    if "verification" in n or "sign-in" in n or "password" in n:
        return "Account", "Workday account or sign-in issue."
    return "Review", "Open and finish manually."


def main(limit=400):
    db = sqlite3.connect(f"file:{ROOT / 'applications.sqlite3'}?mode=ro", uri=True, timeout=30)
    rows = db.execute("SELECT company,title,location,salary,fit,status,notes,coalesce(nullif(apply_url,''),url),source "
                      "FROM jobs WHERE status IN ('needs_you','captcha','staged') ORDER BY fit DESC, found_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for i, (co, title, loc, sal, fit, status, notes, url, src) in enumerate(rows, 1):
        tag, hint = kind(status, notes)
        q = re.sub(r"\s+", " ", notes or "")[:220]
        out.append(f"<tr><td>{i}</td><td class=fit>{int(fit or 0)}</td><td><a href='{html.escape(url or '', True)}' target=_blank>"
                   f"{html.escape(title or '')}</a><div class=co>{html.escape(co or '')} · {html.escape((loc or '')[:60])}"
                   f"{' · ' + html.escape(sal) if sal else ''}</div></td><td><span class='tag'>{tag}</span><div class=hint>{hint}</div>"
                   f"<div class=q>{html.escape(q)}</div></td></tr>")
    page = f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Needs you</title>
<style>body{{background:#121417;color:#d8dde3;font:14px/1.45 system-ui,sans-serif;margin:0;padding:16px}}h1{{font-size:20px;margin:0 0 4px}}
.sub{{color:#8b949e;margin-bottom:14px}}table{{border-collapse:collapse;width:100%}}td{{border-top:1px solid #262b31;padding:8px 6px;vertical-align:top}}
a{{color:#7cc4ff;text-decoration:none;font-weight:600}}a:visited{{color:#b59cff}}.co,.hint{{color:#8b949e;font-size:12.5px}}.q{{color:#6e7681;font-size:12px;margin-top:2px}}
.fit{{color:#7ee2a8;font-weight:700}}.tag{{background:#1f2933;border:1px solid #2f3a45;border-radius:4px;padding:1px 6px;font-size:12px}}</style>
<h1>Needs you: {len(rows)} jobs</h1><div class=sub>Highest fit first. Visited links turn purple. Regenerate: python3 build/needs_you_page.py</div>
<table>{''.join(out)}</table>"""
    OUT.write_text(page)
    print(OUT, len(rows))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 400)
