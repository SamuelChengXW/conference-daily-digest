"""Apply the deadline window + minimum relevance score, and sort what's left
by submission deadline (soonest first) for the digest.

Run standalone against the persistent DB:
    python tools/filter_and_rank.py
"""
from __future__ import annotations

from typing import Optional

from common import ConferenceRecord, DB_PATH, load_config, parse_iso_date, read_json, today


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


if __name__ == "__main__":
    results = run()
    print(f"{len(results)} records pass filters and are within the deadline window:")
    for r in results:
        print(f"  {r.submission_deadline}  ({r.relevance_score:.2f})  {r.title[:70]}")
