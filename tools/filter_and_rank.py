"""Apply the free/funded eligibility rule, the deadline window, and the
minimum relevance score; sort what's left by its nearest actionable deadline
and split it into the digest's four sections (free/waivable, funded,
standalone funding programs, no-APC journal calls).

Run standalone against the persistent DB:
    python tools/filter_and_rank.py
"""
from __future__ import annotations

import re
from typing import Optional

from common import ConferenceRecord, DB_PATH, load_config, parse_iso_date, read_json, today

# Shared with extract_access.apply_verdict()'s deterministic cross-check of
# a Groq "free" verdict. Originally built (2026-08-10) for the old Free
# Submission tab and validated then against every non-"Not stated" fee_info
# in the live DB: flags "No submission fee." as free, and treats anything
# mentioning a currency amount (even a discount) as not-confirmed-free.
_FREE_RE = re.compile(
    r"\bfree\b|\bno\s+\w*\s*fee\b|\bno\s+fee\b|no charge|no cost|complimentary|\bwaived\b",
    re.IGNORECASE,
)
_CURRENCY_RE = re.compile(
    r"(usd|myr|rm|eur|gbp|sgd|jpy|[$€£¥])\s?\d|\d+\s?(usd|myr|rm|eur|gbp|sgd|jpy)",
    re.IGNORECASE,
)


ROLLING_PROGRAM_MAX_AGE_DAYS = 60


def effective_deadline(record: ConferenceRecord) -> Optional[str]:
    """The deadline that matters for acting on this opportunity: the
    nearest UPCOMING of funding_deadline and submission_deadline (a travel
    grant often closes before the paper deadline, or vice versa). Falls back
    to the latest past one if both have passed, None if neither is known.
    """
    dates = [d for d in (parse_iso_date(record.funding_deadline), parse_iso_date(record.submission_deadline)) if d]
    if not dates:
        return None
    upcoming = [d for d in dates if d >= today()]
    return (min(upcoming) if upcoming else max(dates)).isoformat()


def is_in_window(record: ConferenceRecord, window_days: int) -> bool:
    deadline = parse_iso_date(effective_deadline(record))
    if deadline is None:
        # Standalone funding programs are often rolling/evergreen — keep
        # them visible rather than silently dropping them for lack of a
        # date, but only while their source still lists them (re-fetched
        # every run, so last_verified goes stale once they disappear).
        seen = parse_iso_date(record.last_verified)
        return (record.opportunity_type == "funding_program"
                and seen is not None and (today() - seen).days <= ROLLING_PROGRAM_MAX_AGE_DAYS)
    days_until = (deadline - today()).days
    return 0 <= days_until <= window_days


def is_eligible(record: ConferenceRecord) -> bool:
    """The core rule of the free & funded tracker (2026-10-13): show only
    what's free, fee-waivable, or comes with funding. Paid / unknown /
    not-yet-checked never appear — see extract_access.py for how a verdict
    is reached (and why an unverifiable one is downgraded to unknown).
    """
    return record.cost_status in {"free", "waivable"} or record.funding_available


def rank(records: list[ConferenceRecord]) -> list[ConferenceRecord]:
    return sorted(records, key=lambda r: effective_deadline(r) or "9999-99-99")


def _archive_sort_key(record: ConferenceRecord):
    """Upcoming deadlines first (soonest first), then past deadlines (most
    recently expired first), then records with no parseable deadline last.
    Used for the Google Sheet 'living archive' view, where old entries
    should sink rather than clutter the top of an ascending date sort.
    """
    deadline = parse_iso_date(record.submission_deadline)
    if deadline is None:
        return (2, 0)
    if deadline >= today():
        return (0, deadline.toordinal())
    return (1, -deadline.toordinal())


def run(records: Optional[list[ConferenceRecord]] = None, config: Optional[dict] = None) -> list[ConferenceRecord]:
    config = config or load_config()
    if records is None:
        raw = read_json(DB_PATH, default={})
        records = [ConferenceRecord.from_dict(v) for v in raw.values()]

    window_days = config.get("deadline_window_days", 120)
    min_score = config.get("min_relevance_score", 0.0)

    filtered = [
        r for r in records
        if not r.excluded
        and r.relevance_score >= min_score
        and r.user_status != "Dismissed"
        and is_in_window(r, window_days)
        and is_eligible(r)
    ]
    return rank(filtered)


SECTION_TITLES = {
    "free": "Free or fee-waivable to submit",
    "funded": "Funding available (travel grants / scholarships)",
    "programs": "Standalone funding programs",
    "journals": "Journal calls with no publication fee (APC)",
}


def section_of(record: ConferenceRecord) -> str:
    if record.opportunity_type == "funding_program":
        return "programs"
    if record.opportunity_type == "journal_issue":
        return "journals"
    if record.cost_status in {"free", "waivable"}:
        return "free"
    return "funded"


def sections(records: Optional[list[ConferenceRecord]] = None, config: Optional[dict] = None) -> dict[str, list[ConferenceRecord]]:
    """run()'s eligible list split into the four digest/Sheet sections, in
    SECTION_TITLES order. A free conference that ALSO offers funding stays
    in "free" (with a funding badge) rather than appearing twice.
    """
    out: dict[str, list[ConferenceRecord]] = {k: [] for k in SECTION_TITLES}
    for r in run(records, config):
        out[section_of(r)].append(r)
    return out


def all_relevant(records: Optional[list[ConferenceRecord]] = None, config: Optional[dict] = None) -> list[ConferenceRecord]:
    """Like run(), but WITHOUT the deadline window filter — every relevant
    record ever seen, past or future (still excludes user-Dismissed ones).
    Not used by the main email/website/Sheet views (those all auto-drop
    expired deadlines via run(), per user request) — kept as a debug/
    reporting utility and for tools/publish_sheet.py's "Participated" tab
    reconciliation, where a record needs to be found even after it's aged
    out of the current window.
    """
    config = config or load_config()
    if records is None:
        raw = read_json(DB_PATH, default={})
        records = [ConferenceRecord.from_dict(v) for v in raw.values()]

    min_score = config.get("min_relevance_score", 0.0)
    filtered = [
        r for r in records
        if not r.excluded and r.relevance_score >= min_score and r.user_status != "Dismissed"
    ]
    return sorted(filtered, key=_archive_sort_key)


def malaysia_tab(records: Optional[list[ConferenceRecord]] = None, config: Optional[dict] = None) -> list[ConferenceRecord]:
    """Records for the dedicated Malaysia Sheet tab: windowed/not-expired/
    not-Dismissed like run(), but membership is (region_match == "Malaysia"
    AND passes the normal min_relevance_score) OR malaysia_ai_match — the
    latter deliberately bypasses min_relevance_score, since a general AI
    conference has no reason to score highly against energy-topic keywords.
    """
    config = config or load_config()
    if records is None:
        raw = read_json(DB_PATH, default={})
        records = [ConferenceRecord.from_dict(v) for v in raw.values()]

    window_days = config.get("deadline_window_days", 120)
    min_score = config.get("min_relevance_score", 0.0)

    filtered = [
        r for r in records
        if not r.excluded
        and r.user_status != "Dismissed"
        and is_in_window(r, window_days)
        and is_eligible(r)
        and (
            r.malaysia_ai_match
            or (r.region_match == "Malaysia" and r.relevance_score >= min_score)
        )
    ]
    return rank(filtered)


if __name__ == "__main__":
    for key, recs in sections().items():
        print(f"{SECTION_TITLES[key]}: {len(recs)}")
        for r in recs:
            print(f"  {effective_deadline(r)}  [{r.cost_status}{'+funding' if r.funding_available else ''}]  {r.title[:70]}")
