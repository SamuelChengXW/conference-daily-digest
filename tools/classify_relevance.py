"""Deterministic keyword-based relevance scoring against config/filters.yaml.

Phase 1 scores every record this way (WikiCFP items only). Phase 2 will add
an LLM-based extraction path for less-structured search-API-sourced records,
kept as a separate function so this deterministic path stays untouched.

Run standalone against normalize_records.py's output:
    python tools/classify_relevance.py
"""
from __future__ import annotations

from typing import Optional

from common import ConferenceRecord, TMP_DIR, load_config, read_json, write_json


def score_keyword_match(record: ConferenceRecord, config: dict) -> tuple[float, list[str]]:
    """Return (score 0-1, matched_topic_names).

    Score = sum of weights of topics with >=1 keyword hit, normalized by the
    sum of all topic weights. Searches the title, WikiCFP categories, and
    location text.
    """
    haystack = " ".join(
        [record.title or "", " ".join(record.topics or []), record.location or ""]
    ).lower()

    topics_cfg = config.get("topics", [])
    total_weight = sum(t.get("weight", 1.0) for t in topics_cfg) or 1.0

    matched = []
    matched_weight = 0.0
    for topic in topics_cfg:
        hit = any(kw.lower() in haystack for kw in topic.get("keywords", []))
        if hit:
            matched.append(topic["name"])
            matched_weight += topic.get("weight", 1.0)

    score = min(matched_weight / total_weight, 1.0)
    return score, matched


def apply_region_boost(record: ConferenceRecord, config: dict, base_score: float) -> tuple[float, Optional[str]]:
    """Give conferences in Malaysia/Southeast Asia a scoring boost so they
    rank higher in an already-topic-relevant digest — this does NOT rescue
    an otherwise-irrelevant conference (gated on base_score > 0), since the
    project's scope is deliberately global/topic-first, not location-
    restricted. Returns (boosted_score, region_name_or_None).
    """
    region_cfg = config.get("region_boost", {})
    if not region_cfg.get("enabled") or base_score <= 0:
        return base_score, None

    location = (record.location or "").lower()
    boost = region_cfg.get("boost", 0.0)

    for region in region_cfg.get("regions", []):
        if any(kw.lower() in location for kw in region.get("keywords", [])):
            return min(base_score + boost, 1.0), region["name"]

    return base_score, None


def apply_malaysia_ai_carveout(record: ConferenceRecord, config: dict) -> bool:
    """Broadened AI/CS carve-out for the DEDICATED MALAYSIA SHEET TAB ONLY
    (per user request) — a general AI/ML/data-science conference located in
    Malaysia, independent of relevance_score/matched_topics. Deliberately
    NOT gated on an existing topic match (unlike apply_region_boost) since
    the whole point is to surface Malaysia AI conferences the energy-topic
    scoring would otherwise miss entirely. Never affects the main email/
    website/Conferences tab — see publish_sheet.py's Malaysia tab, which is
    the only place that reads this flag.
    """
    malaysia_keywords = next(
        (r["keywords"] for r in config.get("region_boost", {}).get("regions", [])
         if r["name"] == "Malaysia"),
        [],
    )
    haystack = " ".join(
        [record.title or "", " ".join(record.topics or []), record.location or ""]
    ).lower()

    is_malaysia = any(kw.lower() in haystack for kw in malaysia_keywords)
    is_ai = any(kw.lower() in haystack for kw in config.get("malaysia_ai_keywords", []))
    return is_malaysia and is_ai


def apply_exclusions(record: ConferenceRecord, config: dict) -> tuple[bool, Optional[str]]:
    """Return (excluded, reason)."""
    haystack = f"{record.title} {record.url or ''}".lower()

    for org in config.get("excluded_organizers", []):
        if org.lower() in haystack:
            return True, f"excluded organizer match: {org}"

    for kw in config.get("excluded_keywords", []):
        if kw.lower() in haystack:
            return True, f"excluded keyword match: {kw}"

    return False, None


def classify(record: ConferenceRecord, config: dict) -> ConferenceRecord:
    score, matched = score_keyword_match(record, config)
    boosted_score, region = apply_region_boost(record, config, score)
    record.relevance_score = round(boosted_score, 3)
    record.matched_topics = matched
    record.region_match = region
    record.malaysia_ai_match = apply_malaysia_ai_carveout(record, config)

    excluded, reason = apply_exclusions(record, config)
    record.excluded = excluded
    record.exclude_reason = reason

    return record


def run(records: Optional[list[ConferenceRecord]] = None, config: Optional[dict] = None) -> list[ConferenceRecord]:
    config = config or load_config()
    if records is None:
        raw = read_json(TMP_DIR / "normalized_records.json", default=[])
        records = [ConferenceRecord.from_dict(r) for r in raw]

    classified = [classify(r, config) for r in records]
    write_json(TMP_DIR / "classified_records.json", [r.to_dict() for r in classified])
    return classified


if __name__ == "__main__":
    records = run()
    kept = [r for r in records if not r.excluded]
    print(f"Classified {len(records)} records ({len(kept)} not excluded) "
          f"-> .tmp/classified_records.json")
    for r in sorted(kept, key=lambda r: -r.relevance_score)[:5]:
        region = f" [{r.region_match}]" if r.region_match else ""
        print(f"  {r.relevance_score:.2f}  {r.title[:70]}{region}  {r.matched_topics}")
