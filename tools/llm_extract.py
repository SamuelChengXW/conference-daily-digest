"""Shared Groq helpers: fetch_page_text() and call_groq() (JSON mode, with
429 retry/backoff), used by extract_access.py, fetch_search_api.py and
fetch_um_events.py.

This module originally also did homepage-only fee/travel extraction (its
own run()). That was replaced on 2026-10-13 by extract_access.py, because
across 228 stored records it found a fee for just 10 and travel support for
just 3: fees and grants live on Registration / Travel Grant subpages, not
homepages. See workflows/daily_conference_digest.md.

Provider history: Groq's free API (OpenAI-compatible chat completions) was
chosen after both the user's Gemini keys (two separate keys, same account)
hit an identical account-level "limit: 0" free-tier restriction, and after
weighing Claude (works, but not free) — see the workflow doc's known edge
cases for the full trail.

Requires GROQ_API_KEY (GitHub Secret in production, .env locally); callers
skip gracefully if unset.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

from common import PROJECT_ROOT
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
# honoring the Retry-After header. Callers bound their own per-run volume
# (extract_access.MAX_RECORDS_PER_RUN, search_api.max_queries_per_run).
MAX_RETRIES = 3
RATE_LIMIT_DEFAULT_WAIT = 25  # seconds, fallback when Retry-After header is absent
MAX_RATE_LIMIT_WAIT = 60  # hard ceiling on a single Retry-After-driven sleep — a header
# reporting something far larger (e.g. reflecting a daily-quota-scale reset rather than
# the per-minute TPM window this was designed around) should mean "give up on this call
# and let the record carry over to a future run" (see extract_access.py's
# MAX_RECORDS_PER_RUN), not "block this step for however long the header says."

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
            if wait_s > MAX_RATE_LIMIT_WAIT:
                print(f"  WARNING: Groq rate-limited (429) with an unusually long "
                      f"Retry-After ({wait_s}s > {MAX_RATE_LIMIT_WAIT}s cap) — skipping "
                      f"rather than blocking this long.")
                return None
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
