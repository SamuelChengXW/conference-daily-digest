"""Publish the Free & Funded Opportunities tracker (2026-10-13 redesign) to
a Google Sheet. Every tab except Participated shows ONLY eligible records —
free, fee-waivable, or funded (filter_and_rank.is_eligible) — windowed,
non-expired, non-Dismissed, same data as the email/website:

  "Free to Submit"        — free or fee-waivable conferences
  "Funding Available"     — paid conferences that offer travel grants /
                            scholarships / financial aid
  "Funding Programs"      — standalone travel grants / scholarships
  "Journal Calls (No APC)"— journal special issues with no publication fee
  "Malaysia"              — eligible Malaysia-located records, plus the
                            general-AI-in-Malaysia carve-out (also
                            eligible-only; filter_and_rank.malaysia_tab)
  "Participated"          — permanent log of anything ever marked
                            Submitted / Accepted / Rejected, never dropped
                            when a deadline passes.

Each tab has a Status dropdown (STATUS_OPTIONS); "Dismissed" hides a record
everywhere, permanently.

Migration: the pre-redesign "Conferences" and "Free Submission" tabs are
read once for Status/Notes (LEGACY_TABS) so nothing you set is lost, then
deleted.

Status persistence: this script reads every tab's current Status/Notes
BEFORE rewriting them (merged,
earlier-listed tabs taking priority on conflicts — see
merge_existing_rows), and writes them back onto the matching
ConferenceRecord's `user_status`/`user_notes` fields — then saves the
*persistent* data/conferences_db.json (not just the Sheet) via
dedupe_and_store.save_db(), so status survives even if the Sheet were ever
lost. This is also what makes "Dismissed" stick: filter_and_rank.py checks
`user_status != "Dismissed"`, so a dismissed record disappears from every
tab, the website, AND the email on every subsequent run — not
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
    "Title", "Status", "Cost", "Funding", "Next Deadline", "Funding Deadline",
    "Submission Deadline", "Eligibility", "Fee", "Funding Details",
    "Evidence", "Region", "Location", "Conference Start", "Conference End",
    "Matched Topics", "Official Link", "Verify Link", "First Seen",
    "Last Verified", "Notes", "Source ID",
]
STATUS_COL_INDEX = HEADERS.index("Status")  # 0-based

# Shown as a dropdown (data validation) in the Status column of every tab.
STATUS_OPTIONS = [
    "Interested",
    "Planned to Submit",
    "In Progress",
    "Submitted",
    "Accepted",
    "Rejected",
    "Dismissed",
]
# Setting Status to this permanently hides a record from every tab, the
# website, and the email (see filter_and_rank.run()'s user_status check) —
# not just this run, going forward, since it's persisted to the JSON DB.
DISMISS_STATUS = "Dismissed"
# Landing in the Participated tab, and staying there even after the
# section tabs window it out once its deadline passes.
PARTICIPATED_STATUSES = {"Submitted", "Accepted", "Rejected"}

# Section key (filter_and_rank.SECTION_TITLES) -> tab name.
SECTION_TABS = {
    "free": "Free to Submit",
    "funded": "Funding Available",
    "programs": "Funding Programs",
    "journals": "Journal Calls (No APC)",
}
MALAYSIA_TAB = "Malaysia"
PARTICIPATED_TAB = "Participated"
# Pre-2026-10-13 tabs: read once for Status/Notes, then deleted.
LEGACY_TABS = ["Conferences", "Free Submission"]


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


def merge_existing_rows(*sources: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """Merge multiple {source_id: {header: value}} dicts (e.g. the
    Conferences and Malaysia tabs, for a record that legitimately appears
    on both) — earlier sources' non-empty fields win; later sources only
    fill in fields the earlier ones left blank. Prevents one tab silently
    blanking out a Status the user set on the other.
    """
    merged: dict[str, dict[str, str]] = {}
    for source in sources:
        for source_id, row in source.items():
            if source_id not in merged:
                merged[source_id] = dict(row)
            else:
                for key, value in row.items():
                    if not merged[source_id].get(key) and value:
                        merged[source_id][key] = value
    return merged


def sync_user_fields(db_records: list[ConferenceRecord], existing: dict[str, dict[str, str]]) -> None:
    """Pull Status/Notes from the tabs' current content (read BEFORE this
    run rewrites them) onto the matching record's user_status/
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
    evidence = ""
    if record.evidence_quote:
        evidence = f'"{record.evidence_quote}" — {record.evidence_url or ""}'
    return [
        record.title,
        record.user_status or "",
        record.cost_status,
        (record.funding_type or "yes") if record.funding_available else "",
        filter_and_rank.effective_deadline(record) or "rolling / not stated",
        record.funding_deadline or "",
        record.submission_deadline or "",
        record.eligibility or "",
        record.fee_info or "",
        record.funding_details or "",
        evidence,
        record.region_match or "",
        record.location or "",
        record.conference_start or "",
        record.conference_end or "",
        ", ".join(record.matched_topics),
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
    # Tabs created before the 2026-10-13 redesign have fewer columns than
    # the current HEADERS — grow the grid first rather than write past it.
    if worksheet.col_count < len(HEADERS):
        worksheet.add_cols(len(HEADERS) - worksheet.col_count)
    if worksheet.row_count < len(rows) + 51:
        worksheet.add_rows(len(rows) + 51 - worksheet.row_count)
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
    section_ws = {key: get_or_create_worksheet(spreadsheet, name) for key, name in SECTION_TABS.items()}
    malaysia_ws = get_or_create_worksheet(spreadsheet, MALAYSIA_TAB)
    legacy_ws = []
    for name in LEGACY_TABS:
        try:
            legacy_ws.append(spreadsheet.worksheet(name))
        except gspread.WorksheetNotFound:
            pass

    # 1. Pull Status/Notes as the user currently has them on every
    #    non-Participated tab (incl. legacy tabs, so a pre-redesign Status
    #    carries over), BEFORE we overwrite any, and persist onto the
    #    records (-> JSON DB). Merged, earlier tabs winning on conflicts.
    merged_existing = merge_existing_rows(
        *[read_sheet_rows(ws) for ws in section_ws.values()],
        read_sheet_rows(malaysia_ws),
        *[read_sheet_rows(ws) for ws in legacy_ws],
    )
    sync_user_fields(db_records, merged_existing)
    dedupe_and_store.save_db({r.dedup_key: r for r in db_records})

    # 2. One tab per digest section — same lists as the email/website.
    for key, recs in filter_and_rank.sections(db_records, config).items():
        write_tab(section_ws[key], [record_to_row(r) for r in recs])

    # 3. Malaysia tab (eligible-only, see filter_and_rank.malaysia_tab).
    malaysia_records = filter_and_rank.malaysia_tab(db_records, config)
    write_tab(malaysia_ws, [record_to_row(r) for r in malaysia_records])

    # 4. Legacy tabs: their Status/Notes are now persisted (step 1), so
    #    remove them. A spreadsheet must keep >=1 sheet — the section tabs
    #    above guarantee that.
    for ws in legacy_ws:
        spreadsheet.del_worksheet(ws)
        print(f"  Removed legacy tab '{ws.title}' (Status/Notes migrated).")

    # 5. Participated tab: union of (rows already there) and (any DB record
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
