# Conference Daily Digest

A daily, unattended pipeline that finds conferences and calls for papers
(CFPs) relevant to an energy-science research focus (renewable/power
systems, energy storage/materials, energy policy/economics, AI/ML for
energy, plus adjacent environment/engineering/economics, with a ranking
boost for Malaysia/Southeast Asia) and delivers them by email, a small
GitHub Pages website, and (optionally) an actionable Google Sheet.

Scope is **global and topic-first** — not restricted by conference location.

See [`workflows/daily_conference_digest.md`](workflows/daily_conference_digest.md)
for the full SOP (what each step does, and lessons learned from building it).
This README is just the setup checklist.

## How it works, in one line

`.github/workflows/daily_digest.yml` runs `tools/run_pipeline.py` daily,
which fetches from WikiCFP, scores relevance by keyword against
[`config/filters.yaml`](config/filters.yaml), dedupes against
`data/conferences_db.json`, and renders + sends the result — no AI/agent
involved at run time, since nobody's there to supervise it on an unattended
cron.

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
`daily_digest.yml` (22:37 UTC daily ≈ 07:37 JST the next day) takes over.

## Optional: Google Sheet (choose/dismiss conferences, track submissions)

Two tabs, both auto-managed:

- **Conferences** — the current/upcoming list (same as the email/website).
  A conference **automatically drops off once its deadline passes** — no
  manual cleanup. Has a **Status dropdown** per row: Interested / Planned
  to Submit / In Progress / Submitted / Accepted / Rejected / Dismissed.
  Set a row to **Dismissed** to permanently hide that conference from the
  Conferences tab, the website, and the email — not just visually in the
  Sheet, it stays hidden on every future run too.
- **Participated** — a permanent log of anything you've ever marked
  Submitted / Accepted / Rejected, "so I can remember which one I had
  submitted." Unlike Conferences, rows here are **never** dropped just
  because the deadline passed.

`Notes` is a free-text column on both tabs for your own reference — never
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
9. Re-run the workflow manually — the sheet should populate with
   `Conferences` and `Participated` tabs, each with the Status dropdown
   already applied.

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
- **Missing relevant results?** Add keywords to `topics`, add another entry
  to `wikicfp_categories` (this accepts arbitrary strings, not just real
  WikiCFP categories — see the comment above it in the file), or lower
  `min_relevance_score`.
- **Deadline window too short/long?** Adjust `deadline_window_days` (default
  180) — applies to the email, website, AND the Sheet's Conferences tab (all
  three always show the same set). The Sheet's Participated tab is the
  exception: it ignores this setting entirely, by design.
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
.github/workflows/  The daily cron
```

## What's deferred (Phase 2)

A secondary search-API source (Serper.dev) for journal/society CFPs
WikiCFP misses, LLM-based extraction for that messier source, refined
predatory-venue blocklisting, and a failure-notification safety net —
see `workflows/daily_conference_digest.md`'s "known edge cases" and
"deferred" sections. Worth revisiting once Phase 1 has run cleanly for a
while.
