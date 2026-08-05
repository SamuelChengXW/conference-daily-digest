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
        and is_in_window(r, window_days)
    ]
    return rank(filtered)


if __name__ == "__main__":
    results = run()
    print(f"{len(results)} records pass filters and are within the deadline window:")
    for r in results:
        print(f"  {r.submission_deadline}  ({r.relevance_score:.2f})  {r.title[:70]}")
