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
  `GOOGLE_SHEET_ID` (optional — see README.md's Google Sheet section).

## Steps (executed by `tools/run_pipeline.py`)

Because this runs unattended on a schedule — no agent or human present to
make live judgment calls — every step below is a deterministic Python
function, not an LLM decision. This is Phase 1: WikiCFP is the only source,
and relevance scoring is keyword-based (see "Deferred to Phase 2" below for
what's intentionally not built yet).

1. **`tools/fetch_wikicfp.py`** — Pull WikiCFP category RSS feeds and
   keyword-search RSS feeds (both configured in `filters.yaml`). Cheaply
   prefilter on RSS title/description before spending a request on each
   item's detail page (which has the actual deadline data, via hCalendar
   microformat spans). Respects WikiCFP's `robots.txt` `Crawl-delay: 5`
   with a hard 5s sleep between every HTTP request to the site.

2. **`tools/normalize_records.py`** — Map raw WikiCFP items into the shared
   `ConferenceRecord` schema (`tools/common.py`). Cleans HTML-entity-escaped
   titles, infers `mode` (online/hybrid/onsite) from the location text,
   picks the submission deadline out of WikiCFP's milestone list. Drops any
   record with no extractable submission deadline — can't rank or window-
   filter something with no deadline.

3. **`tools/classify_relevance.py`** — Deterministic keyword scoring against
   `filters.yaml`'s topic list (weighted, normalized to 0–1) plus a hard
   exclusion check (organizer/keyword blocklist for known low-quality
   "conference mill" listings).

4. **`tools/dedupe_and_store.py`** — Merge into `data/conferences_db.json`.
   Exact match on WikiCFP's stable `eventid` first; fuzzy title match
   (rapidfuzz, threshold 90) as a fallback, **gated by matching year** so
   annual recurrences (e.g. "HEEPS 2026" vs "HEEPS 2027") don't wrongly
   merge. New records get `first_seen` set; existing ones get
   `last_verified` bumped and their mutable fields refreshed. Deliberately
   preserves `user_status`/`user_notes` from the existing record on every
   re-fetch (see "Known edge cases") — a routine daily re-crawl must never
   silently erase a Status you set in the Sheet.

5. **`tools/publish_sheet.py`** *(optional — skips gracefully if
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

6. **`tools/filter_and_rank.py`** — Keep records with `relevance_score >=
   min_relevance_score`, `user_status != "Dismissed"`, and a submission
   deadline inside `deadline_window_days` (default 180, today or later).
   Sort by deadline ascending. Feeds the email, the website, AND the
   Sheet's Conferences tab — all three are always in sync. Its
   `malaysia_tab()` sibling feeds only the Sheet's Malaysia tab (see step 5).

7. **`tools/render_digest.py`** — Render the same ranked list into the email
   HTML body and `docs/index.html` (GitHub Pages source), from the shared
   Jinja2 template at `tools/templates/digest.html.j2`.

8. **`tools/send_email.py`** — Send via Resend's API (send-to-self sandbox
   mode, no domain verification needed). Chosen over Gmail SMTP because CI's
   rotating IPs are a known trigger for Google security holds on unattended
   SMTP logins — a failure mode with no human present to clear it.

9. *(Handled by the GitHub Actions workflow, not the Python pipeline)*:
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

## Deferred to Phase 2 (not built yet — revisit after a stretch of clean runs)

- `tools/fetch_search_api.py` (Serper.dev) as a secondary source for
  journal/society CFPs WikiCFP misses.
- LLM-based field extraction (Claude Haiku) for the less-structured
  search-API results only — WikiCFP stays on deterministic keyword scoring.
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
