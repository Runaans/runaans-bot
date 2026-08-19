"""Google Sheet -> MongoDB sync engine.

All connections are created lazily so this module is safe to import
anywhere (bot, CLI scripts, tests) without side effects.
"""

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials
from pymongo import UpdateOne

from config import (
    CREDENTIALS_FILE,
    SPREADSHEET_ID,
    SHEET_NAME,
    SHEET_DATA_START_ROW,
    SHEET_RETRIES,
)
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
            more = "..." if len(self.bad_date_rows) > 5 else ""
            text += (
                f"\nWarning: {len(self.bad_date_rows)} row(s) skipped for an "
                f"unreadable date: {shown}{more}"
            )

        # Deliberately ASCII-only: pull_sheets.py prints this to a Windows
        # console, where a non-cp1252 character raises UnicodeEncodeError.
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


# Google returns these when we're rate-limited or it's having a moment.
# Anything else (403 no access, 404 wrong id) will never fix itself.
RETRYABLE_STATUSES = {429, 500, 502, 503}


def _retry_after(error) -> float | None:
    try:
        return float(error.response.headers.get("Retry-After"))
    except (AttributeError, TypeError, ValueError):
        return None


def fetch_rows() -> list[list[str]]:
    """Read the whole worksheet, retrying transient Google API failures.

    /recap syncs before it renders, so a single 429 would otherwise turn the
    most-used command into a stale recap.
    """
    delay = 2.0

    for attempt in range(1, SHEET_RETRIES + 1):
        try:
            sheet = _get_client().open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
            return sheet.get_all_values()
        except gspread.exceptions.APIError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)

            if status not in RETRYABLE_STATUSES or attempt == SHEET_RETRIES:
                raise

            # Honour Retry-After when Google sends one, otherwise back off
            # exponentially with jitter. Capped so an interactive /recap
            # still answers promptly.
            retry_after = _retry_after(e)
            wait = retry_after if retry_after is not None else delay + random.uniform(0, 1)
            wait = max(0.5, min(wait, 15.0))

            log.warning(
                "Sheets read failed with %s (attempt %s/%s), retrying in %.1fs",
                status, attempt, SHEET_RETRIES, wait,
            )
            time.sleep(wait)
            delay *= 2

    # Unreachable: the loop either returns or raises
    raise RuntimeError("Sheets read exhausted retries")


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

    all_rows = fetch_rows()
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
