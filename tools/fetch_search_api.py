"""Serper.dev search + Groq extraction: finds opportunities WikiCFP doesn't
index. Since the 2026-10-13 free & funded redesign, config's
search_api.priority_queries search for free/fee-waivable/funded
conferences, no-APC journal calls, and standalone travel grants directly
and run every time; the older rotating CFP pool below fills in behind them.
Cost/funding status is NOT read from this pass — every record found here
goes through extract_access.py's evidence-checked verdict afterward.

History: Started as Malaysian-university-only (UM/UKM/USM/UPM/UTM etc.),
broadened 2026-08-08 to also cover Japan and general energy-society/journal
CFPs, then broadened again 2026-08-09 with Malaysia professional-body sites
(ieeemy.org, myiem.org.my) and a curated list of known recurring Malaysia
conference series — see config/filters.yaml's search_api.queries for the
current query list, grouped by category with comments.

Direct scraping of the Malaysian sources was investigated and rejected —
alternative conference aggregators are bot-blocked (403 on every request,
unrelated to their permissive robots.txt), and the one centralized
university conference hub found (conference.utm.my) has a dead RSS feed and
stale placeholder content. This is the sustainable replacement: targeted
search queries + LLM extraction from each result's actual page content.

Requires SERPER_API_KEY and GROQ_API_KEY (both GitHub Secrets in
production, .env locally). Skips gracefully if either is unset.

Cost/quota discipline: capped at max_queries_per_run queries per run
(config), RESULTS_PER_QUERY results examined per query — each examined
result costs one page fetch + one Groq request, so total Groq spend per run
is bounded at max_queries_per_run * RESULTS_PER_QUERY calls, worst case.
Once the query pool grew past max_queries_per_run, rotate_queries() picks a
different day-of-year-shifted window each run instead of always the same
fixed prefix, so the full pool eventually gets covered rather than the tail
entries never running at all (a real gap in the earlier fixed-prefix
behavior). How long "eventually" takes depends on run frequency, not just
pool size: at the original daily cadence this cycled every ~few days; at
the current weekly cadence (2026-08-19) a full cycle over the 47-query pool
takes about 10 weeks, since each week's window only shifts a few positions
rather than a clean max_queries_per_run-sized step. Slower discovery is the
direct, expected tradeoff of a less frequent run, not a bug.

Run standalone for a quick manual check:
    python tools/fetch_search_api.py
"""
from __future__ import annotations

import hashlib
import os
from typing import Optional

import requests
from dotenv import load_dotenv

from common import ConferenceRecord, DB_PATH, PROJECT_ROOT, load_config, read_json, today
import llm_extract

load_dotenv(PROJECT_ROOT / ".env")

SERPER_URL = "https://google.serper.dev/search"
RESULTS_PER_QUERY = 3

EXTRACTION_INSTRUCTIONS = (
    "Respond with ONLY a JSON object, no other text, matching exactly this shape "
    "(use null for any date/location field you can't determine):\n"
    '{"is_opportunity": <bool>, "opportunity_type": "conference|journal_issue|funding_program", '
    '"title": "<string>", '
    '"submission_deadline": "<YYYY-MM-DD or null>", '
    '"funding_deadline": "<YYYY-MM-DD or null>", '
    '"conference_start": "<YYYY-MM-DD or null>", "conference_end": "<YYYY-MM-DD or null>", '
    '"location": "<string or null>", "topics": ["<string>", ...]}\n\n'
    "is_opportunity: true ONLY if this page is ONE specific, current academic "
    "opportunity: a conference/workshop call for papers (opportunity_type="
    "conference), a journal special-issue call for papers (journal_issue), or a "
    "travel grant / scholarship / fellowship program researchers or students "
    "can apply to (funding_program). False for news items, lists/round-ups of "
    "many opportunities, non-academic events, or anything you can't confirm. "
    "Be conservative — if unsure, false.\n\n"
    "submission_deadline: the paper/abstract submission deadline. "
    "funding_deadline: the application deadline for a grant/scholarship/travel "
    "support, if any. Both as YYYY-MM-DD, null if not clearly stated — do not "
    "guess or infer a date.\n\n"
    "Do not guess or infer anything not directly stated in the text."
)


# Google time filter for priority_queries: past month only. The first live
# run (2026-10-13) found 16 records from those queries, but most were
# last-cycle pages whose deadlines had already passed (e.g. AICEE x ACCEPT
# 2026, closed 16 Aug) — "2027" in the query text alone doesn't keep
# Google from ranking older pages first.
PRIORITY_TBS = "qdr:m"


def search(query: str, api_key: str, tbs: Optional[str] = None) -> list[dict]:
    resp = requests.post(
        SERPER_URL,
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        json={"q": query, **({"tbs": tbs} if tbs else {})},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json().get("organic", [])


def extract_conference_record(result: dict, groq_api_key: str) -> Optional[ConferenceRecord]:
    url = result.get("link")
    if not url:
        return None

    page_text = llm_extract.fetch_page_text(url)
    if not page_text:
        return None

    data = llm_extract.call_groq(
        groq_api_key,
        f"Search result title: {result.get('title', '')}\n"
        f"Search result snippet: {result.get('snippet', '')}\n"
        f"Page URL: {url}\n"
        f"Page text (may be incomplete/truncated):\n\n{page_text}\n\n"
        f"{EXTRACTION_INSTRUCTIONS}",
    )
    if not data:
        return None

    if not data.get("is_opportunity"):
        return None
    opp_type = data.get("opportunity_type")
    if opp_type not in {"conference", "journal_issue", "funding_program"}:
        opp_type = "conference"
    # Needs SOME actionable deadline: a paper deadline for a CFP, an
    # application deadline for a funding program (a search hit with no date
    # is too often an evergreen round-up page to be worth tracking).
    deadline_field = "funding_deadline" if opp_type == "funding_program" else "submission_deadline"
    if not data.get(deadline_field):
        return None

    source_id = hashlib.sha1(url.encode()).hexdigest()[:16]
    today_iso = today().isoformat()
    return ConferenceRecord(
        source="search_malaysia",
        source_id=source_id,
        title=data.get("title") or result.get("title", url),
        url=url,
        cfp_url=url,  # no separate aggregator detail page for search-sourced records
        location=data.get("location"),
        mode="unknown",
        topics=data.get("topics", []) or [],
        submission_deadline=data.get("submission_deadline"),
        funding_deadline=data.get("funding_deadline"),
        conference_start=data.get("conference_start"),
        conference_end=data.get("conference_end"),
        opportunity_type=opp_type,
        # Cost/funding are NOT taken from this pass — extract_access.py
        # verifies them (with a verbatim evidence quote) on the next step.
        first_seen=today_iso,
        last_verified=today_iso,
    )


def rotate_queries(queries: list[str], max_per_run: int) -> list[str]:
    """Returns a max_per_run-sized, wrapping window into queries, shifted by
    day-of-year so the full pool gets covered over multiple days instead of
    a fixed prefix that silently never reaches queries past index
    max_per_run - 1. E.g. with 25 queries and max_per_run=14: day N covers
    indices [14N % 25, ...), day N+1 picks up right after, wrapping around
    to index 0 partway through — the full 25 gets touched within 2 days,
    then the cycle repeats. Daily Serper/Groq spend stays bounded at
    max_per_run either way (see the cost/runtime note in
    config/filters.yaml above search_api).
    """
    if not queries or max_per_run <= 0:
        return []
    if len(queries) <= max_per_run:
        return queries

    day_of_year = today().timetuple().tm_yday
    start = (day_of_year * max_per_run) % len(queries)
    window = [queries[(start + i) % len(queries)] for i in range(max_per_run)]
    return window


def run(config: Optional[dict] = None) -> list[ConferenceRecord]:
    config = config or load_config()
    search_cfg = config.get("search_api", {})

    if not search_cfg.get("enabled"):
        print("search_api.enabled is false in config — skipping search source.")
        return []

    serper_key = os.environ.get("SERPER_API_KEY")
    groq_key = llm_extract.get_api_key()  # reuses the same GROQ_API_KEY check
    if not serper_key or not groq_key:
        print("SERPER_API_KEY / GROQ_API_KEY not set — skipping search source "
              "(set both in .env for local runs or as GitHub Actions secrets).")
        return []

    # priority_queries (free/funded-first searches) always run; the older
    # rotating pool fills in behind them.
    priority = list(search_cfg.get("priority_queries", []))
    queries = priority + rotate_queries(
        search_cfg.get("queries", []), search_cfg.get("max_queries_per_run", 8)
    )

    # Pages already stored are skipped before spending a Groq call on them —
    # every-run priority queries keep returning the same pages week to week,
    # and Groq's free tier is 200K tokens/day per model (see llm_extract).
    known_ids = {
        v["source_id"] for v in read_json(DB_PATH, default={}).values()
        if v.get("source") == "search_malaysia"
    }
    seen_urls: set[str] = set()
    records: list[ConferenceRecord] = []
    for query in queries:
        if llm_extract.model_exhausted():
            print("  Groq daily quota spent — skipping remaining search queries this run.")
            break
        try:
            tbs = PRIORITY_TBS if query in priority else None
            try:
                results = search(query, serper_key, tbs)
            except requests.exceptions.HTTPError:
                if not tbs:
                    raise
                # Never lose a priority query to the date filter itself.
                results = search(query, serper_key)
        except requests.exceptions.RequestException as e:
            print(f"  WARNING: Serper search failed for query '{query}' ({type(e).__name__}) — skipping.")
            continue

        for result in results[:RESULTS_PER_QUERY]:
            url = result.get("link")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            if hashlib.sha1(url.encode()).hexdigest()[:16] in known_ids:
                continue

            record = extract_conference_record(result, groq_key)
            if record:
                records.append(record)

    return records


if __name__ == "__main__":
    cfg = load_config()
    results = run(cfg)
    print(f"Found {len(results)} opportunity record(s) via search:")
    for r in results:
        print(f"  [{r.opportunity_type}] {r.submission_deadline or r.funding_deadline}  {r.title[:70]}")
