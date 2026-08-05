"""Deterministic orchestrator for the weekly digest.

This is the "Tools" layer executing the full SOP documented in
workflows/weekly_conference_digest.md end to end, unattended — there is no
agent/human in the loop when GitHub Actions fires this on a cron schedule,
so every decision here is fixed in code rather than made live.

Steps: fetch -> normalize -> classify -> dedupe/store -> filter/rank ->
render -> send email (+ docs/index.html is written as a side effect of
render, ready to be committed to the repo for GitHub Pages by the calling
GitHub Actions workflow).

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
import fetch_wikicfp
import filter_and_rank
import normalize_records
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
    log("=== Weekly digest pipeline starting ===")

    try:
        config = load_config()

        log("Step 1/7: fetch_wikicfp")
        raw = fetch_wikicfp.run(config)
        log(f"  -> {len(raw)} candidate records fetched")

        log("Step 2/7: normalize_records")
        normalized = normalize_records.run(raw)
        log(f"  -> {len(normalized)} records normalized")

        log("Step 3/7: classify_relevance")
        classified = classify_relevance.run(normalized, config)
        log(f"  -> {len(classified)} records classified")

        log("Step 4/7: dedupe_and_store")
        db_records = dedupe_and_store.run(classified)
        log(f"  -> {len(db_records)} records in persistent DB")

        log("Step 5/7: filter_and_rank")
        ranked = filter_and_rank.run(db_records, config)
        log(f"  -> {len(ranked)} records pass filters, within deadline window")

        log("Step 6/7: render_digest")
        email_html, site_html = render_digest.run(ranked)
        log("  -> docs/index.html written")

        if send:
            log("Step 7/7: send_email")
            sent = send_email.run(email_html, config)
            log(f"  -> email sent: {sent}")
            if not sent:
                log("WARNING: pipeline completed but email did not send "
                    "(see message above — commonly a missing/invalid RESEND_API_KEY).")
        else:
            log("Step 7/7: send_email — SKIPPED (--no-email)")

        log("=== Pipeline completed successfully ===")
        return 0

    except Exception:
        log("=== Pipeline FAILED ===")
        log(traceback.format_exc())
        return 1


if __name__ == "__main__":
    send_flag = "--no-email" not in sys.argv
    sys.exit(main(send=send_flag))
