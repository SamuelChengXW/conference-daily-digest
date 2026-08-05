# Workflow: Weekly Conference & CFP Digest

## Objective

Every week, find conferences and calls for papers (CFPs) relevant to the
user's Master's research (energy science — renewable/power systems, energy
storage/materials, energy policy/economics, AI/ML applied to energy — plus
adjacent environment/engineering/economics topics), and deliver them as:

1. An email digest.
2. An always-current website (GitHub Pages, `docs/index.html`).

Scope is **global and topic-first, not location-restricted** — paper
submission isn't limited by the author's location, so we don't filter by
where the conference is held. Location is shown in each entry purely as
travel-planning info.

## Trigger

`.github/workflows/weekly_digest.yml`, on a weekly cron (see that file for
the exact schedule) and on manual `workflow_dispatch` for testing.

## Required inputs

- `config/filters.yaml` — topic keywords/weights, WikiCFP categories and
  keyword queries to pull, deadline window, exclusion list, email settings.
- `data/conferences_db.json` — persistent state from previous runs (dedup +
  history). Git-tracked deliberately, unlike everything in `.tmp/`.
- Secrets (GitHub Actions secrets in production, `.env` locally):
  `RESEND_API_KEY`, `EMAIL_TO`.

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
   `last_verified` bumped and their mutable fields refreshed.

5. **`tools/filter_and_rank.py`** — Keep records with `relevance_score >=
   min_relevance_score` and a submission deadline inside
   `deadline_window_days` (default 120, today or later). Sort by deadline
   ascending.

6. **`tools/render_digest.py`** — Render the same ranked list into the email
   HTML body and `docs/index.html` (GitHub Pages source), from the shared
   Jinja2 template at `tools/templates/digest.html.j2`.

7. **`tools/send_email.py`** — Send via Resend's API (send-to-self sandbox
   mode, no domain verification needed). Chosen over Gmail SMTP because CI's
   rotating IPs are a known trigger for Google security holds on unattended
   SMTP logins — a failure mode with no human present to clear it.

8. *(Handled by the GitHub Actions workflow, not the Python pipeline)*:
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

- **WikiCFp's own keyword search ranks loosely** (a search for "energy
  storage" surfaced an unrelated media-studies conference first) — don't
  trust its relevance ordering, only use it for candidate discovery; our own
  `classify_relevance.py` does the real filtering.
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
  since this workflow commits weekly on success, a *silent* multi-week
  failure compounds into the cron itself going dark with no alert (Phase 2:
  add a failure-notification safety net for this).

## Deferred to Phase 2 (not built yet — revisit after a few clean weeks of Phase 1)

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
