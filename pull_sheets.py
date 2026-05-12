import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timezone
from pymongo import UpdateOne

from config import CREDENTIALS_FILE, SPREADSHEET_ID, SHEET_NAME
from database import plays_col

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
gc = gspread.authorize(creds)
sheet = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)

# Puts Dates into MongoDB Dates
def parse_date(date_str: str) -> str | None:
    date_str = str(date_str).strip()

    if not date_str:
        return None

    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    return None

# Converts sheet value into numbers
def parse_float(val: str) -> float | None:
    val = str(val).strip().replace(",", "").replace("u", "")

    if not val:
        return None

    try:
        return float(val)
    except ValueError:
        return None

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

# 
def main():
    print("Fetching rows from Google Sheet...")

    all_rows = sheet.get_all_values()

    # Skip the first 11 header/summary rows
    data_rows = all_rows[11:]

    ops = []
    skipped = 0
    parsed = 0

    for sheet_row, row in enumerate(data_rows, start=12):
        if not row or not row[0].strip():
            skipped += 1
            continue

        date_raw = row[0].strip() if len(row) > 0 else ""
        capper = row[1].strip() if len(row) > 1 else ""
        selection = row[2].strip() if len(row) > 2 else ""
        stake_raw = row[3].strip() if len(row) > 3 else ""
        odds = row[4].strip() if len(row) > 4 else ""
        win_col = row[5].strip() if len(row) > 5 else ""
        profit_raw = row[8].strip() if len(row) > 8 else ""

        if not selection:
            skipped += 1
            continue

        date_str = parse_date(date_raw)

        if not date_str:
            skipped += 1
            continue

        stake = parse_float(stake_raw)
        profit = parse_float(profit_raw)
        result = map_result(win_col, profit)

        # Turns date into real python date time
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            skipped += 1
            continue

        # Creates document to save into MongoDB
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

        parsed += 1

    if not ops:
        print("No rows found to sync.")
        return

    print(f"Parsed {parsed} rows, skipped {skipped} empty rows")
    print("Syncing to MongoDB...")

    result = plays_col.bulk_write(ops)

    print(
        f"{result.upserted_count} new rows inserted, "
        f"{result.modified_count} rows updated."
    )


if __name__ == "__main__":
    main()