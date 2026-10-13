# Workflow: Free & Funded Research Opportunities Digest

## Objective

Every week, find academic opportunities the user can take up **without
paying out of pocket**, relevant to their Master's research, and deliver
them as an email digest, an always-current website (GitHub Pages,
`docs/index.html`), and a Google Sheet they can act on (optional).

Since **2026-10-13** (user request: "too many paid conferences"), the
digest shows **only**:

1. **Free or fee-waivable conferences**: no registration/submission fee,
   or an explicitly offered waiver or reduced fee (e.g. students,
   developing countries). The user chose to include "waivable", not just
   strictly $0.
2. **Conferences with funding**: organizer travel grants, scholarships,
   financial aid.
3. **Standalone funding programs**: travel grants and fellowships not
   tied to one conference.
4. **Journal special issues with no publication fee (APC).**

Paid entries, and entries whose cost couldn't be confirmed, stay in
`data/conferences_db.json` (for dedup and history) but are never shown.

Topics: energy (renewable and general, power systems, storage/materials),
engineering, AI/ML, and, added 2026-10-13, **economics and policy, ESG, the
SDGs and sustainability**. Malaysia/Southeast Asia/Japan get a ranking
boost. Scope is **global and topic-first**: paper submission isn't limited
by the author's location, so location is shown for travel planning only,
never used as a filter.

The tracker ran daily at first, then every 2 days (2026-08-10), then
weekly (2026-08-19), as run time grew to ~15-29 min. WikiCFP's 5s
`Crawl-delay` is respected regardless of cadence.

## Trigger

`.github/workflows/daily_digest.yml`, on a cron that fires weekly
(stepped down from daily -> every 2 days on 2026-08-10 -> weekly on
2026-08-19, both per user request as run time grew to ~15-29 min with more
sources/topics — see that file for the exact schedule) and on manual
`workflow_dispatch` for testing.

## Required inputs

- `config/filters.yaml` — topic keywords/weights, WikiCFP categories and
  keyword queries to pull, deadline window, exclusion list, email settings.
- `data/conferences_db.json` — persistent state from previous runs (dedup +
  history). Git-tracked deliberately, unlike everything in `.tmp/`.
- Secrets (GitHub Actions secrets in production, `.env` locally):
  `RESEND_API_KEY`, `EMAIL_TO` (required); `GOOGLE_SERVICE_ACCOUNT_JSON`,
  `GOOGLE_SHEET_ID`, `SERPER_API_KEY`, `GROQ_API_KEY` (optional — each
  integration skips gracefully if its own secret(s) are unset).

## Steps (executed by `tools/run_pipeline.py`)

Because this runs unattended on a schedule, with no agent or human present
to make live judgment calls, every step below is a deterministic Python
function. The steps that call an LLM (`fetch_search_api.py`,
`fetch_um_events.py`, `extract_access.py`) each make a bounded,
single-purpose API call inside a deterministic script. No agent decides
what to do next.

1. **`tools/fetch_wikicfp.py`**: pulls WikiCFP category RSS feeds
   (`wikicfp_categories`), prefilters on RSS title/description, then
   fetches detail pages for deadlines (hCalendar microformat). Respects
   `Crawl-delay: 5`, retries transient errors, and stops at a 20-min
   wall-clock budget. WikiCFP has **no cost/funding data**, so it is only a
   candidate source: its records appear only after step 8 confirms them.

2. **`tools/fetch_search_api.py`** *(needs `SERPER_API_KEY` +
   `GROQ_API_KEY`)*: first, `search_api.priority_queries` run **every run**
   and search for free and funded opportunities directly (travel grants,
   fee waivers, free registration, ESG/SDG calls with funding, no-APC
   journal calls, Japan graduate travel grants). Then the older
   Malaysia/Japan/energy-society CFP pool rotates behind them through
   `rotate_queries()` (`max_queries_per_run`, cut 14→4→2 on 2026-10-13 to
   keep total spend about the same). Groq classifies each result page as
   `is_opportunity` plus `opportunity_type` (conference / journal_issue /
   funding_program) and extracts dates. It requires a paper deadline for a
   CFP, or an application deadline for a funding program. Cost/funding is
   **not** taken from this pass; step 8 verifies it.

3. **`tools/fetch_um_events.py`** *(needs `GROQ_API_KEY` + Playwright)*:
   Universiti Malaya's event system (see the edge-case entries below).
   Candidate source only, same as WikiCFP.

4. **`tools/fetch_funding_programs.py`** *(no key needed)*: config's
   `funding_programs`:
   - `pages`: single program pages (IEEE PELS student travel grants, IEEE
     PES GM student program), emitted unchecked so step 8 verifies them,
     and re-checked every 30 days.
   - `listings`: the Terra Viva Grants travel & mobility tag, parsed
     **without an LLM**. Each item is already a structured summary: a
     title, an official link, region/deadline-month tags, and a body with a
     stated deadline. Only `worldwide`/Asia-tagged items are kept, and
     journalism fellowships are excluded. Items are funding by
     construction, so they are emitted already verified, with the
     money/award sentence as the evidence quote.
   - The Kyoto University Foundation page (the user's university) is
     Japanese, so `extract_access.py`'s link and snippet regexes include
     Japanese terms (助成, 奨学, 旅費, 参加費 …).

5. **`tools/normalize_records.py`**: maps raw WikiCFP items into the shared
   `ConferenceRecord` schema (`tools/common.py`). Other sources already
   produce records and are concatenated in directly.

6. **`tools/classify_relevance.py`**: deterministic keyword scoring. The
   score is matched topic weight divided by a **fixed** `score_denominator`
   (3.0), plus `region_boost`, plus an exclusion blocklist. The haystack is
   title + tags + location + `funding_details`.

7. **`tools/dedupe_and_store.py`**: merges into `data/conferences_db.json`
   (exact `source:source_id` match, then a fuzzy title match gated by
   year). It preserves `user_status`/`user_notes`, **and all
   cost/funding/evidence fields** (`ACCESS_FIELDS`), unless the fresh
   record carries its own verified result (funding listings do).

8. **`tools/extract_access.py`** *(needs `GROQ_API_KEY`; nothing can be
   shown without it)*: the core of the tracker. For relevant, in-window
   records not yet checked:
   1. Fetch the official page, plus up to 3 same-domain subpages whose
      link text or URL suggests registration/fee/grant/travel/award/
      scholarship (strong terms ranked first).
   2. Reduce each page to ±400-char windows around fee/funding keywords
      (deterministic, ≤5,000 chars total, to stay under Groq's 8k TPM).
   3. Ask Groq for `cost_status` (free/waivable/paid/unknown),
      `funding_available`, type, deadline, eligibility, and a **verbatim
      evidence quote**.
   4. **Verify the quote is literally in the fetched text.** If it isn't,
      downgrade to `unknown`/not funded.
   5. Cross-check a "free" verdict against a currency-amount regex.

   Capped at 30 records and 15 min per run; the rest carry over. A Groq
   failure leaves the record unchecked (not "unknown"), so it is retried.
   "Unknown" results are re-checked after 45 days.

9. **`tools/publish_sheet.py`** *(optional)*: runs **before** step 10 on
   purpose. It reads Status/Notes from every tab (plus, once, the legacy
   Conferences / Free Submission tabs, which are then deleted), persists
   them to the DB, then writes these tabs: Free to Submit, Funding
   Available, Funding Programs, Journal Calls (No APC), Malaysia
   (eligible-only, incl. the Malaysia-AI carve-out), and Participated
   (permanent log). Auth is a service account, so no interactive
   re-consent is needed on the cron.

10. **`tools/filter_and_rank.py`**: keeps records that are
    `is_eligible()` (free / waivable / funded), not excluded, not
    Dismissed, at or above `min_relevance_score`, and whose
    **`effective_deadline()`** (the nearest upcoming of funding deadline
    and submission deadline) is within `deadline_window_days`. Funding
    programs with no deadline (rolling) stay visible while their source
    still lists them (seen within 60 days). `sections()` splits the result
    into free / funded / programs / journals. The same function feeds the
    email, the website, and the Sheet tabs.

11. **`tools/render_digest.py`**: renders the sections into the email body
    and `docs/index.html` (`tools/templates/digest.html.j2`).

12. **`tools/send_email.py`**: sends via Resend (sandbox send-to-self).

13. *(GitHub Actions, not Python)*: commits `data/conferences_db.json`
    and `docs/` back to `main`.

## Output format

Four sections, each with a count and an empty-state message. Each entry
shows:
- badges: FREE / FEE WAIVER / NO APC / the funding type / region, plus
  urgency (based on the nearest actionable deadline: high ≤14 days,
  medium ≤45 days)
- title (linked to the official site)
- the funding application deadline (highlighted) and the submission
  deadline
- event dates, location, cost summary, funding details, and eligibility
- **"Source: <verbatim quote> — check"**, linked to the page it came from

The footer reminds the reader to confirm terms on the linked page before
applying.

## Known edge cases / lessons learned

- **WikiCFP's `?q=` keyword-search RSS endpoint is broken — verified it
  ignores the query entirely.** `?q=renewable+energy` and `?q=Malaysia`
  return byte-for-byte the same "latest 50" results. This was originally
  wired up as a second discovery mechanism alongside `?cat=` category feeds
  and silently contributed nothing (every query after the first just added
  already-seen duplicates) — which is *why* widening the category list
  first (6→10 results) barely moved the needle further on a second pass.
  Fixed by dropping `?q=` entirely: `?cat=` turns out to accept **any**
  string, not just WikiCFP's own published categories, and does real
  substring matching against each listing's tags (confirmed `cat=malaysia`,
  `cat=energy+security` etc. all filter correctly). So `wikicfp_categories`
  in `filters.yaml` is now the single discovery lever — for topics,
  sub-topics, *and* regions alike, just add another string to that one list.
- **Region ranking boost (not a filter).** Per user request, conferences in
  Malaysia/Southeast Asia get `region_boost` applied in
  `classify_relevance.py` — but only on top of an existing topic match
  (`base_score > 0` gate), so a Malaysia-located conference with zero
  energy/AI/environment/economics relevance still doesn't appear. This
  keeps the project's original global/topic-first scope intact while
  surfacing regional events more prominently within it.
- **Bare generic keywords cause false positives.** Early testing showed
  plain "machine learning" / "artificial intelligence" matching unrelated AI
  conferences, and bare "sustainability" matching a humanities seminar on
  "Borders and Sustainability." Fixed by requiring energy/environment-
  qualified compound phrases in `filters.yaml` instead of single generic
  words. If new false positives show up, tighten the same way rather than
  raising `min_relevance_score` globally (that also punishes genuine
  single-topic matches, e.g. a pure energy-storage conference with no other
  topic overlap).
- **GitHub Actions cron caveats**: use an off-round UTC minute (round-minute
  crons see documented queueing delays); never add a `push` trigger
  alongside the auto-commit step (infinite loop); GitHub auto-disables
  scheduled workflows after 60 days with no commits to the default branch —
  a real (if distant) risk again now that the cadence stepped back to
  weekly (2026-08-19): every successful run commits at least
  `data/conferences_db.json`'s refreshed `last_verified` timestamps, so
  one working run every ~7 days leaves comfortable margin (~8-9 consecutive
  missed weeks before 60 days), but a silent multi-week breakage would burn
  through that margin far faster than it would have at the old daily
  cadence. Worth a periodic manual check that runs are still green, not
  just trusting the schedule indefinitely. Also: an earlier version of this
  schedule was documented as "17:22 UTC ≈ 07:22 JST" — that arithmetic was
  simply wrong (17:22 + 9h = 02:22, not 07:22). Caught and fixed when
  switching to daily; worth double-checking any UTC/local-time comment in a
  cron file rather than trusting it by eye.
- **A routine re-fetch must never silently erase a user-set Status.**
  `dedupe_and_store.upsert()` originally replaced an existing DB record
  wholesale with the freshly-refetched one on every match — fine for
  algorithmic fields (deadline, score, topics) but would have wiped
  `user_status`/`user_notes` back to blank on the next run after you set
  them, since a fresh WikiCFP fetch has no way to know about those. Fixed
  by carrying `user_status`/`user_notes` forward from the existing record
  by default; `publish_sheet.py`'s live Sheet-read (which runs right after,
  before `filter_and_rank`) remains the real authority if you've since
  changed something in the Sheet.
- **"Dismissed" needed to survive the record aging out of the Sheet's
  Conferences tab entirely.** Since that tab only shows the current window,
  a dismissed-but-now-expired conference wouldn't be there for
  `publish_sheet.py` to re-read next run. Handled by persisting
  `user_status` to `data/conferences_db.json` (not just the Sheet) the
  moment it's read — the JSON file, not the Sheet, is the durable source of
  truth, so the dismissal sticks even if that row later disappears from
  view.
- **A downloaded service-account key ended up sitting unignored in the repo
  root during setup** (named after the GCP project, e.g.
  `conference-digest-xxxxxxxxxxxx.json` — not a fixed, easily-recognized
  filename). `git add -A` would have committed a live credential to this
  public repo. Caught before it happened; `.gitignore` now has patterns for
  common service-account key filenames as a backstop, and the README calls
  out deleting the downloaded file after use. `get_or_create_worksheet()`'s
  first real run against the live Sheet also revealed a leftover `Untitled`
  tab from the original one-time manual CSV seed (predates the two-tab
  design) — deleted; worth checking for stray tabs if the Sheet was manually
  seeded before `publish_sheet.py` ever ran against it.
- **A single flaky WikiCFP response took down an entire scheduled run.**
  2026-08-07's scheduled run hit a `ReadTimeout` on one out of ~90-100
  requests (category feeds + detail pages) and the whole pipeline crashed —
  losing that day's email/Sheet/website update over one transient network
  blip, not a real bug. `_throttled_get()` now retries transient errors
  (timeout/connection error — not 4xx/5xx, which usually means something
  actually wrong) with backoff, and `fetch_category_rss()`/
  `fetch_event_detail()` catch remaining failures and skip just that one
  category/conference (logged as a warning) instead of raising. One bad
  request now costs one missing conference, not one missing day. Worth
  keeping an eye on `.tmp/pipeline_run.log` / Actions run logs for repeated
  skip warnings — occasional is normal internet flakiness, frequent would
  mean something changed on WikiCFP's end.
- **Gemini API key issue → tried Claude → landed on Groq (2026-08-08).**
  A user-provided Gemini key authenticated but every model tested —
  `gemini-2.0-flash` (429, quota `limit: 0`, not "used up"), and every other
  model including lite variants (`2.5-flash-lite`, `2.0-flash-lite`,
  `3.1-flash-lite`, etc. — all 404 "not available to new users") — failed.
  A **second, freshly-generated Gemini key on the same account hit the
  identical `limit: 0` failure**, confirming this is an account/project-level
  restriction, not a stale-key problem — regenerating a key doesn't fix it.
  First switched `tools/llm_extract.py`/`tools/fetch_search_api.py` to the
  Claude API (`claude-opus-5`) as a working-but-paid fallback, then to
  **Groq** (`openai/gpt-oss-20b`, OpenAI-compatible JSON mode via
  `requests` — no new SDK dependency) once the user asked for a genuinely
  free option instead. Both prior integrations were already designed to
  skip gracefully on a missing/broken key, so each swap was a provider
  change, not a redesign. **Note for future model changes**: check
  console.groq.com/docs/deprecations first —
  `llama-3.3-70b-versatile`/`llama-3.1-8b-instant` (the obvious first
  choices) retire 2026-08-16, a week after this was built.
- **Groq free-tier rate limit (8,000 TPM) hit on the first live run with a
  real key (2026-08-08).** `openai/gpt-oss-20b`'s free tier is 30 RPM but
  only **8,000 tokens/minute** — the real binding constraint, since one
  extraction request (page text + instructions + reasoning tokens) runs
  ~1,800-2,300 tokens, so only ~3-4 fit per minute. The first run with
  `GROQ_API_KEY` set had a large one-time backlog (every existing DB record
  had `fee_info=None`) and fired requests back-to-back with no pacing —
  nearly all 429'd and were silently swallowed by the broad
  `except RequestException` in the original `call_groq()`, so the run
  "succeeded" but extracted almost nothing. Fixed in `llm_extract.py`:
  `call_groq()` now retries on 429 honoring the `Retry-After` header (up to
  3 attempts), non-2xx failures log the real status code + response body
  instead of just the exception type name, and `run()` caps a single run at
  `MAX_RECORDS_PER_RUN = 30` (nearest-deadline first) so a large backlog
  spreads across a few days instead of hammering the rate limit in one run
  — records left unprocessed keep `fee_info=None` and are picked up
  automatically on a later day via `dedupe_and_store.py`'s carry-forward
  logic. `fetch_search_api.py` shares the same fix since it calls the same
  `llm_extract.call_groq()`. Verified locally: an artificial 10-record
  backlog now hits real 429s, retries, and completes with all 10 actually
  processed (vs. the original 53/53 failure on Actions).
- **User reported the main list looked sparse again (2026-08-08, post-Groq).**
  Diagnosed by reading `data/conferences_db.json` directly: 65 conferences
  stored, but 51 (78%) already had a submission deadline in the past —
  WikiCFP's category feeds carry a lot of historical/expired listings
  alongside live ones, not just current CFPs. Only 13 were within the
  180-day window, 12 of which passed `min_relevance_score` (which excluded
  almost nothing — the filter logic itself was working correctly). The real
  ceiling was WikiCFP's own live-listing volume for these topics, not a
  filter being too strict. Fix: broadened `fetch_search_api.py`'s query set
  (same Serper.dev + Groq mechanism already proven reliable) from
  Malaysia-only to also cover Japan and general energy-society/journal
  CFPs, and raised `max_queries_per_run` 8→14 to fit the larger query list
  — see `config/filters.yaml`'s `search_api.queries` (grouped by category
  with comments). This was the "broadening beyond Malaysia" item already
  listed under Deferred; the sparse-list report is what surfaced it as
  worth doing immediately rather than later.
- **Direct scraping of Malaysian university sites (UM/UKM/USM/UPM/UTM) was
  investigated and rejected before building `fetch_search_api.py`.**
  Alternative conference aggregators that surfaced these universities'
  events in search (internationalconferencealerts.com,
  allconferencealert.com) return 403 on every scripted request — a
  Cloudflare/bot-challenge unrelated to their permissive `robots.txt`, so
  not fixable by a polite crawl-delay. The one centralized hub found
  (`conference.utm.my`) looked promising but its RSS feed is disabled
  (410) and its visible content turned out to be stale WordPress
  placeholder entries (2024 dates, generic titles, identical pricing on
  every listing), not live data. Individual real conferences (verified via
  search: UTM's InEC2026, APEE2026, UiTM's ICEP2026) exist but are
  scattered across unrelated domains with no shared structure — one
  scraper per conference, indefinitely. Serper.dev search + LLM
  extraction from each result's actual page sidesteps all three problems.
- **User asked for more ideas to detect Malaysia conferences (2026-08-09).**
  Live-verified several candidate sources before proposing anything (same
  discipline as the entry above): `10times.com` and `conferenceindex.org`
  both **403 Forbidden** on direct fetch, same bot-blocking pattern as the
  earlier-rejected aggregators — not viable. `myjurnal.my` (Malaysia's
  national journal aggregator, a plausible source for journal special-issue
  CFPs) — connection failed (`ECONNREFUSED`), not currently reachable.
  `ieeemy.org` (IEEE Malaysia Section) — **confirmed live and real**: its
  conference listing page returned 7 genuine 2026 Malaysia conferences on
  first fetch (APEE 2026, already known, plus 6 not yet in the system:
  ISCI, IEACon, ICSIPA, IICAIET, ICSSA, ISTT), `robots.txt` only blocks
  admin/system paths. `myiem.org.my` (Institution of Engineers Malaysia) —
  also live, broader/noisier scope (mixes in non-academic dinners/
  seminars), relies on the existing conservative `is_conference_cfp` Groq
  check to filter correctly. Added both as new `search_api.queries`
  entries — no new scraping code needed, both slot into the exact same
  Serper+Groq mechanism. Also added a curated "known recurring Malaysia
  conference series" query group (APEE, IICAIET, ISCI, IEACon, ICSIPA,
  ICSSA, ISTT, InEC, ICEP) so a specific annual series isn't at the mercy
  of generic keyword-search ranking.
- **Query pool outgrew `max_queries_per_run` — fixed-prefix truncation was
  a silent coverage gap.** Adding the sources above brought
  `search_api.queries` to 25 entries against a 14/run cap; the original
  `queries[:max_queries_per_run]` slice meant entries past index 13 would
  **never run, ever** — not delayed, just permanently unreachable. Fixed by
  adding `fetch_search_api.py`'s `rotate_queries()`: a `max_queries_per_run`
  -sized window shifted by day-of-year, wrapping around the list. Verified
  by simulating 4 consecutive `day_of_year` values against the real 25/14
  config — full pool covered within 2 days, as expected
  (`ceil(25/14) = 2`). Daily Serper/Groq spend and run time stay the same
  as before (still capped at `max_queries_per_run` per run); only which
  14 queries run on a given day changes.
- **A verification run took 1h36m before being manually canceled
  (2026-08-09), vs. the normal ~15-17 min.** `gh run view --log` after
  cancellation showed **zero pipeline output** — not even the first
  `=== Daily digest pipeline starting ===` line — which was itself
  misleading: Python block-buffers stdout when it isn't a tty (the case
  under GitHub Actions' log capture), so a hang before enough output
  accumulates to trigger a flush leaves the log looking empty even if the
  process ran through several steps' worth of `print()` calls already —
  they just never got flushed before the forced cancellation. This made
  the actual stuck step impossible to identify after the fact. Ruled out
  Groq-side exhaustion first (direct key test showed 998/1000 requests and
  7893/8000 tokens still available — healthy). Most likely cause: WikiCFP
  itself having a broadly slow/degraded period, where `_throttled_get()`'s
  existing per-request retry+backoff (worst case ~75s: 3×20s timeout + 5s +
  10s backoff) is individually bounded but can still compound across dozens
  of category/detail-page requests into a very long total. Three fixes,
  all defense-in-depth rather than mutually exclusive:
  1. `PYTHONUNBUFFERED=1` added to the workflow's env — so a future hang
     shows real-time log output instead of nothing.
  2. `fetch_wikicfp.py`'s `WALL_CLOCK_BUDGET_SECONDS = 20*60` — `run()` now
     checks elapsed time before starting each category/detail fetch and
     stops early past the budget, returning whatever it already has;
     remaining categories/items are picked up on a future run (same
     carry-forward pattern as `llm_extract.py`'s `MAX_RECORDS_PER_RUN`).
     Verified by forcing the budget to 0 and confirming a clean early exit
     with a clear log message rather than a crash.
  3. `llm_extract.py`'s `call_groq()` now caps a 429's `Retry-After`-driven
     sleep at `MAX_RATE_LIMIT_WAIT = 60`s — if a future response ever
     reports something far larger (e.g. a daily-quota-scale reset), skip
     that call rather than block for however long the header says.
  4. `timeout-minutes: 45` added to the workflow's pipeline step as an
     outer safety net, in case some future unforeseen hang isn't caught by
     1-3.
  **Update (2026-08-09, later the same day)**: fix 3 above turned out to be
  more than defense-in-depth — a subsequent local run hit real Groq 429
  `Retry-After` values of 140-771 seconds (not the single-digit-to-20s
  values seen earlier), all correctly caught and skipped by the new
  `MAX_RATE_LIMIT_WAIT` cap. This is a strong retroactive signal that the
  original 1h36m hang was plausibly `llm_extract.py` blocking on one or
  more multi-hundred-second sleeps rather than (or in addition to) a
  WikiCFP slowdown — the empty log from the buffering issue made it
  impossible to tell at the time, and both explanations are now covered by
  the same fixes regardless of which was the actual cause.
- **Asked the user for real Malaysia conferences we were missing, got 5
  concrete links, live-verified each (2026-08-09).** Far more useful than
  guessing at more generic sources. Findings:
  - **`umevent.um.edu.my`** (UM Event System) — real and substantial: 665
    total events, 45 for 2026. But the listing renders client-side (a plain
    fetch shows only the search UI and a result count, no rows) — no public
    JSON/API endpoint found by guessing common patterns
    (`/search/results?year=`, etc.). Unlike everything else in this
    project (`requests` + `BeautifulSoup` only), unlocking this needs a
    headless browser. User explicitly approved adding Playwright for this
    specifically — see `tools/fetch_um_events.py`.
  - **`conference.upm.edu.my`** — real, live, individual-conference
    subpages, confirmed via two user-provided links: HEIC-II 2026 and
    I-GREDUC 2026. Matches exactly what the user described: "main landing
    page not updated, new conferences just use subpages." No
    `sitemap.xml`, no discoverable index of subpages — nothing found short
    of already knowing the specific subpage or finding it via search.
    Mitigated by adding the two confirmed events as named
    `search_api.queries` entries rather than trying to enumerate the
    domain.
  - **UTM department/center subdomains** (`fai.utm.my`, `prospect.utm.my`)
    — confirms UTM conferences scatter across many faculty/center
    subdomains rather than one central site. The existing `site:utm.my`
    query already spans all subdomains via Google's `site:` operator, so
    this wasn't a targeting gap — `fai.utm.my/icssa2026/` (already one of
    our curated known-series queries) validated that. But
    `prospect.utm.my/synergy2026` (**SYNERGY 2026**) wasn't yet in the
    system, and its abstract deadline (extended to 2026-08-15) was six
    days away when found — added as an urgent named query rather than
    waiting on organic rotation.
  - **Facebook** — genuinely the freshest channel (SYNERGY 2026's deadline
    extension was posted there first, ahead of the conference's own
    website reflecting it) but a real ceiling: aggressive anti-scraping,
    unreliable via Serper, no Graph API token in scope. Explicitly not
    pursued as an automated source — the user's own awareness of specific
    department/center pages stays the most reliable channel for this one
    freshness advantage; the pipeline is a complement here, not a
    replacement.
- **Broadened topic scope beyond energy-only, per user request
  (2026-08-10).** Motivated by a concrete gap: `fetch_um_events.py` found
  two real, clearly on-topic Malaysia conferences (RSCE2026 — chemical
  engineering; MICNC2026 — nanotechnology & catalysis) that didn't clear
  `min_relevance_score` under the old scope, because "engineering" only
  existed as a narrow sub-clause of the 0.6-weight catch-all, and "energy"
  was scoped almost entirely to renewables. Added three new full-weight
  (1.0) topic groups to `config/filters.yaml`: "Energy (general)"
  (oil/gas, nuclear, thermal, combustion — additive to the existing
  renewable-specific group, not replacing it), "Engineering (general)"
  (chemical/mechanical/civil/materials engineering, nanotechnology,
  catalysis), and "AI/ML (general)" — bare AI/ML terms, deliberately
  **not** energy-qualified this time. That last one reverses an earlier
  deliberate choice ("AI/ML for energy"'s keywords were kept
  energy-qualified specifically because bare AI/ML terms were "confirmed
  noisy in testing") — the user was asked directly whether general AI/ML
  should get full weight (matching everything else) or a reduced
  catch-all-style weight, and chose full weight, explicitly accepting more
  non-energy AI conferences will now surface. Verified the regression case
  directly: `classify_relevance.score_keyword_match()` on both RSCE2026 and
  MICNC2026 now returns 0.132 (was 0, both now clear the 0.10 threshold).
  Matching `wikicfp_categories` entries added too (a separate discovery-time
  list from `topics`'s scoring list — without these, WikiCFP would never
  fetch candidates in these areas regardless of how they'd score).
  Also investigated (same live-verify discipline as every prior round)
  whether sibling universities (USM, UKM, UiTM) run a UM-style centralized,
  Playwright-scrapable event database — no evidence found; USM appears to
  use per-conference tooling (subdomain-per-conference, similar to UPM's
  pattern) rather than one central listing, so a second Playwright scraper
  wasn't built without a concrete target. Found and added instead: **SEDA
  Malaysia** (`seda.gov.my` — confirmed live, its INSPIRE symposium page,
  co-run with UKM, had a real abstract/paper deadline on first check),
  **Suruhanjaya Tenaga/Energy Commission Malaysia** (`st.gov.my`), and
  **IChemE Malaysia / SOMChE 2026** (35th Symposium of Malaysian Chemical
  Engineers, Sunway University — its own description name-checks
  "AI-driven process design" and "digital twins," hitting the newly
  broadened engineering *and* AI/ML scope at once) as new
  `search_api.queries` entries. Query pool grew 28→36 —
  `rotate_queries()` (built for exactly this in the prior round) needs no
  code change, just cycles over 3 days instead of 2.
- **Covered more Malaysian universities, per user request (2026-08-10).**
  Institution coverage before this was just 5 (UM/UKM/USM/UPM/UTM) out of
  Malaysia's ~20 public universities. Checked five more not covered at
  all — **every single one** turned up a real, current 2026 conference on
  the first search: **UiTM** (Universiti Teknologi MARA — Malaysia's
  *largest* university, previously represented by exactly one narrow named
  query and nothing institution-wide — found ICPEA 2026, IEEE Power
  Engineering Applications; IConBEE 2026, Built Environment & Engineering),
  **UTHM** (Universiti Tun Hussein Onn Malaysia — found ICME 2026,
  Mechanical & Manufacturing Engineering, hosted on its own
  `conference.uthm.edu.my` subdomain — same subpage-per-conference pattern
  already seen at UPM), **MMU** (Multimedia University, private — found
  MECON 2026, hosted on `difcon.mmu.edu.my`), **UNIMAS** (Universiti
  Malaysia Sarawak — found EnCon 2026, a substantial 500+-delegate event),
  **UMS** (Universiti Malaysia Sabah — found ICAIMS 2026, IEEE Sabah
  Subsection co-organized, hits engineering and AI/ML at once). Added one
  institution-wide query plus one named-conference query per university —
  same low-risk pattern as every prior institution addition, no new code.
  Checked USIM and UMT too but found no confirmed current conference in a
  reasonable search effort — not added without the same evidence bar the
  other five met. Also checked whether UTHM/MMU's conference-hosting
  subdomains have an enumerable central index (would justify a dedicated
  scraper like `fetch_um_events.py`) — `conference.uthm.edu.my`'s root
  page didn't even load cleanly on a quick check (socket hang up),
  consistent with the same no-index pattern already found at UPM; not
  investigated further. Query pool grew 36→47 — `rotate_queries()` needs
  no code change, just cycles over 4 days instead of 3. Deliberately did
  *not* raise `max_queries_per_run` alongside this: the most recent run
  already took 21 minutes (up from ~10-17 historically, after the topic
  broadening pulled in far more WikiCFP/Groq work), so adding more
  per-run query volume on top of that wasn't the right lever — letting
  rotation absorb the growth instead keeps daily cost/runtime flat.
- **Added a "Free Submission" Sheet tab, per user request (2026-08-10 —
  tight budget, needs confirmed-free venues at a glance).** Deliberately
  did *not* add a second Groq-extracted boolean field to the fee/travel
  extraction schema: `llm_extract.py` only re-checks records with
  `fee_info is None` (see its `MAX_RECORDS_PER_RUN` docstring), so a new
  boolean would sit empty/`None` on every one of the ~80 records already
  extracted before this change, and backfilling all of them would mean
  re-spending a Groq call per record for no new information — the fee
  text is already sitting right there. Instead, `filter_and_rank.py` got a
  deterministic regex (`is_confirmed_free()`, `_FREE_RE`/`_CURRENCY_RE`)
  applied to the existing `fee_info` string at read time — zero extra
  Groq cost, works retroactively on everything already extracted, matches
  `CLAUDE.md`'s "deterministic code handles execution" once the page's
  been reduced to a short structured sentence. Validated against every
  non-"Not stated" `fee_info` value in the live DB (81 records checked, 6
  non-trivial) before writing the rule: correctly flags both real
  "No submission fee." entries as free, and correctly excludes every
  currency-amount mention (even "10% DISCOUNT... capped at $30" — still a
  paid fee) and "Only paid papers will be published." Deliberately
  conservative on mixed signals: "free for members, $50 for non-members"
  has both a free-word hit and a currency hit, so it's *not* counted —
  conditionally-free isn't the same claim as confirmed no-cost.
  `free_tab()` is a strict subset of `run()`'s membership (same
  window/relevance/Dismissed gating, plus the free check) rather than its
  own universe, so "Free Submission ⊆ Conferences" always holds — no
  free-but-otherwise-filtered-out conference can appear here. Wired into
  `publish_sheet.py` as a fourth tab; its Status/Notes now also
  participate in the pre-write merge-read (previously just Conferences +
  Malaysia) so a Status set specifically on this tab survives the next run.
- **The first real digest only returned 6 conferences** from 5 WikiCFP
  categories/6 keyword queries — widened to 10 categories/15 queries (each
  category feed is WikiCFP's ~20-item page size, so more categories is a
  cheap way to surface more distinct candidates), `deadline_window_days`
  120→180, `min_relevance_score` 0.15→0.10. Got 6→10 with no quality
  regression. If it's still too sparse, the next lever is Phase 2's second
  source (Serper.dev), not further loosening these thresholds.
- **WikiCFP sometimes lists the same conference twice** (a plain listing and
  an "--EI" Ei-Compendex-indexed variant, seen for real in testing) with
  slightly different title/category text. `dedupe_and_store.py`'s fuzzy
  merge originally let whichever copy was processed *last* silently win,
  even if it had a lower relevance score or fewer matched topics than the
  one it replaced. Fixed to take `max(scores)` and the *union* of matched
  topics on merge.

- **Redesigned into a free & funded tracker (2026-10-13), and why the old
  fee extraction couldn't support it.** The user asked to drop paid
  conferences and track only free, fee-waivable or funded opportunities,
  plus standalone grants and no-APC journal calls. A first check of the
  live DB showed a simple filter wasn't possible. Of 228 stored records,
  the old homepage-only `llm_extract.py` had found a fee for just **10**
  and travel support for just **3**. 120 were "Not stated" and 98 were
  never checked. Fees and grants almost always live on a Registration /
  Student Travel Grant / Financial Aid **subpage**. For example, IEEE PES
  APPEEC 2026's grant is at `/student-travel-grant-support/`, which its CFP
  page links to. Filtering the existing data would have left ~2 entries.
  Changes made:
  - `extract_access.py` follows subpages, with strong-term links ranked
    first. Before that ranking, APPEEC's grant page was reached only after
    camera-ready and author-instructions links had used up the slots.
  - Every verdict must carry a verbatim quote, checked by substring match.
    An LLM "free" with no matching quote is downgraded. This matters more
    now, because a false "free" is the one error that would mislead the
    user here.
  - Discovery gained free/funded-first priority queries and a
    funding-program source.
  - Scoring moved to a fixed `score_denominator`. With the old
    sum-of-weights denominator, adding four topic groups would have pushed
    a single full-weight match from 0.116 to ~0.08, silently under the 0.10
    cutoff.
  - MDPI's travel-award pages (Sustainability/Energies/Climate) return 403
    to scripts. Terra Viva Grants mirrors them on a robots.txt-permitted
    listing, so that is used instead.
  - The old Free Submission regex (`_FREE_RE`/`_CURRENCY_RE`) was kept as a
    cross-check on the LLM verdict, not as the decision itself.
  - The legacy Conferences / Free Submission Sheet tabs are migrated
    (Status/Notes read once) and then deleted.
  - Bare "sustainability"/"sustainable"/"climate" keywords were previously
    rejected as noisy. The user asked for sustainability coverage, so they
    are now in, on the reasoning that the free/funded gate keeps the cost
    of a false-positive topic match low.

## Deferred (not built yet — revisit after a stretch of clean runs)

`fetch_search_api.py` (Serper.dev + LLM extraction, Malaysia-focused) and
`llm_extract.py` (fee/travel/accommodation via LLM, now Groq) both shipped
2026-08-08 — no longer deferred, see the Steps section above. Broadening
`fetch_search_api.py`'s queries beyond Malaysia (to Japan + general
energy-society/journal CFPs) also shipped 2026-08-08, same day, once the
low-candidate-count investigation below showed WikiCFP's own listings were
the limiting factor and a same-mechanism expansion was the practical fix.
Still deferred:

- `docs/archive/YYYY-MM-DD.html` history pages.
- Refined predatory-venue blocklist based on real Phase 1 output.
- Failure-notification safety net (e.g., open/update a GitHub issue on hard
  pipeline failure).

## Maintenance

If a run fails or looks off, check `.tmp/pipeline_run.log` (local) or the
Actions run log (CI). If WikiCFP changes its HTML/RSS structure, the first
thing to break will be `tools/fetch_wikicfp.py`'s microformat parsing in
`fetch_event_detail()` — re-verify against a live detail page and adjust
the CSS/property selectors there. If irrelevant results start showing up,
tighten the matched keyword in `config/filters.yaml` (see "Known edge
cases" above) rather than editing code.
