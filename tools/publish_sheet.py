"""Publish the full conference history to a Google Sheet — a running
archive, unlike docs/index.html and the email (which are this-week
snapshots that drop entries once their deadline passes or they fall out of
the relevance/window filters).

Auth: a Google Cloud **service account** (not OAuth) — the right choice for
unattended weekly automation, since it never needs interactive re-consent.
Requires two env vars (GitHub Secrets in production, .env locally):
  GOOGLE_SERVICE_ACCOUNT_JSON — the full service account key JSON, as a
    single string (paste the downloaded .json file's contents verbatim).
  GOOGLE_SHEET_ID — the target spreadsheet's ID (from its URL, the
    long string between /d/ and /edit). The sheet must be shared with the
    service account's `client_email` (found inside the JSON key) as Editor.

Design: every run reads the current sheet, preserves the user-editable
Status/Notes columns keyed by each record's stable dedup_key, then rewrites
the whole grid in a single batched `update()` call — cheap on Sheets API
quota (one read + one write per run, regardless of row count) and avoids
row-ordering edge cases that per-cell updates would risk.

Run standalone against the persistent DB:
    python tools/publish_sheet.py
"""
from __future__ import annotations

import json
import os
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from common import ConferenceRecord, PROJECT_ROOT, load_config
import filter_and_rank

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HEADERS = [
    "Title", "Status", "Submission Deadline", "Notification Date",
    "Conference Start", "Conference End", "Location", "Mode",
    "Matched Topics", "Relevance Score", "Official Link", "Verify (CFP) Link",
    "First Seen", "Last Verified", "Notes", "Source ID",
]
# Columns the script must never overwrite once a user has filled them in.
USER_EDITABLE_COLUMNS = {"Status", "Notes"}

WORKSHEET_TITLE = "Conferences"


def get_client() -> Optional[gspread.Client]:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        return None
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_worksheet(client: gspread.Client, sheet_id: str) -> gspread.Worksheet:
    spreadsheet = client.open_by_key(sheet_id)
    try:
        return spreadsheet.worksheet(WORKSHEET_TITLE)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=WORKSHEET_TITLE, rows=200, cols=len(HEADERS))


def read_existing_user_columns(worksheet: gspread.Worksheet) -> dict[str, dict[str, str]]:
    """Return {source_id: {"Status": ..., "Notes": ...}} from whatever's
    currently in the sheet, so a rewrite doesn't clobber manual edits."""
    values = worksheet.get_all_values()
    if not values:
        return {}
    header = values[0]
    try:
        id_col = header.index("Source ID")
    except ValueError:
        return {}
    col_indices = {c: header.index(c) for c in USER_EDITABLE_COLUMNS if c in header}

    preserved = {}
    for row in values[1:]:
        if len(row) <= id_col or not row[id_col]:
            continue
        source_id = row[id_col]
        preserved[source_id] = {
            col: (row[idx] if idx < len(row) else "")
            for col, idx in col_indices.items()
        }
    return preserved


def record_to_row(record: ConferenceRecord, preserved: dict[str, dict[str, str]]) -> list[str]:
    saved = preserved.get(record.dedup_key, {})
    return [
        record.title,
        saved.get("Status", ""),
        record.submission_deadline or "",
        record.notification_date or "",
        record.conference_start or "",
        record.conference_end or "",
        record.location or "",
        record.mode,
        ", ".join(record.matched_topics),
        f"{record.relevance_score:.2f}",
        record.url or "",
        record.cfp_url or "",
        record.first_seen or "",
        record.last_verified or "",
        saved.get("Notes", ""),
        record.dedup_key,
    ]


def run(records: Optional[list[ConferenceRecord]] = None, config: Optional[dict] = None) -> bool:
    config = config or load_config()
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")

    client = get_client()
    if not client or not sheet_id:
        print("GOOGLE_SERVICE_ACCOUNT_JSON / GOOGLE_SHEET_ID not set — skipping "
              "Google Sheet publish (set both in .env for local runs or as "
              "GitHub Actions secrets).")
        return False

    if records is None:
        records = filter_and_rank.all_relevant(config=config)

    worksheet = get_worksheet(client, sheet_id)
    preserved = read_existing_user_columns(worksheet)

    rows = [HEADERS] + [record_to_row(r, preserved) for r in records]
    worksheet.clear()
    worksheet.update(values=rows, range_name="A1")
    worksheet.freeze(rows=1)
    return True


if __name__ == "__main__":
    ok = run()
    print("Google Sheet published." if ok else "Google Sheet NOT published.")
