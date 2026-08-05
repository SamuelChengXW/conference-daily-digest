# Conference Daily Digest

A weekly, unattended pipeline that finds conferences and calls for papers
(CFPs) relevant to an energy-science research focus (renewable/power
systems, energy storage/materials, energy policy/economics, AI/ML for
energy, plus adjacent environment/engineering/economics) and delivers them
by email, a small GitHub Pages website, and (optionally) a running Google
Sheet archive you can annotate.

Scope is **global and topic-first** — not restricted by conference location.

See [`workflows/weekly_conference_digest.md`](workflows/weekly_conference_digest.md)
for the full SOP (what each step does, and lessons learned from building it).
This README is just the setup checklist.

## How it works, in one line

`.github/workflows/weekly_digest.yml` runs `tools/run_pipeline.py` weekly,
which fetches from WikiCFP, scores relevance by keyword against
[`config/filters.yaml`](config/filters.yaml), dedupes against
`data/conferences_db.json`, and renders + sends the result — no AI/agent
involved at run time, since nobody's there to supervise it on a 3am cron.

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

6. **Test it manually before trusting the schedule**: Actions tab → "Weekly
   Conference Digest" → Run workflow. Confirm: the Action run is green, an
   email arrives, and the Pages site (step 2's URL) shows the digest.

That's it — after a clean manual run, the `schedule:` trigger in
`weekly_digest.yml` (Sundays 17:22 UTC ≈ Mondays ~07:22 JST) takes over.

## Optional: Google Sheet archive

`docs/index.html` and the email are **this-week snapshots** — a conference
drops off once its deadline passes or it falls out of the relevance/window
filters, with no record you ever saw it. A Google Sheet fixes that: every
relevant conference ever found stays as a row (sorted upcoming-first, then
most-recently-expired), so you can mark a `Status` (e.g. Interested /
Submitted / Skip) and add `Notes` per row — the pipeline never overwrites
those two columns on later runs, only the data columns (deadline, score,
links, etc.).

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
5. Open the downloaded JSON file and find the `"client_email"` field
   (looks like `something@your-project.iam.gserviceaccount.com`).
6. Create a new Google Sheet (any name) at sheets.google.com. Click
   **Share**, paste that `client_email` address, give it **Editor** access.
7. Copy the sheet's ID from its URL:
   `https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`.
8. Add two more repo secrets (same place as step 5 above):
   - `GOOGLE_SERVICE_ACCOUNT_JSON` — paste the **entire contents** of the
     downloaded JSON file.
   - `GOOGLE_SHEET_ID` — the ID from step 7.
9. Re-run the workflow manually — the sheet should populate with a
   `Conferences` tab.

## Local development

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
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
- **Missing relevant results?** Add keywords, or lower `min_relevance_score`.
- **Deadline window too short/long?** Adjust `deadline_window_days` (default
  180 — this only affects the email/website snapshot; the Google Sheet
  archive, if enabled, always shows everything regardless of this setting).
- **A specific conference/organizer is low-quality?** Add it to
  `excluded_organizers` or `excluded_keywords`.

## Repo layout

```
tools/          Deterministic Python pipeline steps (see docstrings in each file)
workflows/      The human-readable SOP this pipeline implements
config/         filters.yaml — the only file you should need to edit regularly
data/           conferences_db.json — persistent dedup/history state (git-tracked)
docs/           GitHub Pages source (index.html — regenerated every run)
.tmp/           Disposable per-run scratch output, gitignored
.github/workflows/  The weekly cron
```

## What's deferred (Phase 2)

A secondary search-API source (Serper.dev) for journal/society CFPs
WikiCFP misses, LLM-based extraction for that messier source, refined
predatory-venue blocklisting, and a failure-notification safety net (so a
silent multi-week breakage doesn't quietly compound into GitHub disabling
the cron — see `workflows/weekly_conference_digest.md`'s "known edge
cases" section). Worth revisiting once Phase 1 has run cleanly for a
couple of weeks.
