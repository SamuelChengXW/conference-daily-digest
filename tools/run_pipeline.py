"""Deterministic orchestrator for the daily digest.

This is the "Tools" layer executing the full SOP documented in
workflows/daily_conference_digest.md end to end, unattended — there is no
agent/human in the loop when GitHub Actions fires this on a cron schedule,
so every decision here is fixed in code rather than made live.

Steps: fetch (WikiCFP + Malaysia search) -> normalize -> classify ->
dedupe/store -> fee/travel LLM extraction (new records only) ->
publish_sheet (reads back any Status/Notes you set in the Sheet, incl.
"Dismissed", and persists them to data/conferences_db.json BEFORE the next
step) -> filter/rank -> render -> send email. publish_sheet runs before
filter/rank specifically so a conference you dismiss today is already gone
from today's email/website, not just next run's.

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
import fetch_search_api
import fetch_wikicfp
import filter_and_rank
import llm_extract
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

        log("Step 1/9: fetch_wikicfp")
        raw = fetch_wikicfp.run(config)
        log(f"  -> {len(raw)} candidate records fetched")

        log("Step 2/9: fetch_search_api (Malaysia university sources via Serper.dev + Claude)")
        search_records = fetch_search_api.run(config)
        log(f"  -> {len(search_records)} candidate records found via search")

        log("Step 3/9: normalize_records")
        normalized = normalize_records.run(raw) + search_records
        log(f"  -> {len(normalized)} records normalized (WikiCFP + search combined)")

        log("Step 4/9: classify_relevance")
        classified = classify_relevance.run(normalized, config)
        log(f"  -> {len(classified)} records classified")

        log("Step 5/9: dedupe_and_store")
        db_records = dedupe_and_store.run(classified)
        log(f"  -> {len(db_records)} records in persistent DB")

        log("Step 6/9: llm_extract (fee/travel info, new records only)")
        n_extracted = llm_extract.run(db_records)
        log(f"  -> {n_extracted} record(s) sent for fee/travel extraction")

        log("Step 7/9: publish_sheet (syncs Status/Notes back from the Sheet "
            "first, incl. Dismissed, then writes Conferences + Malaysia + Participated tabs)")
        published = publish_sheet.run(db_records, config)
        log(f"  -> Google Sheet published: {published}")

        log("Step 8/9: filter_and_rank")
        ranked = filter_and_rank.run(db_records, config)
        log(f"  -> {len(ranked)} records pass filters, within deadline window, not dismissed")

        log("Step 9/9: render_digest")
        email_html, site_html = render_digest.run(ranked)
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
