"""Deterministic orchestrator for the Free & Funded Opportunities digest.

This is the "Tools" layer executing the full SOP documented in
workflows/daily_conference_digest.md end to end, unattended — there is no
agent/human in the loop when GitHub Actions fires this on a cron schedule,
so every decision here is fixed in code rather than made live.

Steps: fetch (WikiCFP + search incl. free/funded priority queries + UM
Event System + standalone funding programs) -> normalize -> classify ->
dedupe/store -> extract_access (free / fee-waivable / funded verdict backed
by a verbatim quote from the official pages) -> publish_sheet (reads back
any Status/Notes you set in the Sheet, incl. "Dismissed", and persists them
to data/conferences_db.json BEFORE the next step) -> filter/rank into the
four eligible-only sections -> render -> send email. publish_sheet runs
before filter/rank specifically so a record you dismiss today is already
gone from today's email/website, not just next run's.

Exits non-zero on a hard failure so a broken run shows red in Actions
instead of silently "succeeding" empty-handed.

Usage:
    python tools/run_pipeline.py            # full run, sends email
    python tools/run_pipeline.py --no-email # build + render, skip sending
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime

import classify_relevance
import dedupe_and_store
import extract_access
import fetch_funding_programs
import fetch_search_api
import fetch_um_events
import fetch_wikicfp
import filter_and_rank
import normalize_records
import publish_sheet
import render_digest
import send_email
from common import TMP_DIR, load_config

LOG_PATH = TMP_DIR / "pipeline_run.log"


def log(message: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {message}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main(send: bool = True) -> int:
    LOG_PATH.parent.mkdir(exist_ok=True)
    log("=== Daily digest pipeline starting ===")

    try:
        config = load_config()

        log("Step 1/11: fetch_wikicfp")
        raw = fetch_wikicfp.run(config)
        log(f"  -> {len(raw)} candidate records fetched")

        log("Step 2/11: fetch_search_api (free/funded priority queries + rotating CFP pool, via Serper.dev + Groq)")
        search_records = fetch_search_api.run(config)
        log(f"  -> {len(search_records)} candidate records found via search")

        log("Step 3/11: fetch_um_events (Universiti Malaya's own event system, via Playwright + Groq)")
        um_records = fetch_um_events.run(config)
        log(f"  -> {len(um_records)} candidate record(s) found via UM Event System")

        log("Step 4/11: fetch_funding_programs (standalone travel grants / scholarships)")
        program_records = fetch_funding_programs.run(config)
        log(f"  -> {len(program_records)} funding program record(s)")

        log("Step 5/11: normalize_records")
        normalized = normalize_records.run(raw) + search_records + um_records + program_records
        log(f"  -> {len(normalized)} records normalized (all sources combined)")

        log("Step 6/11: classify_relevance")
        classified = classify_relevance.run(normalized, config)
        log(f"  -> {len(classified)} records classified")

        log("Step 7/11: dedupe_and_store")
        db_records = dedupe_and_store.run(classified)
        log(f"  -> {len(db_records)} records in persistent DB")

        log("Step 8/11: extract_access (free / fee-waivable / funded verdict, evidence-checked)")
        n_checked = extract_access.run(db_records, config)
        dedupe_and_store.save_db({r.dedup_key: r for r in db_records})
        log(f"  -> {n_checked} record(s) checked for cost/funding")

        log("Step 9/11: publish_sheet (syncs Status/Notes back from the Sheet "
            "first, incl. Dismissed, then writes the section tabs + Malaysia + Participated)")
        published = publish_sheet.run(db_records, config)
        log(f"  -> Google Sheet published: {published}")

        log("Step 10/11: filter_and_rank (eligible-only sections)")
        sections = filter_and_rank.sections(db_records, config)
        for key, recs in sections.items():
            log(f"  -> {filter_and_rank.SECTION_TITLES[key]}: {len(recs)}")

        log("Step 11/11: render_digest")
        email_html, site_html = render_digest.run(sections)
        log("  -> docs/index.html written")

        if send:
            log("Sending email")
            sent = send_email.run(email_html, config)
            log(f"  -> email sent: {sent}")
            if not sent:
                log("WARNING: pipeline completed but email did not send "
                    "(see message above — commonly a missing/invalid RESEND_API_KEY).")
        else:
            log("Sending email — SKIPPED (--no-email)")

        log("=== Pipeline completed successfully ===")
        return 0

    except Exception:
        log("=== Pipeline FAILED ===")
        log(traceback.format_exc())
        return 1


if __name__ == "__main__":
    send_flag = "--no-email" not in sys.argv
    sys.exit(main(send=send_flag))
