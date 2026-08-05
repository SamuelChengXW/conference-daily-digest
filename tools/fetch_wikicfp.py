"""Fetch raw conference listings from WikiCFP.

WikiCFP's robots.txt (checked 2026-08-05) allows crawling for generic user
agents with `Crawl-delay: 5`. We respect that with a 5s sleep between every
HTTP request to wikicfp.com (RSS calls and detail-page calls alike).

Two-stage fetch, to keep total requests per run low:
  1. RSS feeds (config-driven `wikicfp_categories`) — cheap, one request per
     feed, gives title/link/short description for many items.
  2. Detail-page fetch — only for items that pass `prefilter_relevant()`,
     since this is where the actual deadline data lives (WikiCFP's RSS
     descriptions don't include submission deadlines).

IMPORTANT (found 2026-08 while investigating why the digest wasn't growing):
`wikicfp.com/cfp/rss?q=...` — the "keyword search" RSS endpoint — does NOT
actually filter by query. Verified directly: `?q=renewable+energy` and
`?q=Malaysia` return byte-for-byte the same 50 "most recent" items regardless
of query text. It was previously wired up as a second discovery mechanism
alongside categories and silently contributed nothing beyond the first query
in the list (every later query's items were all already-seen duplicates).
`wikicfp.com/cfp/rss?cat=...` does NOT have a fixed taxonomy, though —
confirmed it accepts arbitrary strings and does real substring matching
against each listing's tags (e.g. `cat=malaysia`, `cat=green+energy`,
`cat=energy+security` all return distinct, correctly-filtered results). So
`cat=` is now the *only* discovery mechanism this module uses — for both
topics and one-off keywords/regions, just add another entry to
`wikicfp_categories` in config/filters.yaml, whatever the phrase is. There's
no need for a real WikiCFP-published category to exist first, and no
separate keyword-query config key.

Detail pages carry hCalendar/RDFa microformat spans (`v:startDate`,
`v:summary`, `v:locality`, `dc:source`, ...) which are far more stable to
parse than the surrounding HTML layout.

Run standalone for a quick manual check:
    python tools/fetch_wikicfp.py
"""
from __future__ import annotations

import re
import time
from typing import Optional

import feedparser
import requests
from bs4 import BeautifulSoup

from common import CONFIG_PATH, TMP_DIR, load_config, write_json

BASE = "http://www.wikicfp.com"
USER_AGENT = "Mozilla/5.0 (compatible; ConferenceDigestBot/1.0; +https://github.com/)"
CRAWL_DELAY_SECONDS = 5

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})
_last_request_time = 0.0


def _throttled_get(url: str, timeout: int = 20) -> requests.Response:
    """GET with a hard floor of CRAWL_DELAY_SECONDS between requests to wikicfp.com."""
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < CRAWL_DELAY_SECONDS:
        time.sleep(CRAWL_DELAY_SECONDS - elapsed)
    resp = _session.get(url, timeout=timeout)
    _last_request_time = time.monotonic()
    return resp


def fetch_category_rss(category_slug: str) -> list[dict]:
    """`cat=` does real substring matching against each listing's tags —
    despite the name, this works fine for arbitrary keywords/regions too
    (see module docstring), not just WikiCFP's own published categories.
    """
    url = f"{BASE}/cfp/rss?cat={requests.utils.quote(category_slug)}"
    resp = _throttled_get(url)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    return [_rss_entry_to_raw(e, source_query=f"cat:{category_slug}") for e in feed.entries]


def _rss_entry_to_raw(entry, source_query: str) -> dict:
    return {
        "title": entry.get("title", "").strip(),
        "wikicfp_link": entry.get("link", "").strip(),
        "description": entry.get("description", "").strip(),
        "source_query": source_query,
    }


EVENTID_RE = re.compile(r"eventid=(\d+)")


def extract_eventid(wikicfp_link: str) -> Optional[str]:
    m = EVENTID_RE.search(wikicfp_link)
    return m.group(1) if m else None


def prefilter_relevant(raw_item: dict, config: dict) -> bool:
    """Cheap keyword gate on the RSS title+description, before spending a
    detail-page request. Any single keyword hit from any configured topic
    is enough to pass — real scoring happens later in classify_relevance.py.
    """
    haystack = f"{raw_item['title']} {raw_item['description']}".lower()
    for topic in config.get("topics", []):
        for kw in topic.get("keywords", []):
            if kw.lower() in haystack:
                return True
    return False


def fetch_event_detail(wikicfp_link: str) -> dict:
    """Parse a WikiCFP event detail page for structured fields.

    Returns a dict with: official_url, location, conference_start,
    conference_end, milestones (dict of milestone-name -> ISO date string),
    categories (list of str).
    """
    resp = _throttled_get(wikicfp_link)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    result = {
        "official_url": None,
        "location": None,
        "conference_start": None,
        "conference_end": None,
        "milestones": {},
        "categories": [],
    }

    # --- RDFa/hCalendar spans: walk in document order, grouping by v:summary ---
    spans = soup.find_all(attrs={"property": True})
    groups: list[dict] = []
    current: dict = {}
    for s in spans:
        prop = s.get("property")
        content = s.get("content")
        text = content if content is not None else s.get_text(strip=True)
        if prop == "v:summary":
            if current:
                groups.append(current)
            current = {"summary": text}
        elif prop in ("v:startDate", "v:endDate", "v:locality", "v:eventType"):
            current[prop] = text
        elif prop == "dc:source":
            result["official_url"] = text
    if current:
        groups.append(current)

    for i, g in enumerate(groups):
        if i == 0 and g.get("v:eventType") == "Conference":
            # First group is the conference itself, not a milestone.
            result["conference_start"] = _iso_date(g.get("v:startDate"))
            result["conference_end"] = _iso_date(g.get("v:endDate"))
            result["location"] = g.get("v:locality")
            continue
        name = g.get("summary")
        when = _iso_date(g.get("v:startDate"))
        if name and when:
            result["milestones"][name] = when

    # --- Categories row: "<a class="blackbold" href="/cfp/allcat">Categories</a> <a>energy</a> ..." ---
    cat_anchor = soup.find("a", class_="blackbold", href="/cfp/allcat")
    if cat_anchor and cat_anchor.parent:
        for a in cat_anchor.parent.find_all("a"):
            text = a.get_text(strip=True)
            if text and text.lower() != "categories":
                result["categories"].append(text)

    return result


def _iso_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    # v:startDate looks like "2026-08-30T00:00:00" — keep just the date part.
    return value.split("T")[0]


def run(config: Optional[dict] = None) -> list[dict]:
    """Full fetch pass: one cat= feed per entry in wikicfp_categories,
    deduped by eventid, prefiltered, then detail-fetched. Writes raw output
    to .tmp/ for debugging and returns the list of raw records
    (pre-normalization).
    """
    config = config or load_config()

    seen_eventids: set[str] = set()
    candidates: list[dict] = []

    for cat in config.get("wikicfp_categories", []):
        for item in fetch_category_rss(cat):
            eid = extract_eventid(item["wikicfp_link"])
            if not eid or eid in seen_eventids:
                continue
            seen_eventids.add(eid)
            item["eventid"] = eid
            candidates.append(item)

    write_json(TMP_DIR / "wikicfp_candidates_raw.json", candidates)

    relevant = [c for c in candidates if prefilter_relevant(c, config)]

    raw_records = []
    for item in relevant:
        detail = fetch_event_detail(item["wikicfp_link"])
        raw_records.append({**item, **detail})

    write_json(TMP_DIR / "wikicfp_raw_records.json", raw_records)
    return raw_records


if __name__ == "__main__":
    cfg = load_config()
    records = run(cfg)
    print(f"Fetched {len(records)} candidate records from WikiCFP "
          f"(after prefiltering) -> .tmp/wikicfp_raw_records.json")
