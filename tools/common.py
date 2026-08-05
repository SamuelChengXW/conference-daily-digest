"""Shared paths, config loading, and the record schema used across all tools/*.py.

Per CLAUDE.md's WAT framework, these are deterministic helpers — no
decision-making, just plumbing shared by the pipeline steps.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = PROJECT_ROOT / ".tmp"
DATA_DIR = PROJECT_ROOT / "data"
DOCS_DIR = PROJECT_ROOT / "docs"
CONFIG_PATH = PROJECT_ROOT / "config" / "filters.yaml"
DB_PATH = DATA_DIR / "conferences_db.json"

TMP_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)
DOCS_DIR.mkdir(exist_ok=True)


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def today() -> date:
    """Wrapped so tests/dry-runs can monkeypatch a fixed 'today' if needed."""
    return date.today()


@dataclass
class ConferenceRecord:
    """Normalized schema for a single conference/CFP listing.

    Dates are stored as ISO 'YYYY-MM-DD' strings (or None if unknown) so the
    record round-trips cleanly through JSON in data/conferences_db.json.
    """

    source: str  # e.g. "wikicfp"
    source_id: str  # stable id within that source (e.g. wikicfp eventid)
    title: str
    url: str  # official conference/organizer website (fallback: source detail page)
    cfp_url: str  # link to verify the CFP details (source detail page)
    location: Optional[str] = None
    mode: str = "unknown"  # "online" | "hybrid" | "onsite" | "unknown"
    topics: list[str] = field(default_factory=list)  # raw categories/tags from source
    matched_topics: list[str] = field(default_factory=list)  # our config topic names matched
    submission_deadline: Optional[str] = None
    notification_date: Optional[str] = None
    conference_start: Optional[str] = None
    conference_end: Optional[str] = None
    relevance_score: float = 0.0
    excluded: bool = False
    exclude_reason: Optional[str] = None
    first_seen: Optional[str] = None
    last_verified: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ConferenceRecord":
        known = {f: d[f] for f in ConferenceRecord.__dataclass_fields__ if f in d}
        return ConferenceRecord(**known)

    @property
    def dedup_key(self) -> str:
        return f"{self.source}:{self.source_id}"


def parse_iso_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=False)


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
