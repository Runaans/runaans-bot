"""Google Sheet -> MongoDB sync engine.

All connections are created lazily so this module is safe to import
anywhere (bot, CLI scripts, tests) without side effects.
"""

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials
from pymongo import UpdateOne

from config import CREDENTIALS_FILE, SPREADSHEET_ID, SHEET_NAME, SHEET_DATA_START_ROW
from database import plays_col
from helpers import normalize_date, parse_units

log = logging.getLogger("runaans.sync")

# Only one sync at a time (/sync, /recap and the background loop can overlap)
_sync_lock = threading.Lock()

# Result of the most recent sync attempt, for /status
last_sync = {"time": None, "stats": None, "error": None}

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
    # Sheet rows that had a selection but an unreadable date, so they were
    # dropped. Surfaced to the owner so plays don't vanish silently.
    bad_date_rows: list = field(default_factory=list)

    def summary(self) -> str:
        text = (
            f"{self.parsed} rows parsed, {self.inserted} new, "
            f"{self.updated} updated, {self.deleted} removed"
        )

        if self.bad_date_rows:
            shown = ", ".join(str(r) for r in self.bad_date_rows[:5])
            more = "…" if len(self.bad_date_rows) > 5 else ""
            text += (
                f"\n⚠️ {len(self.bad_date_rows)} row(s) skipped for an "
                f"unreadable date: {shown}{more}"
            )

        return text


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
    database always matches the sheet exactly. Concurrent calls are
    serialized; each caller still gets a fresh sync.
    """
    with _sync_lock:
        try:
            stats = _do_sync()
        except Exception as e:
            last_sync.update(
                time=datetime.now(timezone.utc), stats=None, error=str(e)
            )
            raise

        last_sync.update(
            time=datetime.now(timezone.utc), stats=stats, error=None
        )
        return stats


def _do_sync() -> SyncStats:
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

        # A real play with a date we can't read: don't drop it silently
        if not date_str:
            stats.skipped += 1
            stats.bad_date_rows.append(sheet_row)
            log.warning(
                "Row %s skipped: unreadable date %r (selection %r)",
                sheet_row, date_raw, selection,
            )
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

    # Remove anything that isn't a live row of the current sheet: rows that
    # were deleted/cleared/moved, plus leftovers from an older SHEET_NAME or
    # SPREADSHEET_ID. Without the last two clauses, renaming the sheet tab
    # would strand the old documents and double every recap total forever.
    delete_result = plays_col.delete_many(
        {
            "source": "sheet",
            "$or": [
                {"spreadsheet_id": {"$ne": SPREADSHEET_ID}},
                {"sheet_name": {"$ne": SHEET_NAME}},
                {"sheet_row": {"$nin": seen_rows}},
            ],
        }
    )
    stats.deleted = delete_result.deleted_count

    log.info("Sheet sync complete: %s", stats.summary())
    return stats
