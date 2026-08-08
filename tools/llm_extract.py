"""LLM-based extraction for information WikiCFP's structured data doesn't
carry: registration fee and travel/accommodation support. Uses Groq's free
API (OpenAI-compatible chat completions) — chosen after both the user's
Gemini keys (two separate keys, same account) hit an identical account-level
"limit: 0" free-tier restriction unrelated to which key or model was tried,
and after weighing Claude (works, but not free — see workflow doc's known
edge cases for the full trail). Groq's free tier has no such restriction and
this is simple structured extraction, well within a mid-size open model's
reliable range.

Deliberately scoped to run once per conference, not once per pipeline run:
callers only invoke this for records that don't already have
fee_info/travel_support_info set (see dedupe_and_store.py, which carries
those fields forward on every re-fetch), so cost/quota usage scales with
new-conferences-per-day, not total-conferences-per-day.

Requires GROQ_API_KEY (GitHub Secret in production, .env locally). Skips
gracefully if unset — records simply keep the "not yet checked" default,
same pattern as every other optional integration in this project.

Run standalone for a quick manual check:
    python tools/llm_extract.py
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

from common import ConferenceRecord, PROJECT_ROOT
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "openai/gpt-oss-20b"  # right-sized for simple structured extraction; see
# console.groq.com/docs/deprecations before changing — llama-3.3-70b-versatile
# and llama-3.1-8b-instant retire 2026-08-16, don't reach for those.
NOT_STATED = "Not stated — check official site"
MAX_PAGE_CHARS = 6000
FETCH_TIMEOUT = 15
REQUEST_TIMEOUT = 30

# Free-tier limits for openai/gpt-oss-20b (console.groq.com/docs/rate-limits):
# 30 RPM, 8,000 TPM. TPM is the real binding constraint — a single extraction
# request (page text + instructions + reasoning tokens) typically runs
# 1,800-2,300 tokens, so only ~3-4 fit per minute. Discovered the hard way:
# the first live CI run (2026-08-08) fired 53 requests back-to-back with no
# pacing and nearly all of them 429'd. call_groq() now retries on 429,
# honoring the Retry-After header.
MAX_RETRIES = 3
RATE_LIMIT_DEFAULT_WAIT = 25  # seconds, fallback when Retry-After header is absent

FEE_TRAVEL_INSTRUCTIONS = (
    "Respond with ONLY a JSON object, no other text, matching exactly this shape:\n"
    '{"fee_info": "<string>", "travel_support_info": "<string>"}\n\n'
    "fee_info: a short factual statement about the registration/submission fee, "
    "based ONLY on what the page text states — e.g. 'Free for authors', "
    "'USD 300 early bird / USD 400 regular', 'Registration fee required, amount "
    f"not stated on this page'. If the page says nothing about fees, respond with "
    f"exactly: '{NOT_STATED}'.\n\n"
    "travel_support_info: a short factual statement about whether travel/"
    "accommodation is covered for accepted authors/presenters, based ONLY on what "
    "the page text states — e.g. 'Not covered, self-funded', 'Travel grant "
    "available for selected presenters'. If the page says nothing about travel or "
    f"accommodation support, respond with exactly: '{NOT_STATED}'.\n\n"
    "Do not guess or infer anything not directly stated in the text."
)


def get_api_key() -> Optional[str]:
    return os.environ.get("GROQ_API_KEY")


def fetch_page_text(url: str) -> Optional[str]:
    """Best-effort fetch of a conference's official homepage, stripped to
    visible text. Returns None on any failure — the caller skips the LLM
    call entirely rather than spending a request on an empty page.
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


def call_groq(api_key: str, user_content: str) -> Optional[dict]:
    """POST to Groq's chat completions endpoint with JSON-mode enabled.
    Returns the parsed JSON dict, or None on any failure (network, non-2xx
    after retries exhausted, or malformed JSON in the response) — callers
    treat that as "couldn't determine, use the not-stated default" rather
    than raising.
    """
    resp = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": MODEL,
                    "messages": [{"role": "user", "content": user_content}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.2,
                },
                timeout=REQUEST_TIMEOUT,
            )
        except requests.exceptions.RequestException as e:
            print(f"  WARNING: Groq request failed ({type(e).__name__}) — skipping.")
            return None

        if resp.status_code == 429:
            wait_s = int(resp.headers.get("retry-after", RATE_LIMIT_DEFAULT_WAIT))
            if attempt < MAX_RETRIES:
                print(f"  Groq rate-limited (429) — waiting {wait_s}s "
                      f"(retry {attempt}/{MAX_RETRIES - 1})...")
                time.sleep(wait_s)
                continue
            print(f"  WARNING: Groq still rate-limited after {MAX_RETRIES} attempts — skipping.")
            return None

        if not resp.ok:
            print(f"  WARNING: Groq request failed (HTTP {resp.status_code}: "
                  f"{resp.text[:200]!r}) — skipping.")
            return None

        break  # success

    try:
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except (KeyError, IndexError, json.JSONDecodeError):
        return None


def extract_fee_travel_info(record: ConferenceRecord, api_key: str) -> tuple[str, str]:
    """Returns (fee_info, travel_support_info). Falls back to NOT_STATED for
    both without spending a request if the official page can't be fetched.
    """
    page_text = fetch_page_text(record.url)
    if not page_text:
        return NOT_STATED, NOT_STATED

    data = call_groq(
        api_key,
        f"Conference: {record.title}\n"
        f"Official site text (may be incomplete/truncated):\n\n{page_text}\n\n"
        f"{FEE_TRAVEL_INSTRUCTIONS}",
    )
    if not data:
        return NOT_STATED, NOT_STATED
    return data.get("fee_info", NOT_STATED), data.get("travel_support_info", NOT_STATED)


MAX_RECORDS_PER_RUN = 30  # keeps one run's worst-case Groq spend bounded (30
# RPM free-tier limit) even after a large one-time backlog — e.g. the day
# GROQ_API_KEY is first set against an already-populated DB, every existing
# record is "pending" at once. Records not reached this run stay fee_info=None
# (dedupe_and_store carries that forward — see its docstring) and get picked
# up on a later day, prioritized by nearest deadline below.


def run(records: list[ConferenceRecord]) -> int:
    """Populate fee_info/travel_support_info for records that don't have it
    yet (mutates in place), capped at MAX_RECORDS_PER_RUN per run. Returns
    the number of records actually attempted this run.
    """
    api_key = get_api_key()
    if not api_key:
        print("GROQ_API_KEY not set — skipping fee/travel extraction "
              "(set it in .env for local runs or as a GitHub Actions secret).")
        return 0

    pending = [r for r in records if r.fee_info is None or r.travel_support_info is None]
    pending.sort(key=lambda r: r.submission_deadline or "9999-99-99")
    if len(pending) > MAX_RECORDS_PER_RUN:
        print(f"  {len(pending)} record(s) pending fee/travel extraction — "
              f"processing the {MAX_RECORDS_PER_RUN} nearest-deadline ones this "
              f"run, rest carry over to future runs.")
        pending = pending[:MAX_RECORDS_PER_RUN]

    for record in pending:
        fee_info, travel_info = extract_fee_travel_info(record, api_key)
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
