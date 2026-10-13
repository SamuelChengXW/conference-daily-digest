"""Render the four free/funded sections (filter_and_rank.sections) into the
email HTML body and the docs/index.html website page (GitHub Pages source).

Both use the same Jinja2 template with a `standalone_page` flag so the
website version gets full <html>/<head> wrapping while the email version is
just the inner body (most mail clients strip/mangle a full document anyway).

Run standalone against the persistent DB:
    python tools/render_digest.py
"""
from __future__ import annotations

from typing import Optional

from jinja2 import Environment, FileSystemLoader

import filter_and_rank
from common import ConferenceRecord, DOCS_DIR, PROJECT_ROOT, parse_iso_date, today

TEMPLATE_DIR = PROJECT_ROOT / "tools" / "templates"

_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)


SECTION_EMPTY = {
    "free": "No confirmed free or fee-waivable conferences in the window right now.",
    "funded": "No conferences with confirmed travel grants or scholarships in the window right now.",
    "programs": "No relevant standalone funding programs open right now.",
    "journals": "No no-APC journal calls confirmed in the window right now.",
}


def urgency(record: ConferenceRecord) -> str:
    deadline = parse_iso_date(filter_and_rank.effective_deadline(record))
    if deadline is None:
        return "unknown"
    days_until = (deadline - today()).days
    if days_until <= 14:
        return "high"
    if days_until <= 45:
        return "medium"
    return "low"


def badges(record: ConferenceRecord) -> list[str]:
    out = []
    if record.opportunity_type == "journal_issue" and record.cost_status == "free":
        out.append("No APC")
    elif record.cost_status == "free":
        out.append("Free")
    elif record.cost_status == "waivable":
        out.append("Fee waiver")
    if record.funding_available:
        out.append(record.funding_type or "Funding")
    return out


def _template_sections(sections: dict[str, list[ConferenceRecord]]) -> list[dict]:
    out = []
    for key, records in sections.items():
        recs = []
        for r in records:
            d = r.to_dict()
            d["urgency"] = urgency(r)
            d["badges"] = badges(r)
            recs.append(d)
        out.append({
            "title": filter_and_rank.SECTION_TITLES[key],
            "records": recs,
            "empty": SECTION_EMPTY[key],
        })
    return out


def _render(sections: dict[str, list[ConferenceRecord]], standalone: bool, site_url: Optional[str]) -> str:
    template = _env.get_template("digest.html.j2")
    return template.render(
        sections=_template_sections(sections),
        generated_date=today().isoformat(),
        standalone_page=standalone,
        site_url=site_url,
    )


def run(sections: dict[str, list[ConferenceRecord]], site_url: Optional[str] = None) -> tuple[str, str]:
    email_html = _render(sections, standalone=False, site_url=site_url)
    site_html = _render(sections, standalone=True, site_url=site_url)
    (DOCS_DIR / "index.html").write_text(site_html, encoding="utf-8")
    return email_html, site_html


if __name__ == "__main__":
    secs = filter_and_rank.sections()
    email_html, _ = run(secs)
    print(f"Rendered {sum(len(v) for v in secs.values())} records in "
          f"{len(secs)} sections -> docs/index.html")
    print(f"Email HTML length: {len(email_html)} chars")
