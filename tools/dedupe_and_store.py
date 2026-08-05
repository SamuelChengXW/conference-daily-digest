"""Merge freshly classified records into the persistent dedup database
(data/conferences_db.json), which is git-committed each run so state
survives across ephemeral GitHub Actions runners.

Matching strategy:
  1. Exact match on `dedup_key` (source:source_id — e.g. WikiCFP's stable
     eventid). Cheap and unambiguous whenever the source provides one.
  2. Fuzzy match as a fallback (fuzzy match against records lacking an exact
     hit): normalized-title token-sort-ratio >= FUZZY_THRESHOLD, GATED by
     matching year — otherwise annual recurrences ("HEEPS 2026" vs
     "HEEPS 2027") would wrongly merge.

New records get `first_seen` set; matched existing records get their mutable
fields (deadlines, relevance, etc.) refreshed and `last_verified` bumped,
while `first_seen` is preserved from the original insert.

Run standalone against classify_relevance.py's output:
    python tools/dedupe_and_store.py
"""
from __future__ import annotations

import re
from typing import Optional

from rapidfuzz import fuzz

from common import ConferenceRecord, DB_PATH, TMP_DIR, read_json, write_json, today

FUZZY_THRESHOLD = 90
ORDINAL_RE = re.compile(
    r"\b\d+(?:st|nd|rd|th)\b|\binternational\b|\bconference\b|\bon\b|\bthe\b",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"\b(20\d{2})\b")


def normalize_title(title: str) -> str:
    text = ORDINAL_RE.sub("", title.lower())
    return re.sub(r"\s+", " ", text).strip()


def extract_year(record: ConferenceRecord) -> Optional[int]:
    for candidate in (record.conference_start, record.title):
        if candidate:
            m = YEAR_RE.search(candidate)
            if m:
                return int(m.group(1))
    return None


def load_db() -> dict[str, ConferenceRecord]:
    raw = read_json(DB_PATH, default={})
    return {k: ConferenceRecord.from_dict(v) for k, v in raw.items()}


def save_db(db: dict[str, ConferenceRecord]) -> None:
    write_json(DB_PATH, {k: v.to_dict() for k, v in db.items()})


def fuzzy_match(record: ConferenceRecord, db: dict[str, ConferenceRecord]) -> Optional[str]:
    norm_title = normalize_title(record.title)
    year = extract_year(record)
    for key, existing in db.items():
        if existing.source == record.source and existing.source_id == record.source_id:
            continue  # would've been caught by exact match already
        if year and extract_year(existing) != year:
            continue
        score = fuzz.token_sort_ratio(norm_title, normalize_title(existing.title))
        if score >= FUZZY_THRESHOLD:
            return key
    return None


def upsert(record: ConferenceRecord, db: dict[str, ConferenceRecord]) -> str:
    """Insert or update `record` into `db`. Returns 'new' or 'updated'."""
    key = record.dedup_key
    today_iso = today().isoformat()

    if key in db:
        existing = db[key]
        record.first_seen = existing.first_seen
        record.last_verified = today_iso
        db[key] = record
        return "updated"

    match_key = fuzzy_match(record, db)
    if match_key:
        existing = db[match_key]
        record.first_seen = existing.first_seen
        record.last_verified = today_iso
        del db[match_key]
        db[key] = record
        return "updated"

    record.first_seen = today_iso
    record.last_verified = today_iso
    db[key] = record
    return "new"


def run(records: Optional[list[ConferenceRecord]] = None) -> list[ConferenceRecord]:
    if records is None:
        raw = read_json(TMP_DIR / "classified_records.json", default=[])
        records = [ConferenceRecord.from_dict(r) for r in raw]

    db = load_db()
    new_count = updated_count = 0
    for record in records:
        if record.excluded:
            continue
        result = upsert(record, db)
        if result == "new":
            new_count += 1
        else:
            updated_count += 1

    save_db(db)
    print(f"DB upsert: {new_count} new, {updated_count} updated, {len(db)} total in db")
    return list(db.values())


if __name__ == "__main__":
    run()
