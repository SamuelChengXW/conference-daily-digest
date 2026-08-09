"""Playwright-based scraper for Universiti Malaya's own event management
system (umevent.um.edu.my) — a real, substantial source the rest of this
project's plain-requests approach can't reach: 665 total events, 45 for
2026 alone as of 2026-08-09. Found via a user-provided link after asking
"what are we missing" — see workflows/daily_conference_digest.md's
known-edge-cases entry for the full investigation.

Two things make this source different from every other fetcher here:

1. The listing renders via an AJAX endpoint (list.php, POST'd with a page
   number) rather than being present in the initial HTML — a plain
   requests.get() (like fetch_wikicfp.py uses) shows only the search UI and
   a result count, no rows. Needs a real browser.
2. The site's bot-protection blocks a *default* headless Chromium outright
   ("The URL you requested has been blocked"). Worked around by disabling
   the automation-detection flag and setting a realistic desktop
   user-agent/viewport — verified live 2026-08-09. GitHub Actions' shared
   IPs are commonly *more* distrusted by WAFs than the residential IP this
   was tested from, so this may behave differently in CI. Every failure
   mode here returns [] rather than raising — a UM scrape problem must
   never crash the whole pipeline, same principle as every other source.

Once rendered, the listing itself is structured HTML (title, own URL,
event date range) — parsed deterministically with BeautifulSoup, no LLM
needed for this step (CLAUDE.md: "deterministic code handles execution").
UM's system covers ALL event types (workshops, seminars, dinners, not just
CFPs), so fetch_wikicfp.py's existing prefilter_relevant() runs first
(reused as-is — it already checks title+description against config topics
and the Malaysia+AI carve-out, and every UM event is Malaysia-located by
definition, so that branch collapses to "is this AI/CS-relevant"). Only
surviving candidates get their own external page fetched + extracted via
the exact same llm_extract.fetch_page_text()/call_groq() and
fetch_search_api.EXTRACTION_INSTRUCTIONS already used for Malaysia search
results — the listing page has event dates but not a submission deadline,
so this step is what confirms a real CFP and pulls
submission_deadline/fee/travel info.

That enrichment step costs a Groq request per candidate, so — mirroring
llm_extract.py's MAX_RECORDS_PER_RUN reasoning — already-known event IDs
(checked against data/conferences_db.json) are skipped, and new ones are
capped per run; the rest carry over to a future run.

Requires GROQ_API_KEY (for enrichment) and the playwright package + a
downloaded Chromium (`playwright install chromium`). Skips gracefully to
[] if either is missing, same pattern as every other optional integration.

Run standalone for a quick manual check:
    python tools/fetch_um_events.py
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from bs4 import BeautifulSoup
from dotenv import load_dotenv

from common import ConferenceRecord, DB_PATH, PROJECT_ROOT, load_config, read_json, today
import fetch_search_api
import fetch_wikicfp
import llm_extract

load_dotenv(PROJECT_ROOT / ".env")

BASE = "https://umevent.um.edu.my"
SEARCH_URL = f"{BASE}/search/"
LIST_URL = f"{BASE}/search/list.php"

# A default headless Chromium gets an outright WAF block on this site
# ("The URL you requested has been blocked"). Disabling the
# automation-detection flag plus a realistic desktop UA/viewport gets a
# real "UM Event System" page instead — verified live 2026-08-09.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
VIEWPORT = {"width": 1366, "height": 768}

MAX_PAGES_PER_YEAR = 10  # safety cap; observed 45 events/2026 = 3 pages of 15 (2026-08-09)
MAX_NEW_ENRICHED_PER_RUN = 15  # Groq-cost-bearing step; only genuinely new event IDs spend
# a request — see module docstring. Rest carry over to a future run.

DATE_RE = re.compile(r"Date\s*:\s*(\d{2}/\d{2}/\d{4})-(\d{2}/\d{2}/\d{4})")
KONID_RE = re.compile(r"konid=([\w-]+)")


def _to_iso(dmy: str) -> Optional[str]:
    try:
        return datetime.strptime(dmy, "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def _parse_listing_page(html: str) -> list[dict]:
    """Parses one page of the (already-rendered) results list. Each event
    lives in a div.col-md-7 with two <li>s: the first holds the UM system's
    own detail-view link (its query string carries the stable `konid`), the
    second holds the event's own external title-link, a short description,
    and a "Date : DD/MM/YYYY-DD/MM/YYYY" line (event dates, not a
    submission deadline — that has to come from the event's own page).
    """
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for block in soup.select("div.col-md-7"):
        lis = block.find_all("li", recursive=False)
        if len(lis) < 2:
            continue

        detail_a = lis[0].find("a")
        konid = None
        if detail_a and detail_a.get("href"):
            m = KONID_RE.search(detail_a["href"])
            konid = m.group(1) if m else None
        if not konid:
            continue

        title_a = lis[1].find("a")
        if not title_a:
            continue
        title = title_a.get_text(strip=True)
        own_url = (title_a.get("href") or "").strip()
        if not title or not own_url:
            continue

        divs = lis[1].find_all("div")
        description = divs[1].get_text(" ", strip=True) if len(divs) >= 2 else ""

        date_match = DATE_RE.search(lis[1].get_text(" ", strip=True))
        event_start = _to_iso(date_match.group(1)) if date_match else None
        event_end = _to_iso(date_match.group(2)) if date_match else None

        items.append({
            "konid": konid,
            "title": title,
            "url": own_url,
            "description": description,
            "event_start": event_start,
            "event_end": event_end,
        })
    return items


def _launch_context(playwright):
    browser = playwright.chromium.launch(args=LAUNCH_ARGS)
    context = browser.new_context(user_agent=USER_AGENT, viewport=VIEWPORT, locale="en-US")
    return browser, context


def fetch_year_listing(context, year: int) -> list[dict]:
    """Fetches every page of the year-filtered listing. Returns [] on any
    failure — a UM scrape problem must never crash the whole pipeline.
    """
    search_url = f"{SEARCH_URL}?doproc=1&year={year}"
    page = context.new_page()
    try:
        page.goto(search_url, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        if "blocked" in page.title().lower():
            print(f"  WARNING: UM Event System blocked this request (year={year}) — skipping.")
            return []
    except Exception as e:
        print(f"  WARNING: UM Event System navigation failed for year {year} "
              f"({type(e).__name__}) — skipping.")
        return []
    finally:
        page.close()

    all_items: list[dict] = []
    seen_konids: set[str] = set()
    for page_num in range(1, MAX_PAGES_PER_YEAR + 1):
        try:
            resp = context.request.post(
                f"{LIST_URL}?title=&year={year}",
                form={"page": str(page_num)},
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": search_url},
                timeout=20000,
            )
            if not resp.ok:
                break
            html = resp.text()
        except Exception as e:
            print(f"  WARNING: UM Event System page {page_num} fetch failed for year "
                  f"{year} ({type(e).__name__}) — stopping pagination here.")
            break

        items = _parse_listing_page(html)
        new_items = [it for it in items if it["konid"] not in seen_konids]
        if not new_items:
            break
        seen_konids.update(it["konid"] for it in new_items)
        all_items.extend(new_items)

    return all_items


def _extract_um_event(item: dict, groq_api_key: str) -> Optional[ConferenceRecord]:
    """Confirms a real CFP and pulls submission_deadline/fee/travel info by
    visiting the event's own page — reuses fetch_search_api.py's exact
    extraction path (same prompt, same call_groq()), since this is the
    identical "unstructured page -> structured CFP fields" problem.
    """
    page_text = llm_extract.fetch_page_text(item["url"])
    if not page_text:
        return None

    data = llm_extract.call_groq(
        groq_api_key,
        f"Search result title: {item['title']}\n"
        f"Search result snippet: {item['description']}\n"
        f"Page URL: {item['url']}\n"
        f"Page text (may be incomplete/truncated):\n\n{page_text}\n\n"
        f"{fetch_search_api.EXTRACTION_INSTRUCTIONS}",
    )
    if not data:
        return None
    if not data.get("is_conference_cfp") or not data.get("submission_deadline"):
        return None

    today_iso = today().isoformat()
    return ConferenceRecord(
        source="um_event_system",
        source_id=item["konid"],
        title=data.get("title") or item["title"],
        url=item["url"],
        cfp_url=item["url"],
        # Floor default — every event on this system is UM's own, Kuala Lumpur campus.
        # classify_relevance.py's region boost text-matches this field, so keeping it
        # populated even when the event's own page doesn't restate "Malaysia" matters.
        location=data.get("location") or "Kuala Lumpur, Malaysia",
        mode="unknown",
        topics=data.get("topics", []) or [],
        submission_deadline=data.get("submission_deadline"),
        conference_start=data.get("conference_start") or item.get("event_start"),
        conference_end=data.get("conference_end") or item.get("event_end"),
        fee_info=data.get("fee_info"),
        travel_support_info=data.get("travel_support_info"),
        first_seen=today_iso,
        last_verified=today_iso,
    )


def run(config: Optional[dict] = None) -> list[ConferenceRecord]:
    config = config or load_config()
    um_cfg = config.get("um_event_system", {})
    if not um_cfg.get("enabled", True):
        print("um_event_system.enabled is false in config — skipping UM Event System.")
        return []

    groq_key = llm_extract.get_api_key()
    if not groq_key:
        print("GROQ_API_KEY not set — skipping UM Event System (needed to confirm CFPs).")
        return []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed — skipping UM Event System "
              "(pip install playwright && playwright install chromium).")
        return []

    current_year = today().year
    years = [current_year, current_year + 1]

    try:
        with sync_playwright() as p:
            browser, context = _launch_context(p)
            try:
                candidates: list[dict] = []
                for year in years:
                    candidates.extend(fetch_year_listing(context, year))
            finally:
                browser.close()
    except Exception as e:
        print(f"  WARNING: UM Event System scrape failed ({type(e).__name__}) — skipping.")
        return []

    print(f"  -> {len(candidates)} candidate event(s) found across {years}")

    relevant = [
        c for c in candidates
        if fetch_wikicfp.prefilter_relevant(
            {"title": c["title"], "description": c["description"]}, config
        )
    ]
    print(f"  -> {len(relevant)} pass the topic/Malaysia-AI prefilter")

    existing_db = read_json(DB_PATH, default={})
    known_konids = {
        v["source_id"] for v in existing_db.values() if v.get("source") == "um_event_system"
    }
    new_candidates = [c for c in relevant if c["konid"] not in known_konids]
    if len(new_candidates) > MAX_NEW_ENRICHED_PER_RUN:
        print(f"  {len(new_candidates)} new candidate(s) — enriching the first "
              f"{MAX_NEW_ENRICHED_PER_RUN} this run, rest picked up next run.")
        new_candidates = new_candidates[:MAX_NEW_ENRICHED_PER_RUN]

    records = []
    for item in new_candidates:
        record = _extract_um_event(item, groq_key)
        if record:
            records.append(record)

    return records


if __name__ == "__main__":
    cfg = load_config()
    results = run(cfg)
    print(f"Found {len(results)} UM Event System conference record(s):")
    for r in results:
        print(f"  {r.submission_deadline}  {r.title[:70]}")
