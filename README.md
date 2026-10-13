# Conference Daily Digest

A weekly, unattended **Free & Funded Research Opportunities** tracker. It
only shows opportunities a self-funded student can actually take up:

- conferences that are **free** or **fee-waivable** (waivers or reduced fees
  for students or developing countries),
- conferences that offer **travel grants / scholarships / financial aid**,
- **standalone funding programs** (travel grants, fellowships), and
- **journal special issues with no publication fee (APC)**.

Each entry is shown only if its official page *states* this, and the quoted
sentence is shown alongside it. Paid conferences, and ones whose cost
couldn't be confirmed, are tracked but never shown. (The tracker listed all
relevant conferences until 2026-10-13, when it was narrowed to this at the
user's request.)

Topics: energy (renewable and general, power systems, storage/materials),
engineering, AI/ML, economics and policy, ESG, the SDGs and sustainability,
with a ranking boost for Malaysia/Southeast Asia/Japan. Delivered by email,
a GitHub Pages website, and (optionally) a Google Sheet. Sources: WikiCFP,
Serper.dev + Groq searches (free/funded-first queries plus Malaysia/Japan/
energy-society sources), Universiti Malaya's event system (via Playwright),
and curated funding-program pages and listings.

Scope is **global and topic-first**: results are not restricted by
conference location.

See [`workflows/daily_conference_digest.md`](workflows/daily_conference_digest.md)
for the full SOP (what each step does, and lessons learned from building it).
This README is just the setup checklist.

## How it works, in one line

`.github/workflows/daily_digest.yml` runs `tools/run_pipeline.py` weekly. It
gathers candidates (WikiCFP, search, UM Event System, funding programs),
scores topic relevance against [`config/filters.yaml`](config/filters.yaml),
dedupes against `data/conferences_db.json`, then has `tools/extract_access.py`
read each relevant candidate's official page **and its registration/grant
subpages** to decide whether it is free, fee-waivable or funded. A verdict
only counts if the sentence it quotes is really on the page. Only eligible
entries are rendered and sent. The LLM calls are bounded, single-purpose
extraction steps inside deterministic scripts; no agent makes decisions at
run time.

## One-time setup

1. **Push this repo to GitHub** (if you haven't already):
   ```
   git remote add origin <your-repo-url>
   git add -A && git commit -m "Initial digest pipeline"
   git push -u origin main
   ```

2. **Enable GitHub Pages**: repo Settings → Pages → Source: "Deploy from a
   branch" → Branch: `main`, folder: `/docs`. Your site will appear at
   `https://<username>.github.io/<repo-name>/`.

3. **Allow the workflow to commit back to the repo**: Settings → Actions →
   General → Workflow permissions → "Read and write permissions."

4. **Get a Resend API key** (used to send the email — free, no credit card):
   - Sign up at [resend.com](https://resend.com) using **the Gmail address
     you want the digest sent to**. Resend's free sandbox mode can only send
     to the address you signed up with unless you verify a domain — which
     this project deliberately avoids needing, since it only ever emails you.
   - Create an API key in the Resend dashboard.

5. **Add repo secrets**: Settings → Secrets and variables → Actions → New
   repository secret:
   - `RESEND_API_KEY` — from step 4.
   - `EMAIL_TO` — the same Gmail address. (Deliberately not committed
     anywhere in the repo since it's public — this secret is the only place
     it lives.)

6. **Test it manually before trusting the schedule**: Actions tab → "Daily
   Conference Digest" → Run workflow. Confirm: the Action run is green, an
   email arrives, and the Pages site (step 2's URL) shows the digest.

That's it — after a clean manual run, the `schedule:` trigger in
`daily_digest.yml` takes over (22:37 UTC every Sunday ≈ 07:37 JST Monday —
stepped down from daily → every 2 days → weekly as run time grew with more
sources/topics added; still well within the 180-day deadline window).

## Optional: Google Sheet (choose/dismiss opportunities, track submissions)

Tabs, all auto-managed. Every tab except Participated shows only free,
fee-waivable or funded entries, the same set as the email and website:

- **Free to Submit**: free or fee-waivable conferences.
- **Funding Available**: paid conferences that offer travel grants,
  scholarships or financial aid.
- **Funding Programs**: standalone travel grants and fellowships.
- **Journal Calls (No APC)**: special issues with no publication fee.
- **Malaysia**: eligible Malaysia-located entries, plus eligible general
  AI/ML/CS conferences located in Malaysia.
- **Participated**: a permanent log of anything you've ever marked
  Submitted / Accepted / Rejected. Rows here are **never** dropped when a
  deadline passes.

Entries **drop off automatically once their deadline passes**. Each row has
**Cost**, **Funding**, **Next Deadline** (the earlier of the grant deadline
and the paper deadline), **Eligibility** and **Evidence** (the quoted source
sentence plus link). There is also a **Status dropdown**: Interested /
Planned to Submit / In Progress / Submitted / Accepted / Rejected /
Dismissed. **Dismissed** hides an entry from every tab, the website and the
email, on every future run.

The pre-2026-10-13 tabs (**Conferences**, **Free Submission**) are read once
so any Status/Notes you set there carry over, then deleted.

`Notes` is a free-text column on every tab for your own reference — never
overwritten by the pipeline.

This step is entirely optional — the pipeline skips it gracefully if unset.
Setup (one-time, ~5 minutes):

1. Go to [console.cloud.google.com](https://console.cloud.google.com) →
   create a new project (any name).
2. In that project, enable the **Google Sheets API**
   (APIs & Services → Enable APIs and Services → search "Google Sheets API"
   → Enable).
3. Create a **service account**: IAM & Admin → Service Accounts → Create
   Service Account (any name, no roles needed) → Done.
4. Open that service account → Keys tab → Add Key → Create new key → JSON.
   This downloads a `.json` file — **keep it private**, it's a credential.
   **Don't leave it sitting in this project folder** — this repo is public,
   and `git add -A` would happily commit it. `.gitignore` here has patterns
   for common service-account key filenames as a backstop, but the safest
   move is: use it for step 8 below, then delete the downloaded file (or at
   least move it well outside the repo).
5. Open the downloaded JSON file and find the `"client_email"` field
   (looks like `something@your-project.iam.gserviceaccount.com`).
6. Create a new Google Sheet (any name) at sheets.google.com, **or use an
   existing one** (e.g. one already created for you and shared with your
   account). Click **Share**, paste that `client_email` address, give it
   **Editor** access.
7. Copy the sheet's ID from its URL:
   `https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`.
8. Add two more repo secrets (same place as step 5 above):
   - `GOOGLE_SERVICE_ACCOUNT_JSON` — paste the **entire contents** of the
     downloaded JSON file.
   - `GOOGLE_SHEET_ID` — the ID from step 7.
9. Re-run the workflow manually. The sheet should populate with the tabs
   listed above, each with the Status dropdown already applied.

## Optional: search source, cost/funding verification, funding programs

Two more integrations, both optional and independent of each other and of
the Google Sheet above — the pipeline skips whichever secrets are unset:

- **Search source** (`SERPER_API_KEY` + `GROQ_API_KEY`) — started as
  Malaysian-university-only (direct scraping of those sites turned out not
  to be reliable — see `workflows/daily_conference_digest.md`'s "known edge
  cases" for what was tried and why it didn't work), broadened 2026-08-08 to
  also cover Japan and general energy-society/journal CFPs that WikiCFP
  misses regardless of region, then broadened again 2026-08-09 with Malaysia
  professional-body sources (IEEE Malaysia Section, Institution of Engineers
  Malaysia) and a curated list of known recurring Malaysia conference series
  (APEE, IICAIET, ISCI, IEACon, ICSIPA, ICSSA, ISTT, InEC, ICEP), so a
  specific annual series doesn't depend on generic keyword-search ranking to
  get found. Targeted Serper.dev searches (`config/filters.yaml`'s
  `search_api.queries`, grouped by category) find candidate pages, and an
  LLM reads each one to confirm it's a real CFP and extract structured
  fields — configured to never guess a submission deadline that isn't
  stated. Results go through the same relevance scoring and cost/funding
  verification as WikiCFP records. The query list is now longer than one run's budget
  (`search_api.max_queries_per_run`) — `fetch_search_api.py` rotates a
  same-sized window through the full list by day-of-year, so the whole pool
  gets covered over several weeks rather than each run's spend/run time
  growing every time a query is added — at the current weekly cadence and
  47-query pool, a full cycle takes about 10 weeks (simulated directly,
  not estimated) since each week's window only advances a few positions,
  not a clean 14-item step. Slower discovery than when this ran daily is
  the direct tradeoff of the reduced cadence, not a bug.
- **Cost/funding verification** (`tools/extract_access.py`, `GROQ_API_KEY`
  only). This is required for anything to appear: without it, nothing can
  be confirmed free or funded. For each relevant candidate, it fetches the
  official page plus up to 3 linked registration/fee/grant/award subpages,
  keeps only the text around fee and funding keywords, and asks Groq for a
  verdict with a verbatim quote. If the quote isn't actually on the page,
  the verdict is downgraded to "unknown" and the entry isn't shown. Capped
  at 30 entries per run; the rest carry over to the next run. "Unknown"
  results are re-checked after 45 days, because registration pages often
  go up late.
- **Search priority queries** (`search_api.priority_queries`) run every
  week and look for free and funded opportunities directly: travel grants,
  fee waivers, free registration, ESG/SDG calls with funding, no-APC
  journal calls, and Japan graduate travel grants.
- **Funding programs** (`tools/fetch_funding_programs.py`, no key needed).
  Config's `funding_programs` lists single program pages (IEEE PELS student
  travel grants, IEEE PES General Meeting student program), which are
  checked by the verifier above, and the Terra Viva Grants travel & mobility
  listing. That listing is parsed without an LLM and covers MDPI's travel
  awards, whose own pages block scripts. Items are limited to
  worldwide/Asia-eligible ones, then filtered by topic relevance.
- **UM Event System** (`tools/fetch_um_events.py`, `GROQ_API_KEY` +
  `playwright install chromium`) — Universiti Malaya's own event management
  system (665 total events, 45 for 2026 alone as of 2026-08-09). Unlike
  every other source here, its listing only renders via JavaScript and its
  bot-protection blocks a default headless browser outright — needs
  Playwright specifically (see the module's docstring and the workflow
  doc's known-edge-cases entry for the full investigation). Covers all UM
  event types, not just CFPs, so it's filtered the same way WikiCFP is
  (`fetch_wikicfp.prefilter_relevant()`, reused as-is) before spending a
  Groq call confirming each candidate.

All Groq calls go through **Groq's free API** (shared helpers in `tools/llm_extract.py`)
— Gemini was tried first since it also has a free tier, but the account hit
a persistent zero-quota restriction unrelated to which key or model was
used (two separate keys, identical failure — see the workflow doc). Groq's
free tier doesn't have that problem, and this is simple structured
extraction well within a mid-size open model's reliable range.

Setup:

1. **Serper.dev** (only needed for the search source): sign up
   free at [serper.dev](https://serper.dev) (no card required), grab an API
   key from the dashboard.
2. **Groq**: sign up free at [console.groq.com](https://console.groq.com)
   (no card required), create an API key under API Keys.
3. Add repo secrets: `SERPER_API_KEY` and `GROQ_API_KEY`.
4. Re-run the workflow manually to confirm both come through. Check the
   Action log for the "extract_access" step's per-entry verdicts and the
   section counts, and check that Sheet rows have a populated Evidence
   column.

## Local development

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium   # one-time browser download, for fetch_um_events.py
cp .env.example .env   # fill in RESEND_API_KEY if you want to test sending
cd tools
python run_pipeline.py            # full run, sends a real email
python run_pipeline.py --no-email # build + render docs/index.html, skip sending
```

Each step can also be run standalone against the previous step's `.tmp/`
output, e.g. `python fetch_wikicfp.py`, `python classify_relevance.py` —
useful when debugging one stage without re-running the whole chain (and
without re-hitting WikiCFP's rate limit unnecessarily).

## Tuning what shows up

Edit [`config/filters.yaml`](config/filters.yaml) — no code changes needed:

- **Too much noise from one topic?** Tighten its `keywords` list to more
  specific compound phrases (see the comments in that file — this is exactly
  how two false-positive categories were fixed during initial testing).
- **Missing relevant results?** Add keywords to `topics`, add another entry
  to `wikicfp_categories` (this accepts arbitrary strings, not just real
  WikiCFP categories — see the comment above it in the file), add a
  `search_api.priority_queries` entry, or add a page to `funding_programs`.
  Scores are matched topic weight divided by `score_denominator`, so adding
  topics never lowers existing scores.
- **Deadline window too short/long?** Adjust `deadline_window_days` (default
  180). It applies to the email, the website and the Sheet's section tabs,
  which always show the same set. The window uses the earlier upcoming
  deadline (grant application or paper submission). The Sheet's Participated tab is the
  exception: it ignores this setting entirely, by design.
- **Expect something to be free/funded but it isn't shown?** Its page may not
  say so in a way the verifier can quote. Check `cost_status` and
  `evidence_quote` for it in `data/conferences_db.json`.
- **A specific conference/organizer is low-quality?** Add it to
  `excluded_organizers` or `excluded_keywords`.
- **Want a conference gone for good?** Set its Status to `Dismissed` in the
  Sheet — see the Google Sheet section above. (No `filters.yaml` equivalent
  by design — that file is topic-level rules, dismissal is per-conference.)

## Repo layout

```
tools/          Deterministic Python pipeline steps (see docstrings in each file)
workflows/      The human-readable SOP this pipeline implements
config/         filters.yaml — the only file you should need to edit regularly
data/           conferences_db.json — persistent dedup/history/status state (git-tracked)
docs/           GitHub Pages source (index.html — regenerated every run)
.tmp/           Disposable per-run scratch output, gitignored
.github/workflows/  The cron (weekly — see daily_digest.yml)
```

## What's deferred

A per-day archive of past digests (`docs/archive/YYYY-MM-DD.html`), refined
predatory-venue blocklisting, and a failure-notification safety net — see
`workflows/daily_conference_digest.md`'s "known edge cases" and "deferred"
sections. (Broadening the search source beyond Malaysia — to Japan and
general energy-society/journal CFPs — was done 2026-08-08, see the section
above.)
