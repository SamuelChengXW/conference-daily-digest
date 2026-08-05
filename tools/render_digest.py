"""Render the ranked record list into the email HTML body and the
docs/index.html website page (GitHub Pages source).

Both use the same Jinja2 template with a `standalone_page` flag so the
website version gets full <html>/<head> wrapping while the email version is
just the inner body (most mail clients strip/mangle a full document anyway).

Run standalone against filter_and_rank.py's output:
    python tools/render_digest.py
"""
from __future__ import annotations

from typing import Optional

from jinja2 import Environment, FileSystemLoader

from common import ConferenceRecord, DOCS_DIR, PROJECT_ROOT, parse_iso_date, today

TEMPLATE_DIR = PROJECT_ROOT / "tools" / "templates"

_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)


def urgency(record: ConferenceRecord) -> str:
    deadline = parse_iso_date(record.submission_deadline)
    if deadline is None:
        return "unknown"
    days_until = (deadline - today()).days
    if days_until <= 14:
        return "high"
    if days_until <= 45:
        return "medium"
    return "low"


def _template_records(records: list[ConferenceRecord]) -> list[dict]:
    out = []
    for r in records:
        d = r.to_dict()
        d["urgency"] = urgency(r)
        out.append(d)
    return out


def render_email_html(records: list[ConferenceRecord]) -> str:
    template = _env.get_template("digest.html.j2")
    return template.render(
        records=_template_records(records),
        generated_date=today().isoformat(),
        standalone_page=False,
        site_url=None,
    )


def render_site_index(records: list[ConferenceRecord], site_url: Optional[str] = None) -> str:
    template = _env.get_template("digest.html.j2")
    return template.render(
        records=_template_records(records),
        generated_date=today().isoformat(),
        standalone_page=True,
        site_url=site_url,
    )


def run(records: list[ConferenceRecord]) -> tuple[str, str]:
    email_html = render_email_html(records)
    site_html = render_site_index(records)
    (DOCS_DIR / "index.html").write_text(site_html, encoding="utf-8")
    return email_html, site_html


if __name__ == "__main__":
    from common import DB_PATH, read_json
    import filter_and_rank

    results = filter_and_rank.run()
    email_html, site_html = run(results)
    print(f"Rendered {len(results)} records -> docs/index.html")
    print(f"Email HTML length: {len(email_html)} chars")
