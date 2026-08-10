"""Apply the deadline window + minimum relevance score, and sort what's left
by submission deadline (soonest first) for the digest.

Run standalone against the persistent DB:
    python tools/filter_and_rank.py
"""
from __future__ import annotations

import re
from typing import Optional

from common import ConferenceRecord, DB_PATH, load_config, parse_iso_date, read_json, today

# Free-submission detection for filter_and_rank.free_tab() — deterministic
# regex on the already-extracted fee_info text (llm_extract.py/
# fetch_search_api.py), not a second LLM call: CLAUDE.md's "deterministic
# code handles execution" applies once the unstructured page has already
# been reduced to a short structured sentence. Validated against every
# non-"Not stated" fee_info value in the live DB as of 2026-08-10 (81
# records checked, 6 non-trivial) — correctly flags both "No submission
# fee." entries as free, and correctly excludes anything mentioning a
# currency amount (even a discount off a paid fee) or "Only paid papers
# will be published." Deliberately conservative: "free for members, $50
# for non-members" has both a free-word hit and a currency hit, so it's
# NOT counted as confirmed-free (conditionally free isn't the same claim
# as "no cost, period" — worth a manual check of the official page either
# way).
_FREE_RE = re.compile(
    r"\bfree\b|\bno\s+\w*\s*fee\b|\bno\s+fee\b|no charge|no cost|complimentary|\bwaived\b",
    re.IGNORECASE,
)
_CURRENCY_RE = re.compile(
    r"(usd|myr|rm|eur|gbp|sgd|jpy|[$€£¥])\s?\d|\d+\s?(usd|myr|rm|eur|gbp|sgd|jpy)",
    re.IGNORECASE,
)


def is_confirmed_free(fee_info: Optional[str]) -> bool:
    if not fee_info:
        return False
    return bool(_FREE_RE.search(fee_info)) and not bool(_CURRENCY_RE.search(fee_info))


def is_in_window(record: ConferenceRecord, window_days: int) -> bool:
    deadline = parse_iso_date(record.submission_deadline)
    if deadline is None:
        return False
    days_until = (deadline - today()).days
    return 0 <= days_until <= window_days


def rank(records: list[ConferenceRecord]) -> list[ConferenceRecord]:
    return sorted(records, key=lambda r: r.submission_deadline or "9999-99-99")


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
    ]
    return rank(filtered)


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
        and (
            r.malaysia_ai_match
            or (r.region_match == "Malaysia" and r.relevance_score >= min_score)
        )
    ]
    return rank(filtered)


def free_tab(records: Optional[list[ConferenceRecord]] = None, config: Optional[dict] = None) -> list[ConferenceRecord]:
    """Records for the dedicated Free Submission Sheet tab (per user
    request, 2026-08-10 — tight budget, needs to see confirmed-free venues
    at a glance): the exact same membership as run() (windowed/not-expired/
    not-Dismissed/passes min_relevance_score — this is a subset of
    Conferences, not a separate universe, so it stays topic-first like
    everything else here), further filtered to is_confirmed_free(fee_info).
    Only ever shows what the fee_info extraction has actually confirmed —
    "Not stated" or no fee_info yet (not every record has been checked;
    see llm_extract.py's MAX_RECORDS_PER_RUN) means it simply doesn't
    appear here yet, not that it's assumed to cost money.
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
        and r.relevance_score >= min_score
        and r.user_status != "Dismissed"
        and is_in_window(r, window_days)
        and is_confirmed_free(r.fee_info)
    ]
    return rank(filtered)


if __name__ == "__main__":
    results = run()
    print(f"{len(results)} records pass filters and are within the deadline window:")
    for r in results:
        print(f"  {r.submission_deadline}  ({r.relevance_score:.2f})  {r.title[:70]}")
