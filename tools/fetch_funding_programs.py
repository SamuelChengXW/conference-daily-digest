"""Standalone funding programs (travel grants, scholarships, fellowships)
that aren't tied to one conference — added in the 2026-10-13 free & funded
redesign, per user request. Two kinds of source, both configured in
config/filters.yaml's funding_programs:

  pages:    one program per URL (e.g. IEEE PELS Student Travel Grants).
            Emitted with access_checked=None so extract_access.py gives it
            the same evidence-backed funding verdict as any conference, and
            re-checks it every RECHECK_PROGRAM_DAYS (program pages change
            each cycle).

  listings: a page listing many programs (Terra Viva Grants' travel &
            mobility tag). Parsed deterministically — no LLM — since every
            item is already a short, structured summary: title, official
            link, tags (region, deadline month), and a text body that
            states the deadline in a consistent phrasing. Items are funding
            by construction, so they're emitted already verified
            (funding_available=True, evidence quote = the money/award
            sentence from the item text).

MDPI's own travel-award pages were the obvious first source but return 403
to scripted requests (checked 2026-10-13); Terra Viva mirrors those awards
on a public, robots.txt-permitted listing.

Run standalone for a quick check (no DB writes):
    python tools/fetch_funding_programs.py
"""
from __future__ import annotations

import calendar
import hashlib
import re
from datetime import date
from typing import Optional

from bs4 import BeautifulSoup

import extract_access
from common import ConferenceRecord, load_config, today

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(20\d{2})\b", re.IGNORECASE
)
DEADLINE_CUE_RE = re.compile(r"deadline|submitted by|apply by|applications? (?:close|due)|due by|until", re.IGNORECASE)
MONTH_TAG_RE = re.compile(r"^(" + "|".join(MONTHS) + r")(20\d{2})$")
MONEY_SENTENCE_RE = re.compile(
    r"[^.]*\b(?:US\$|USD|EUR|€|£|CHF|grant|award|travel support|stipend|scholarship|fellowship|covers?|pays?)\b[^.]*\.",
    re.IGNORECASE,
)


def _id(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def parse_deadline(text: str, tags: list[str]) -> Optional[str]:
    """First date that follows a deadline cue within ~120 chars; else the
    end of the month named by a Terra Viva month tag (e.g. 'october2026');
    else None (also for 'no-deadline' items — rolling programs).
    """
    for cue in DEADLINE_CUE_RE.finditer(text):
        m = DATE_RE.search(text, cue.end(), cue.end() + 120)
        if m:
            day, month, year = int(m.group(1)), MONTHS[m.group(2).lower()], int(m.group(3))
            try:
                return date(year, month, day).isoformat()
            except ValueError:
                continue
    if "no-deadline" in tags:
        return None
    months = []
    for t in tags:
        m = MONTH_TAG_RE.match(t)
        if m:
            year, month = int(m.group(2)), MONTHS[m.group(1)]
            months.append(date(year, month, calendar.monthrange(year, month)[1]))
    upcoming = [d for d in months if d >= today()]
    if upcoming:
        return min(upcoming).isoformat()
    return max(months).isoformat() if months else None


def parse_terraviva_listing(html: str, listing: dict) -> list[ConferenceRecord]:
    soup = BeautifulSoup(html, "html.parser")
    region_tags = set(listing.get("region_tags", []))
    exclude_tags = set(listing.get("exclude_tags", []))
    today_iso = today().isoformat()

    records = []
    for art in soup.find_all("article"):
        tags = [c[4:] for c in art.get("class", []) if c.startswith("tag-")]
        categories = [c[9:] for c in art.get("class", []) if c.startswith("category-")]
        if region_tags and not region_tags & set(tags):
            continue
        if exclude_tags & set(tags):
            continue
        heading = art.find(["h2", "h3"])
        link = heading.find("a", href=True) if heading else None
        body = art.find(class_="entry-content")
        if not link or not body:
            continue
        post_url = link["href"]
        text = " ".join(body.get_text(" ", strip=True).split())
        official = next(
            (a["href"] for a in body.find_all("a", href=True)
             if "terravivagrants.org" not in a["href"]),
            post_url,
        )
        money = MONEY_SENTENCE_RE.search(text)
        quote = money.group(0).strip() if money else text[:200]

        records.append(ConferenceRecord(
            source="funding_listing",
            source_id=_id(post_url),
            title=heading.get_text(" ", strip=True),
            url=official,
            cfp_url=post_url,
            topics=tags + categories,
            opportunity_type="funding_program",
            cost_status="unknown",
            funding_available=True,
            funding_type="travel / mobility grant",
            funding_details=text[:600],
            funding_deadline=parse_deadline(text, tags),
            funding_url=official,
            travel_support_info=text[:300],
            evidence_url=post_url,
            evidence_quote=quote,
            access_checked=today_iso,
            first_seen=today_iso,
            last_verified=today_iso,
        ))
    return records


def page_record(page: dict) -> ConferenceRecord:
    today_iso = today().isoformat()
    return ConferenceRecord(
        source="funding_page",
        source_id=_id(page["url"]),
        title=page["name"],
        url=page["url"],
        cfp_url=page["url"],
        topics=list(page.get("topics", [])),
        opportunity_type="funding_program",
        first_seen=today_iso,
        last_verified=today_iso,
    )


def run(config: Optional[dict] = None) -> list[ConferenceRecord]:
    config = config or load_config()
    cfg = config.get("funding_programs", {})
    if not cfg.get("enabled"):
        print("funding_programs.enabled is false in config — skipping.")
        return []

    records = [page_record(p) for p in cfg.get("pages", [])]

    for listing in cfg.get("listings", []):
        base = listing["url"].rstrip("/") + "/"
        for n in range(1, listing.get("pages", 1) + 1):
            url = base if n == 1 else f"{base}page/{n}/"
            html = extract_access.fetch_html(url)
            if not html:
                print(f"  WARNING: couldn't fetch {url} — skipping.")
                continue
            items = parse_terraviva_listing(html, listing)
            print(f"  {listing['name']} p{n}: {len(items)} eligible-region item(s)")
            records.extend(items)
    return records


if __name__ == "__main__":
    for r in run():
        print(f"  {r.funding_deadline or '(rolling)':10}  {r.title[:70]}  {r.topics[:4]}")
