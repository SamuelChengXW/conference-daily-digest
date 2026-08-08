"""Serper.dev search + Claude extraction: finds Malaysian university
conferences (UM/UKM/USM/UPM/UTM etc.) that WikiCFP doesn't index.

Direct scraping of these sources was investigated and rejected — alternative
conference aggregators are bot-blocked (403 on every request, unrelated to
their permissive robots.txt), and the one centralized university conference
hub found (conference.utm.my) has a dead RSS feed and stale placeholder
content. This is the sustainable replacement: targeted search queries +
LLM extraction from each result's actual page content.

Requires SERPER_API_KEY and ANTHROPIC_API_KEY (both GitHub Secrets in
production, .env locally). Skips gracefully if either is unset.

Cost/quota discipline: capped at max_queries_per_run queries (config),
RESULTS_PER_QUERY results examined per query — each examined result costs
one page fetch + one Claude call, so total Claude spend per run is bounded
at max_queries_per_run * RESULTS_PER_QUERY calls, worst case.

Run standalone for a quick manual check:
    python tools/fetch_search_api.py
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

import anthropic
import requests
from dotenv import load_dotenv

from common import ConferenceRecord, PROJECT_ROOT, load_config, today
import llm_extract

load_dotenv(PROJECT_ROOT / ".env")

SERPER_URL = "https://google.serper.dev/search"
RESULTS_PER_QUERY = 3
MODEL = "claude-opus-5"

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "is_conference_cfp": {
            "type": "boolean",
            "description": (
                "True only if this page is an academic conference, workshop, or "
                "journal special-issue call for papers with an actual paper "
                "submission process. False for university news items, general "
                "event listings, non-academic events, or pages you can't "
                "confirm are a genuine CFP."
            ),
        },
        "title": {"type": "string", "description": "The conference/CFP name."},
        "submission_deadline": {
            "type": ["string", "null"],
            "description": (
                "Paper/abstract submission deadline as YYYY-MM-DD. Null if not "
                "clearly stated in the page text — do not guess or infer a date."
            ),
        },
        "conference_start": {"type": ["string", "null"], "description": "YYYY-MM-DD or null."},
        "conference_end": {"type": ["string", "null"], "description": "YYYY-MM-DD or null."},
        "location": {"type": ["string", "null"], "description": "City, country, or 'Online'."},
        "topics": {
            "type": "array", "items": {"type": "string"},
            "description": "Short topic/keyword tags for the conference subject matter.",
        },
        "fee_info": {
            "type": "string",
            "description": f"Same rules as elsewhere: state only what's in the text, else '{llm_extract.NOT_STATED}'.",
        },
        "travel_support_info": {
            "type": "string",
            "description": f"Same rules as elsewhere: state only what's in the text, else '{llm_extract.NOT_STATED}'.",
        },
    },
    "required": [
        "is_conference_cfp", "title", "submission_deadline", "conference_start",
        "conference_end", "location", "topics", "fee_info", "travel_support_info",
    ],
    "additionalProperties": False,
}


def search(query: str, api_key: str) -> list[dict]:
    resp = requests.post(
        SERPER_URL,
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        json={"q": query},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json().get("organic", [])


def extract_conference_record(result: dict, client: anthropic.Anthropic) -> Optional[ConferenceRecord]:
    url = result.get("link")
    if not url:
        return None

    page_text = llm_extract.fetch_page_text(url)
    if not page_text:
        return None

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA},
        },
        messages=[{
            "role": "user",
            "content": (
                f"Search result title: {result.get('title', '')}\n"
                f"Search result snippet: {result.get('snippet', '')}\n"
                f"Page URL: {url}\n"
                f"Page text (may be incomplete/truncated):\n\n{page_text}\n\n"
                "Determine whether this is a genuine academic conference/CFP "
                "page and extract the fields per the schema. Be conservative — "
                "if you're not confident this is a real CFP with an actual "
                "submission process, set is_conference_cfp to false."
            ),
        }],
    )
    text_block = next((b.text for b in response.content if b.type == "text"), None)
    if not text_block:
        return None

    try:
        data = json.loads(text_block)
    except json.JSONDecodeError:
        return None

    if not data.get("is_conference_cfp") or not data.get("submission_deadline"):
        return None

    source_id = hashlib.sha1(url.encode()).hexdigest()[:16]
    today_iso = today().isoformat()
    return ConferenceRecord(
        source="search_malaysia",
        source_id=source_id,
        title=data["title"] or result.get("title", url),
        url=url,
        cfp_url=url,  # no separate aggregator detail page for search-sourced records
        location=data.get("location"),
        mode="unknown",
        topics=data.get("topics", []),
        submission_deadline=data.get("submission_deadline"),
        conference_start=data.get("conference_start"),
        conference_end=data.get("conference_end"),
        fee_info=data.get("fee_info"),
        travel_support_info=data.get("travel_support_info"),
        first_seen=today_iso,
        last_verified=today_iso,
    )


def run(config: Optional[dict] = None) -> list[ConferenceRecord]:
    config = config or load_config()
    search_cfg = config.get("search_api", {})

    if not search_cfg.get("enabled"):
        print("search_api.enabled is false in config — skipping Malaysia search source.")
        return []

    serper_key = os.environ.get("SERPER_API_KEY")
    claude_client = llm_extract.get_client()  # reuses the same ANTHROPIC_API_KEY check
    if not serper_key or not claude_client:
        print("SERPER_API_KEY / ANTHROPIC_API_KEY not set — skipping Malaysia search source "
              "(set both in .env for local runs or as GitHub Actions secrets).")
        return []

    queries = search_cfg.get("queries", [])[: search_cfg.get("max_queries_per_run", 8)]

    seen_urls: set[str] = set()
    records: list[ConferenceRecord] = []
    for query in queries:
        try:
            results = search(query, serper_key)
        except requests.exceptions.RequestException as e:
            print(f"  WARNING: Serper search failed for query '{query}' ({type(e).__name__}) — skipping.")
            continue

        for result in results[:RESULTS_PER_QUERY]:
            url = result.get("link")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            record = extract_conference_record(result, claude_client)
            if record:
                records.append(record)

    return records


if __name__ == "__main__":
    cfg = load_config()
    results = run(cfg)
    print(f"Found {len(results)} Malaysia conference record(s) via search:")
    for r in results:
        print(f"  {r.submission_deadline}  {r.title[:70]}  ({r.location})")
