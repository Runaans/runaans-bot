"""Google Sheet -> MongoDB sync engine.

All connections are created lazily so this module is safe to import
anywhere (bot, CLI scripts, tests) without side effects.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials
from pymongo import UpdateOne

from config import CREDENTIALS_FILE, SPREADSHEET_ID, SHEET_NAME, SHEET_DATA_START_ROW
from database import plays_col
from helpers import normalize_date, parse_units

log = logging.getLogger("runaans.sync")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_gc = None


def _get_client() -> gspread.Client:
    global _gc

    if _gc is None:
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
        _gc = gspread.authorize(creds)

    return _gc


@dataclass
class SyncStats:
    parsed: int = 0
    skipped: int = 0
    inserted: int = 0
    updated: int = 0
    deleted: int = 0

    def summary(self) -> str:
        return (
            f"{self.parsed} rows parsed, {self.inserted} new, "
            f"{self.updated} updated, {self.deleted} removed"
        )


# Whether it's a win, loss, push, pending
def map_result(win_col: str, profit: float | None) -> str | None:
    w = str(win_col).strip().upper()

    if w in ("W", "L", "P"):
        return w

    if profit is None:
        return None

    if profit > 0:
        return "W"

    if profit < 0:
        return "L"

    return "P"


def _cell(row: list, index: int) -> str:
    return row[index].strip() if len(row) > index else ""


def sync_sheet() -> SyncStats:
    """Pull all rows from the sheet and mirror them into MongoDB.

    Rows that disappeared from the sheet are deleted from MongoDB so the
    database always matches the sheet exactly.
    """
    stats = SyncStats()

    sheet = _get_client().open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
    all_rows = sheet.get_all_values()

    data_rows = all_rows[SHEET_DATA_START_ROW - 1:]

    ops = []
    seen_rows = []

    for sheet_row, row in enumerate(data_rows, start=SHEET_DATA_START_ROW):
        date_raw = _cell(row, 0)
        capper = _cell(row, 1)
        selection = _cell(row, 2)
        stake_raw = _cell(row, 3)
        odds = _cell(row, 4)
        win_col = _cell(row, 5)
        profit_raw = _cell(row, 8)

        if not date_raw or not selection:
            stats.skipped += 1
            continue

        date_str = normalize_date(date_raw)

        if not date_str:
            stats.skipped += 1
            continue

        stake = parse_units(stake_raw)
        profit = parse_units(profit_raw)
        result = map_result(win_col, profit)

        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)

        doc = {
            "source": "sheet",
            "sheet_row": sheet_row,
            "spreadsheet_id": SPREADSHEET_ID,
            "sheet_name": SHEET_NAME,

            "title": selection,
            "description": "",
            "unit_index": 0,
            "units": stake if stake is not None else 0.0,
            "odds": odds,
            "result": result,
            "profit": profit if profit is not None else 0.0,
            "capper": capper,

            "message_id": None,
            "channel_id": None,
            "posted_by": "imported",

            "date": date_str,
            "timestamp": dt,
            "updated_at": datetime.now(timezone.utc),
        }

        ops.append(
            UpdateOne(
                {
                    "source": "sheet",
                    "spreadsheet_id": SPREADSHEET_ID,
                    "sheet_name": SHEET_NAME,
                    "sheet_row": sheet_row,
                },
                {"$set": doc},
                upsert=True,
            )
        )

        seen_rows.append(sheet_row)
        stats.parsed += 1

    # Safety net: if nothing parsed, something is likely wrong with the
    # sheet - don't treat it as "everything was deleted" and wipe the DB.
    if not ops:
        log.warning("No parseable rows found in sheet; skipping cleanup")
        return stats

    result = plays_col.bulk_write(ops)
    stats.inserted = result.upserted_count
    stats.updated = result.modified_count

    # Remove rows that no longer exist in the sheet (deleted/cleared/moved)
    delete_result = plays_col.delete_many(
        {
            "source": "sheet",
            "spreadsheet_id": SPREADSHEET_ID,
            "sheet_name": SHEET_NAME,
            "sheet_row": {"$nin": seen_rows},
        }
    )
    stats.deleted = delete_result.deleted_count

    log.info("Sheet sync complete: %s", stats.summary())
    return stats
