"""LLM-based extraction for information WikiCFP's structured data doesn't
carry: registration fee and travel/accommodation support. Uses the Claude
API (Messages API, structured outputs) — NOT free, so this is deliberately
scoped to run once per conference, not once per pipeline run: callers only
invoke this for records that don't already have fee_info/travel_support_info
set (see dedupe_and_store.py, which now carries those fields forward on
every re-fetch), so cost scales with new-conferences-per-day, not
total-conferences-per-day.

Requires ANTHROPIC_API_KEY (GitHub Secret in production, .env locally).
Skips gracefully if unset — records simply keep the "not yet checked"
default, same pattern as every other optional integration in this project.

Run standalone for a quick manual check:
    python tools/llm_extract.py
"""
from __future__ import annotations

import os
from typing import Optional

import anthropic
import requests
from bs4 import BeautifulSoup

from common import ConferenceRecord, PROJECT_ROOT
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

MODEL = "claude-opus-5"
NOT_STATED = "Not stated — check official site"
MAX_PAGE_CHARS = 6000
FETCH_TIMEOUT = 15

FEE_TRAVEL_SCHEMA = {
    "type": "object",
    "properties": {
        "fee_info": {
            "type": "string",
            "description": (
                "A short factual statement about the registration/submission fee, "
                "based ONLY on what the page text states — e.g. 'Free for authors', "
                "'USD 300 early bird / USD 400 regular', 'Registration fee required, "
                "amount not stated on this page'. If the page says nothing about "
                f"fees, respond with exactly: '{NOT_STATED}'."
            ),
        },
        "travel_support_info": {
            "type": "string",
            "description": (
                "A short factual statement about whether travel/accommodation is "
                "covered for accepted authors/presenters, based ONLY on what the "
                "page text states — e.g. 'Not covered, self-funded', 'Travel grant "
                "available for selected presenters', 'Accommodation provided for "
                "invited speakers only'. If the page says nothing about travel or "
                f"accommodation support, respond with exactly: '{NOT_STATED}'."
            ),
        },
    },
    "required": ["fee_info", "travel_support_info"],
    "additionalProperties": False,
}


def get_client() -> Optional[anthropic.Anthropic]:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    return anthropic.Anthropic()


def fetch_page_text(url: str) -> Optional[str]:
    """Best-effort fetch of a conference's official homepage, stripped to
    visible text. Returns None on any failure — the caller skips the LLM
    call entirely rather than spending tokens on an empty page.
    """
    if not url:
        return None
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ConferenceDigestBot/1.0)"},
            timeout=FETCH_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    text = " ".join(soup.get_text(" ", strip=True).split())
    return text[:MAX_PAGE_CHARS] if text else None


def extract_fee_travel_info(record: ConferenceRecord, client: anthropic.Anthropic) -> tuple[str, str]:
    """Returns (fee_info, travel_support_info). Falls back to NOT_STATED for
    both without spending an API call if the official page can't be fetched.
    """
    page_text = fetch_page_text(record.url)
    if not page_text:
        return NOT_STATED, NOT_STATED

    response = client.messages.create(
        model=MODEL,
        max_tokens=512,
        output_config={
            "effort": "low",  # simple, mechanical extraction — not a model downgrade
            "format": {"type": "json_schema", "schema": FEE_TRAVEL_SCHEMA},
        },
        messages=[{
            "role": "user",
            "content": (
                f"Conference: {record.title}\n"
                f"Official site text (may be incomplete/truncated):\n\n{page_text}\n\n"
                "Extract the registration fee and travel/accommodation support "
                "info per the schema. Do not guess or infer amounts that aren't "
                "stated — use the not-stated default whenever the page doesn't "
                "say."
            ),
        }],
    )
    text_block = next((b.text for b in response.content if b.type == "text"), None)
    if not text_block:
        return NOT_STATED, NOT_STATED

    import json
    try:
        data = json.loads(text_block)
        return data.get("fee_info", NOT_STATED), data.get("travel_support_info", NOT_STATED)
    except (json.JSONDecodeError, AttributeError):
        return NOT_STATED, NOT_STATED


def run(records: list[ConferenceRecord]) -> int:
    """Populate fee_info/travel_support_info for records that don't have it
    yet (mutates in place). Returns the number of records actually processed
    (i.e. that spent an API call) so the pipeline log can report it.
    """
    client = get_client()
    if not client:
        print("ANTHROPIC_API_KEY not set — skipping fee/travel extraction "
              "(set it in .env for local runs or as a GitHub Actions secret).")
        return 0

    pending = [r for r in records if r.fee_info is None or r.travel_support_info is None]
    for record in pending:
        fee_info, travel_info = extract_fee_travel_info(record, client)
        record.fee_info = fee_info
        record.travel_support_info = travel_info

    return len(pending)


if __name__ == "__main__":
    from common import DB_PATH, read_json
    import dedupe_and_store

    raw = read_json(DB_PATH, default={})
    records = [ConferenceRecord.from_dict(v) for v in raw.values()][:3]  # small manual test batch
    n = run(records)
    print(f"Processed {n} record(s):")
    for r in records:
        print(f"  {r.title[:60]}\n    fee: {r.fee_info}\n    travel: {r.travel_support_info}")
    if n:
        dedupe_and_store.save_db({r.dedup_key: r for r in records})
