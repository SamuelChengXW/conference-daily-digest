"""Cost + funding verification: is this opportunity free, fee-waivable, or
funded? Replaces llm_extract.py's homepage-only fee/travel extraction
(2026-10-13 redesign, per user request — only show free/waivable/funded
opportunities).

Why the old approach wasn't enough: across 228 stored records, homepage-only
extraction found a fee for just 10 and travel support for just 3 — the rest
came back "Not stated", because fees and grants almost always live on a
Registration / Student Travel Grant / Financial Aid subpage, not the
homepage. So this tool:

  1. Fetches the official page AND up to MAX_SUBPAGES same-domain subpages
     whose link text/URL looks like registration/fee/grant/award/etc.
  2. Deterministically cuts each page down to keyword windows around
     fee/funding terms (no LLM) — keeps each Groq call well under the free
     tier's 8,000 TPM.
  3. Asks Groq for a structured verdict that must include a VERBATIM quote.
  4. Verifies that quote is actually in the fetched text (deterministic
     substring check) — no quote, no claim: an unverifiable verdict is
     downgraded to cost_status="unknown", funding_available=False, so a
     hallucinated "free" can never reach the digest.
  5. Cross-checks a "free" verdict against a currency-amount regex (same
     _FREE_RE/_CURRENCY_RE reasoning as filter_and_rank.py).

Bounded per run (MAX_RECORDS_PER_RUN + WALL_CLOCK_BUDGET_SECONDS); anything
not reached carries over to the next run via access_checked=None, the same
carry-forward pattern llm_extract.py used.

Requires GROQ_API_KEY. Skips gracefully if unset.

Run standalone for a quick manual check (first few pending records, no save):
    python tools/extract_access.py
"""
from __future__ import annotations

import re
import time
from datetime import timedelta
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import llm_extract
from common import ConferenceRecord, load_config, parse_iso_date, today

MAX_SUBPAGES = 3
SNIPPET_RADIUS = 400
MAX_SNIPPET_CHARS_PER_PAGE = 2000
MAX_TOTAL_CHARS = 5000
MIN_QUOTE_CHARS = 12
MAX_RECORDS_PER_RUN = 45  # 30 checks took ~8.5 min on the first live run;
# 45 fits inside WALL_CLOCK_BUDGET_SECONDS and the 70-min CI timeout.
SOURCE_PRIORITY = {"funding_page": 0, "search_malaysia": 1, "um_event_system": 2, "wikicfp": 3}
WALL_CLOCK_BUDGET_SECONDS = 15 * 60
# Registration pages often go up only a few months before an event, so an
# "unknown" verdict is worth re-checking later rather than trusting forever.
RECHECK_UNKNOWN_DAYS = 45
# Single funding-program pages (fetch_funding_programs.py "pages") describe a
# recurring program whose deadline/terms change each cycle.
RECHECK_PROGRAM_DAYS = 30

# Strong terms almost always mean a fee/funding page; weak ones only
# sometimes (an "Author instructions" page rarely states fees). Links are
# ranked strong-first so the MAX_SUBPAGES slots go to the likeliest pages —
# without this, APPEEC 2026's student-travel-grant page was reached only
# after camera-ready and author-instructions pages used up earlier slots.
STRONG_LINK_RE = re.compile(
    r"regist|fee|grant|travel|award|scholarship|financial[\s_-]?aid|bursar|waive|"
    r"apc|funding|stipend|"
    # Japanese pages (e.g. Kyoto University Foundation): grant, scholarship,
    # travel cost, participation fee, application guidelines.
    r"助成|奨学|旅費|渡航|参加費|登録料|募集要項|支援",
    re.IGNORECASE,
)
WEAK_LINK_RE = re.compile(r"student|support|open[\s_-]?access|author|diversity|inclusion", re.IGNORECASE)
SNIPPET_RE = re.compile(
    r"fee|registration|grant|travel|award|scholarship|financial aid|bursar|waive|"
    r"free|no charge|complimentary|apc|article processing|open access|stipend|"
    r"funding|funded|sponsor|reimburse|discount|developing countr|"
    r"助成|奨学|旅費|渡航|参加費|登録料|無料|免除|募集期間|締切",
    re.IGNORECASE,
)
_WAIVER_RE = re.compile(r"waive|waiver|reduced|fee support|financial aid|scholarship", re.IGNORECASE)

COST_STATUSES = {"free", "waivable", "paid", "unknown"}

INSTRUCTIONS = (
    "You are checking whether an academic opportunity is affordable for a "
    "self-funded graduate student. Respond with ONLY a JSON object:\n"
    '{"cost_status": "free|waivable|paid|unknown", "fee_summary": "<string>", '
    '"funding_available": <bool>, "funding_type": "<string or null>", '
    '"funding_details": "<string or null>", "funding_deadline": "<YYYY-MM-DD or null>", '
    '"eligibility": "<string or null>", "evidence_url": "<one of the page URLs below>", '
    '"evidence_quote": "<string>"}\n\n'
    "cost_status:\n"
    "- free: the text explicitly says authors/participants pay nothing (no "
    "registration fee, no submission fee, free attendance; for a journal: no "
    "article processing charge / APC).\n"
    "- waivable: there is a fee, but the text explicitly offers a fee waiver, "
    "free registration, or reduced fee for some group (students, developing "
    "countries, presenters, etc.).\n"
    "- paid: a fee is required and no waiver/reduction is mentioned.\n"
    "- unknown: the text doesn't say.\n"
    "funding_available: true ONLY if the text explicitly offers money to "
    "participants/authors (travel grant, scholarship, bursary, stipend, "
    "accommodation covered, financial aid program). A fee waiver alone is "
    "NOT funding — that's cost_status=waivable.\n"
    "funding_deadline: the application deadline for that funding, only if "
    "stated. eligibility: who qualifies (e.g. 'students from developing "
    "countries'), only if stated.\n"
    "evidence_quote: copy ONE sentence or phrase EXACTLY, character for "
    "character, from the text below that best supports your cost_status / "
    "funding_available answer. If cost_status is unknown and no funding, use "
    "an empty string.\n"
    "Never guess or infer anything not directly stated in the text."
)


def fetch_html(url: str) -> Optional[str]:
    if not url:
        return None
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ConferenceDigestBot/1.0)"},
            timeout=llm_extract.FETCH_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException:
        return None
    if "html" not in resp.headers.get("content-type", "html"):
        return None
    return resp.text


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    return " ".join(soup.get_text(" ", strip=True).split())


def find_subpage_links(html: str, base_url: str) -> list[str]:
    """Same-domain links whose anchor text or URL suggests fee/funding info,
    strong matches first (then page order), deduped, capped at MAX_SUBPAGES.
    """
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc
    seen: set[str] = {base_url.split("#")[0]}
    strong: list[str] = []
    weak: list[str] = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"]).split("#")[0]
        if urlparse(href).netloc != base_host or href in seen:
            continue
        if href.lower().endswith((".pdf", ".doc", ".docx", ".jpg", ".png", ".zip")):
            continue
        label = f"{a.get_text(' ', strip=True)} {href}"
        if STRONG_LINK_RE.search(label):
            strong.append(href)
        elif WEAK_LINK_RE.search(label):
            weak.append(href)
        else:
            continue
        seen.add(href)
    return (strong + weak)[:MAX_SUBPAGES]


def keyword_snippets(text: str, max_chars: int = MAX_SNIPPET_CHARS_PER_PAGE) -> str:
    """Merge ±SNIPPET_RADIUS windows around every fee/funding keyword hit
    into one string — deterministic token reduction before the LLM call.
    """
    spans: list[list[int]] = []
    for m in SNIPPET_RE.finditer(text):
        start = max(0, m.start() - SNIPPET_RADIUS)
        end = min(len(text), m.end() + SNIPPET_RADIUS)
        if spans and start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])
    out = " … ".join(text[s:e] for s, e in spans)
    return out[:max_chars]


def gather_pages(url: str) -> list[tuple[str, str]]:
    """[(page_url, snippet_text), ...] for the main page + fee/funding
    subpages. Pages with no keyword hits are dropped. Empty list if the
    main page can't be fetched.
    """
    html = fetch_html(url)
    if not html:
        return []
    pages = [(url, html_to_text(html))]
    for link in find_subpage_links(html, url):
        sub_html = fetch_html(link)
        if sub_html:
            pages.append((link, html_to_text(sub_html)))

    out: list[tuple[str, str]] = []
    total = 0
    for page_url, text in pages:
        snippet = keyword_snippets(text)
        if not snippet:
            continue
        snippet = snippet[: max(0, MAX_TOTAL_CHARS - total)]
        if not snippet:
            break
        out.append((page_url, snippet))
        total += len(snippet)
    return out


def _norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"[\"'`]", "", s)
    return " ".join(s.split())


def quote_is_verified(quote: Optional[str], pages: list[tuple[str, str]]) -> bool:
    if not quote or len(quote.strip()) < MIN_QUOTE_CHARS:
        return False
    q = _norm(quote).strip(" .…")
    return any(q in _norm(text) for _, text in pages)


def apply_verdict(record: ConferenceRecord, data: dict, pages: list[tuple[str, str]]) -> None:
    """Write a Groq verdict onto record, after the deterministic evidence
    and currency cross-checks. Mutates record in place.
    """
    from filter_and_rank import _CURRENCY_RE, _FREE_RE  # shared regexes, avoids a cycle at import time

    cost = str(data.get("cost_status") or "unknown").lower()
    if cost not in COST_STATUSES:
        cost = "unknown"
    funded = bool(data.get("funding_available"))
    fee_summary = data.get("fee_summary") or llm_extract.NOT_STATED
    quote = data.get("evidence_quote") or ""

    verified = quote_is_verified(quote, pages)
    if not verified and (cost in {"free", "waivable"} or funded):
        print(f"    evidence quote not found in page text — downgrading "
              f"({cost}{', funded' if funded else ''} -> unknown)")
        cost, funded = "unknown", False

    if cost == "free" and _CURRENCY_RE.search(fee_summary) and not _WAIVER_RE.search(fee_summary) \
            and not _FREE_RE.search(fee_summary):
        cost = "paid"

    deadline = data.get("funding_deadline")
    if deadline and not parse_iso_date(deadline):
        deadline = None

    evidence_url = data.get("evidence_url")
    page_urls = [u for u, _ in pages]
    if evidence_url not in page_urls:
        evidence_url = page_urls[0] if page_urls else record.url

    record.cost_status = cost
    record.fee_info = fee_summary
    record.funding_available = funded
    record.funding_type = data.get("funding_type") if funded else None
    record.funding_details = data.get("funding_details") if funded else None
    record.travel_support_info = record.funding_details or llm_extract.NOT_STATED
    # Keep a deadline the discovery pass already found (fetch_search_api)
    # if this pass didn't restate it.
    record.funding_deadline = (deadline or record.funding_deadline) if funded else None
    record.funding_url = evidence_url if funded else None
    record.eligibility = data.get("eligibility")
    record.evidence_url = evidence_url if verified else None
    record.evidence_quote = quote.strip() if verified else None


def check_record(record: ConferenceRecord, api_key: str) -> None:
    """Run the full fetch -> snippet -> Groq -> verify chain on one record.
    Always stamps access_checked, even on failure (cost_status stays
    "unknown"), so a dead site doesn't eat the budget every run — unknowns
    get re-checked after RECHECK_UNKNOWN_DAYS anyway.
    """
    record.access_checked = today().isoformat()
    pages = gather_pages(record.url) or (gather_pages(record.cfp_url) if record.cfp_url != record.url else [])
    if not pages:
        record.cost_status = "unknown"
        record.fee_info = record.fee_info or llm_extract.NOT_STATED
        return

    page_block = "\n\n".join(f"[Page: {u}]\n{t}" for u, t in pages)
    data = llm_extract.call_groq(
        api_key,
        f"Opportunity: {record.title}\nType: {record.opportunity_type}\n\n"
        f"Relevant excerpts from its official pages:\n\n{page_block}\n\n{INSTRUCTIONS}",
    )
    if not data:
        # Groq failure (rate limit/network) isn't a verdict — leave it for next run.
        record.access_checked = None
        return
    apply_verdict(record, data, pages)


def needs_check(record: ConferenceRecord) -> bool:
    if record.access_checked is None:
        return True
    checked = parse_iso_date(record.access_checked)
    if record.source == "funding_page":
        return checked is not None and today() - checked >= timedelta(days=RECHECK_PROGRAM_DAYS)
    if record.cost_status == "unknown" and not record.funding_available:
        return checked is not None and today() - checked >= timedelta(days=RECHECK_UNKNOWN_DAYS)
    return False


def run(records: list[ConferenceRecord], config: Optional[dict] = None) -> int:
    """Check cost/funding for relevant, in-window records that haven't been
    checked yet (mutates in place). Returns the number attempted.
    """
    import filter_and_rank

    config = config or load_config()
    api_key = llm_extract.get_api_key()
    if not api_key:
        print("GROQ_API_KEY not set — skipping cost/funding verification.")
        return 0

    window_days = config.get("deadline_window_days", 180)
    min_score = config.get("min_relevance_score", 0.0)
    pending = [
        r for r in records
        if not r.excluded
        and r.user_status != "Dismissed"
        and filter_and_rank.passes_relevance(r, min_score)
        and filter_and_rank.is_in_window(r, window_days)
        and needs_check(r)
    ]
    # Likeliest-to-be-free/funded first: program pages and search finds
    # (from the free/funded priority queries) before WikiCFP/UM candidates.
    # The first live run (2026-10-13) sorted by relevance alone and spent
    # all 30 checks on high-scoring WikiCFP conference-mill listings (28
    # paid/unknown) while 16 search finds went unchecked.
    pending.sort(key=lambda r: (SOURCE_PRIORITY.get(r.source, 9), -r.relevance_score,
                                filter_and_rank.effective_deadline(r) or "9999-99-99"))
    if len(pending) > MAX_RECORDS_PER_RUN:
        print(f"  {len(pending)} record(s) pending cost/funding check — doing the "
              f"{MAX_RECORDS_PER_RUN} most relevant this run, rest carry over.")
        pending = pending[:MAX_RECORDS_PER_RUN]

    started = time.monotonic()
    attempted = 0
    for record in pending:
        if time.monotonic() - started > WALL_CLOCK_BUDGET_SECONDS:
            print(f"  Wall-clock budget ({WALL_CLOCK_BUDGET_SECONDS // 60} min) reached — "
                  f"{len(pending) - attempted} record(s) carry over to next run.")
            break
        print(f"  checking: {record.title[:70]}")
        check_record(record, api_key)
        attempted += 1
        if record.access_checked is None:
            print("    -> deferred (Groq call failed; retried next run)")
        else:
            print(f"    -> {record.cost_status}{' + funding' if record.funding_available else ''}")
    return attempted


if __name__ == "__main__":
    from common import DB_PATH, read_json

    raw = read_json(DB_PATH, default={})
    sample = [ConferenceRecord.from_dict(v) for v in raw.values()]
    sample = [r for r in sample if needs_check(r)][:3]
    key = llm_extract.get_api_key()
    if not key:
        print("GROQ_API_KEY not set.")
    for r in sample if key else []:
        check_record(r, key)
        print(f"{r.title[:60]}\n  cost={r.cost_status} funded={r.funding_available} "
              f"({r.funding_type})\n  quote={r.evidence_quote!r}")
