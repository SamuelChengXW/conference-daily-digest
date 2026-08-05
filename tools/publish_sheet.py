"""Publish to a two-tab Google Sheet:

  "Conferences"  — current/upcoming only. Same windowed, non-expired list as
                   the email/website (via filter_and_rank.run()) — a
                   conference automatically drops off this tab once its
                   deadline passes, no manual cleanup needed. Has a Status
                   dropdown (see STATUS_OPTIONS) so you can mark one
                   "Dismissed" to permanently hide it (see below), or track
                   "Planned to Submit" / "In Progress" while it's still
                   upcoming.
  "Participated" — a permanent log of anything ever marked Submitted /
                   Accepted / Rejected. Unlike the Conferences tab, entries
                   here are NEVER dropped just because the deadline passed —
                   this is specifically "which ones did I submit to",
                   independent of the current window.

Status persistence: this script reads the Conferences tab's current Status/
Notes BEFORE rewriting it, and writes them back onto the matching
ConferenceRecord's `user_status`/`user_notes` fields — then saves the
*persistent* data/conferences_db.json (not just the Sheet) via
dedupe_and_store.save_db(), so status survives even if the Sheet were ever
lost. This is also what makes "Dismissed" stick: filter_and_rank.py checks
`user_status != "Dismissed"`, so a dismissed record disappears from the
Conferences tab, the website, AND the email on every subsequent run — not
just visually hidden in the Sheet.

Auth: a Google Cloud **service account** (not OAuth) — the right choice for
unattended daily automation, since it never needs interactive re-consent.
Requires two env vars (GitHub Secrets in production, .env locally):
  GOOGLE_SERVICE_ACCOUNT_JSON — the full service account key JSON, as a
    single string (paste the downloaded .json file's contents verbatim).
  GOOGLE_SHEET_ID — the target spreadsheet's ID (from its URL, the
    long string between /d/ and /edit). The sheet must be shared with the
    service account's `client_email` (found inside the JSON key) as Editor.

Run standalone against the persistent DB:
    python tools/publish_sheet.py
"""
from __future__ import annotations

import json
import os
from typing import Optional

import gspread
from gspread.utils import ValidationConditionType
from google.oauth2.service_account import Credentials

from common import ConferenceRecord, DB_PATH, PROJECT_ROOT, load_config, read_json
import dedupe_and_store
import filter_and_rank

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HEADERS = [
    "Title", "Status", "Region", "Submission Deadline", "Notification Date",
    "Conference Start", "Conference End", "Location", "Mode",
    "Matched Topics", "Relevance Score", "Official Link", "Verify (CFP) Link",
    "First Seen", "Last Verified", "Notes", "Source ID",
]
STATUS_COL_INDEX = HEADERS.index("Status")  # 0-based

# Shown as a dropdown (data validation) in the Status column of both tabs.
STATUS_OPTIONS = [
    "Interested",
    "Planned to Submit",
    "In Progress",
    "Submitted",
    "Accepted",
    "Rejected",
    "Dismissed",
]
# Setting Status to this permanently hides a conference from the Conferences
# tab, website, and email (see filter_and_rank.run()'s user_status check) —
# not just this run, going forward, since it's persisted to the JSON DB.
DISMISS_STATUS = "Dismissed"
# Landing in the Participated tab, and staying there even after the
# Conferences tab windows it out once its deadline passes.
PARTICIPATED_STATUSES = {"Submitted", "Accepted", "Rejected"}

CONFERENCES_TAB = "Conferences"
PARTICIPATED_TAB = "Participated"


def get_client() -> Optional[gspread.Client]:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        return None
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_or_create_worksheet(spreadsheet: gspread.Spreadsheet, title: str) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=300, cols=len(HEADERS))


def read_sheet_rows(worksheet: gspread.Worksheet) -> dict[str, dict[str, str]]:
    """Return {source_id: {header: value, ...}} for every current row —
    used both to preserve Status/Notes and, for the Participated tab, to
    keep rows for records that may no longer be in the live DB.
    """
    values = worksheet.get_all_values()
    if not values:
        return {}
    header = values[0]
    try:
        id_col = header.index("Source ID")
    except ValueError:
        return {}

    rows = {}
    for row in values[1:]:
        if len(row) <= id_col or not row[id_col]:
            continue
        source_id = row[id_col]
        rows[source_id] = {h: (row[i] if i < len(row) else "") for i, h in enumerate(header)}
    return rows


def sync_user_fields(db_records: list[ConferenceRecord], existing: dict[str, dict[str, str]]) -> None:
    """Pull Status/Notes from the Conferences tab's current content (read
    BEFORE this run rewrites it) onto the matching record's user_status/
    user_notes — mutates db_records in place. Caller is responsible for
    persisting db_records afterward.
    """
    for record in db_records:
        saved = existing.get(record.dedup_key)
        if not saved:
            continue
        record.user_status = saved.get("Status") or None
        record.user_notes = saved.get("Notes") or None


def record_to_row(record: ConferenceRecord) -> list[str]:
    return [
        record.title,
        record.user_status or "",
        record.region_match or "",
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
        record.user_notes or "",
        record.dedup_key,
    ]


def raw_row_from_dict(d: dict[str, str]) -> list[str]:
    """Fallback for a Participated-tab row whose source record is no longer
    in the DB for some reason — pass its last-known values through as-is."""
    return [d.get(h, "") for h in HEADERS]


def apply_status_dropdown(worksheet: gspread.Worksheet, num_rows: int) -> None:
    # Data starts at row 2 (row 1 is the header). +50 buffer rows past the
    # last data row so the dropdown is already there if the tab ever grows
    # without needing to reapply validation every run.
    last_data_row = num_rows + 1
    end_row = last_data_row + 50
    col_letter = chr(ord("A") + STATUS_COL_INDEX)
    worksheet.add_validation(
        f"{col_letter}2:{col_letter}{end_row}",
        ValidationConditionType.one_of_list,
        STATUS_OPTIONS,
        showCustomUi=True,
    )


def write_tab(worksheet: gspread.Worksheet, rows: list[list[str]]) -> None:
    worksheet.clear()
    worksheet.update(values=[HEADERS] + rows, range_name="A1")
    worksheet.freeze(rows=1)
    apply_status_dropdown(worksheet, len(rows))


def run(db_records: Optional[list[ConferenceRecord]] = None, config: Optional[dict] = None) -> bool:
    config = config or load_config()
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")

    client = get_client()
    if not client or not sheet_id:
        print("GOOGLE_SERVICE_ACCOUNT_JSON / GOOGLE_SHEET_ID not set — skipping "
              "Google Sheet publish (set both in .env for local runs or as "
              "GitHub Actions secrets).")
        return False

    if db_records is None:
        raw = read_json(DB_PATH, default={})
        db_records = [ConferenceRecord.from_dict(v) for v in raw.values()]

    spreadsheet = client.open_by_key(sheet_id)
    conf_ws = get_or_create_worksheet(spreadsheet, CONFERENCES_TAB)

    # 1. Pull Status/Notes as the user currently has them, BEFORE we
    #    overwrite the tab, and persist onto the records (-> JSON DB).
    existing_conf_rows = read_sheet_rows(conf_ws)
    sync_user_fields(db_records, existing_conf_rows)
    dedupe_and_store.save_db({r.dedup_key: r for r in db_records})

    # 2. Conferences tab: same windowed/non-expired/non-Dismissed list as
    #    the email and website, so this and those never disagree.
    main_records = filter_and_rank.run(db_records, config)
    write_tab(conf_ws, [record_to_row(r) for r in main_records])

    # 3. Participated tab: union of (rows already there) and (any DB record
    #    currently Submitted/Accepted/Rejected) — never dropped just because
    #    a deadline passed, only ever added to or refreshed.
    part_ws = get_or_create_worksheet(spreadsheet, PARTICIPATED_TAB)
    existing_part_rows = read_sheet_rows(part_ws)
    engaged = {r.dedup_key: r for r in db_records if r.user_status in PARTICIPATED_STATUSES}

    part_rows = []
    for source_id in sorted(set(existing_part_rows) | set(engaged)):
        if source_id in engaged:
            part_rows.append(record_to_row(engaged[source_id]))
        else:
            part_rows.append(raw_row_from_dict(existing_part_rows[source_id]))
    write_tab(part_ws, part_rows)

    return True


if __name__ == "__main__":
    ok = run()
    print("Google Sheet published." if ok else "Google Sheet NOT published.")
