"""Map raw fetched items (currently: WikiCFP only) into the shared
ConferenceRecord schema defined in common.py.

Run standalone against the output of fetch_wikicfp.py for a quick check:
    python tools/normalize_records.py
"""
from __future__ import annotations

import html
import re
from typing import Optional

from common import ConferenceRecord, TMP_DIR, read_json, write_json, today

ONLINE_HINTS = ("online", "virtual", "webinar")
HYBRID_HINTS = ("hybrid",)

YEAR_RE = re.compile(r"\b(20\d{2})\b")


def clean_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    # WikiCFP titles are sometimes double HTML-escaped (e.g. "&amp;amp;").
    text = html.unescape(html.unescape(value)).strip()
    return re.sub(r"\s+", " ", text)


def infer_mode(location: Optional[str]) -> str:
    if not location:
        return "unknown"
    low = location.lower()
    if any(h in low for h in HYBRID_HINTS):
        return "hybrid"
    if any(h in low for h in ONLINE_HINTS):
        return "online"
    return "onsite"


def extract_year(title: str, conference_start: Optional[str]) -> Optional[int]:
    if conference_start:
        m = YEAR_RE.search(conference_start)
        if m:
            return int(m.group(1))
    m = YEAR_RE.search(title or "")
    return int(m.group(1)) if m else None


def pick_submission_deadline(milestones: dict) -> Optional[str]:
    """Prefer an exact 'Submission Deadline' milestone; otherwise fall back to
    the earliest-dated milestone whose name suggests an author action
    (abstract/paper/submission), since WikiCFP entries phrase this
    inconsistently (e.g. "Paper Submission Deadline", "Abstract Due")."""
    if "Submission Deadline" in milestones:
        return milestones["Submission Deadline"]
    candidates = []
    for name, when in milestones.items():
        low = name.lower()
        if any(kw in low for kw in ("submission", "abstract", "paper due", "deadline")):
            candidates.append(when)
    return min(candidates) if candidates else None


def pick_notification_date(milestones: dict) -> Optional[str]:
    for name, when in milestones.items():
        if "notification" in name.lower():
            return when
    return None


def normalize_wikicfp_record(raw: dict) -> ConferenceRecord:
    title = clean_text(raw.get("title", ""))
    location = clean_text(raw.get("location"))
    milestones = raw.get("milestones", {})
    today_iso = today().isoformat()

    official_url = raw.get("official_url") or raw.get("wikicfp_link")
    if official_url and official_url.startswith("//"):
        official_url = "https:" + official_url

    return ConferenceRecord(
        source="wikicfp",
        source_id=raw["eventid"],
        title=title,
        url=official_url,
        cfp_url=raw["wikicfp_link"],
        location=location,
        mode=infer_mode(location),
        topics=raw.get("categories", []),
        submission_deadline=pick_submission_deadline(milestones),
        notification_date=pick_notification_date(milestones),
        conference_start=raw.get("conference_start"),
        conference_end=raw.get("conference_end"),
        first_seen=today_iso,
        last_verified=today_iso,
    )


def run(raw_records: Optional[list[dict]] = None) -> list[ConferenceRecord]:
    raw_records = raw_records if raw_records is not None else read_json(
        TMP_DIR / "wikicfp_raw_records.json", default=[]
    )
    records = [normalize_wikicfp_record(r) for r in raw_records]
    # Drop anything we couldn't extract a submission deadline for — without
    # one, we can't rank or window-filter it, and "verify on official site"
    # only helps once there's a plausible deadline to verify.
    records = [r for r in records if r.submission_deadline]
    write_json(TMP_DIR / "normalized_records.json", [r.to_dict() for r in records])
    return records


if __name__ == "__main__":
    records = run()
    print(f"Normalized {len(records)} records -> .tmp/normalized_records.json")
