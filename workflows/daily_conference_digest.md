# Workflow: Daily Conference & CFP Digest

## Objective

Every day, find conferences and calls for papers (CFPs) relevant to the
user's Master's research (energy science — renewable/power systems, energy
storage/materials, energy policy/economics, AI/ML applied to energy — plus
adjacent environment/engineering/economics topics, with a ranking boost for
Malaysia/Southeast Asia), and deliver them as:

1. An email digest.
2. An always-current website (GitHub Pages, `docs/index.html`).
3. A Google Sheet you can act on (optional) — see step 5 under "Steps" below.

Scope is **global and topic-first, not location-restricted** — paper
submission isn't limited by the author's location, so we don't filter by
where the conference is held. Location is shown in each entry purely as
travel-planning info.

Originally built weekly, switched to daily per user request. Running daily
against WikiCFP is well within its `robots.txt` politeness terms (the 5s
`Crawl-delay` is respected either way) — this just means the crawl happens
7x more often, not more aggressively per-request.

## Trigger

`.github/workflows/daily_digest.yml`, on a daily cron (see that file for
the exact schedule) and on manual `workflow_dispatch` for testing.

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

Because this runs unattended on a schedule — no agent or human present to
make live judgment calls — every step below is a deterministic Python
function; the two steps that call an LLM (`fetch_search_api.py`,
`llm_extract.py`) do so as a bounded, single-purpose API call inside a
deterministic script, not as an agent making its own decisions about what
to do next.

1. **`tools/fetch_wikicfp.py`** — Pull WikiCFP category RSS feeds (both
   configured in `filters.yaml`). Cheaply prefilter on RSS title/description
   before spending a request on each item's detail page (which has the
   actual deadline data, via hCalendar microformat spans). Respects
   WikiCFP's `robots.txt` `Crawl-delay: 5` with a hard 5s sleep between
   every HTTP request to the site, and retries transient network errors
   with backoff before giving up on just that one item (see "Known edge
   cases").

2. **`tools/fetch_search_api.py`** *(optional — skips gracefully if
   `SERPER_API_KEY`/`GROQ_API_KEY` aren't set)* — Finds Malaysian
   university conferences (UM/UKM/USM/UPM/UTM etc.) that WikiCFP doesn't
   index, per user request. Runs targeted Serper.dev search queries
   (`config/filters.yaml`'s `search_api.queries`, capped at
   `max_queries_per_run`), then for each result fetches the actual page and
   asks Groq (`openai/gpt-oss-20b`, JSON mode) to determine whether it's a
   genuine CFP and extract title/dates/deadline/location/topics —
   instructed explicitly not to guess a deadline that isn't stated, and to
   drop the candidate rather than fabricate one. Direct scraping of these
   sources was investigated and rejected first (see "Known edge cases") —
   this is the sustainable replacement.

3. **`tools/normalize_records.py`** — Map raw WikiCFP items into the shared
   `ConferenceRecord` schema (`tools/common.py`). Cleans HTML-entity-escaped
   titles, infers `mode` (online/hybrid/onsite) from the location text,
   picks the submission deadline out of WikiCFP's milestone list. Drops any
   record with no extractable submission deadline — can't rank or window-
   filter something with no deadline. (Search-API records skip this step —
   the LLM's structured extraction already produces clean fields — and are
   concatenated in directly.)

4. **`tools/classify_relevance.py`** — Deterministic keyword scoring against
   `filters.yaml`'s topic list (weighted, normalized to 0–1) plus a hard
   exclusion check (organizer/keyword blocklist for known low-quality
   "conference mill" listings). Applies uniformly to WikiCFP and
   search-API-sourced records alike, so region boost / Malaysia-AI carve-out
   scoring is consistent regardless of source.

5. **`tools/dedupe_and_store.py`** — Merge into `data/conferences_db.json`.
   Exact match on WikiCFP's stable `eventid` first; fuzzy title match
   (rapidfuzz, threshold 90) as a fallback, **gated by matching year** so
   annual recurrences (e.g. "HEEPS 2026" vs "HEEPS 2027") don't wrongly
   merge. New records get `first_seen` set; existing ones get
   `last_verified` bumped and their mutable fields refreshed. Deliberately
   preserves `user_status`/`user_notes` **and `fee_info`/`travel_support_info`**
   from the existing record on every re-fetch (see "Known edge cases") — a
   routine daily re-crawl must never silently erase a Status you set in the
   Sheet, or force a re-run LLM extraction for a conference already checked.

6. **`tools/llm_extract.py`** *(optional — skips gracefully if
   `GROQ_API_KEY` isn't set)* — Fee/travel/accommodation extraction, per
   user request: WikiCFP almost never states this, so for records that
   don't already have `fee_info`/`travel_support_info` set, fetches the
   conference's official homepage and asks Groq (`openai/gpt-oss-20b`, JSON
   mode) to extract only what the page actually states, with an explicit
   not-stated fallback rather than guessing. **Only runs for new records**
   — step 5's field-preservation is what makes that safe — so usage scales
   with new-conferences-per-day, not total-conferences-per-day.

7. **`tools/publish_sheet.py`** *(optional — skips gracefully if
   `GOOGLE_SERVICE_ACCOUNT_JSON`/`GOOGLE_SHEET_ID` aren't set)* — Runs
   **before** `filter_and_rank.py` on purpose: it reads whatever Status/
   Notes are currently in the Sheet's "Conferences" tab and writes them onto
   the matching records' `user_status`/`user_notes` (then immediately
   persists that to `data/conferences_db.json`), so a Status you set
   yesterday — including `Dismissed` — is already in effect for *today's*
   email/website, not just the Sheet. Reads/reconciles Status+Notes across
   the Conferences AND Malaysia tabs (`merge_existing_rows()`, Conferences
   taking priority on conflicts) before writing, since a Malaysia energy
   conference legitimately appears on both. Publishes three tabs:
   - **Conferences** — the same windowed, non-expired, non-Dismissed list
     as the email/website (via `filter_and_rank.run()`). A conference drops
     off this tab automatically once its deadline passes — no manual
     cleanup. Has a Status dropdown (data validation): Interested / Planned
     to Submit / In Progress / Submitted / Accepted / Rejected / Dismissed.
   - **Malaysia** *(added per user request)* — every Malaysia-located
     conference already in Conferences, PLUS a broadened carve-out
     (`classify_relevance.apply_malaysia_ai_carveout`,
     `filter_and_rank.malaysia_tab`) of general AI/ML/CS conferences located
     in Malaysia that the energy-topic scoring would otherwise never surface
     at all — this carve-out is deliberately NOT gated on any energy
     relevance, and exists ONLY on this tab, never leaking into
     Conferences/website/email. Required a `prefilter_relevant()` change in
     `fetch_wikicfp.py` too: without it, a Malaysia-AI listing with zero
     energy-topic keywords would never survive the pre-detail-fetch filter
     to reach classification in the first place.
   - **Participated** — a permanent log of anything ever marked Submitted /
     Accepted / Rejected. Unlike Conferences, entries here are never
     dropped just because the deadline passed — "which ones did I actually
     submit to" needs to survive past its own window.
   Auth is a Google Cloud service account (not OAuth), chosen for the same
   reason as Resend over Gmail SMTP: no human present to handle interactive
   re-consent on an unattended cron.

8. **`tools/filter_and_rank.py`** — Keep records with `relevance_score >=
   min_relevance_score`, `user_status != "Dismissed"`, and a submission
   deadline inside `deadline_window_days` (default 180, today or later).
   Sort by deadline ascending. Feeds the email, the website, AND the
   Sheet's Conferences tab — all three are always in sync. Its
   `malaysia_tab()` sibling feeds only the Sheet's Malaysia tab (see step 7).

9. **`tools/render_digest.py`** — Render the same ranked list into the email
   HTML body and `docs/index.html` (GitHub Pages source), from the shared
   Jinja2 template at `tools/templates/digest.html.j2`.

10. **`tools/send_email.py`** — Send via Resend's API (send-to-self sandbox
    mode, no domain verification needed). Chosen over Gmail SMTP because CI's
    rotating IPs are a known trigger for Google security holds on unattended
    SMTP logins — a failure mode with no human present to clear it.

11. *(Handled by the GitHub Actions workflow, not the Python pipeline)*:
    commit `data/conferences_db.json` and `docs/` back to `main` so state
    persists across runs and GitHub Pages picks up the update.

## Output format

Each digest entry: title (linked to the official conference site),
submission deadline, notification date (if known), conference dates,
location + mode, matched topics, urgency badge (high: ≤14 days, medium:
≤45 days, low: beyond that), and a link back to the WikiCFP listing with a
"verify before submitting" note — deadlines sourced from an aggregator can
occasionally be stale.

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
  now a non-issue at daily cadence (was a real risk at weekly). Also: an
  earlier version of this schedule was documented as "17:22 UTC ≈ 07:22
  JST" — that arithmetic was simply wrong (17:22 + 9h = 02:22, not 07:22).
  Caught and fixed when switching to daily; worth double-checking any
  UTC/local-time comment in a cron file rather than trusting it by eye.
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
