"""CLI wrapper: sync the Google Sheet into MongoDB once and exit.

The bot now syncs automatically (see cogs/sync.py), so this is only
needed for manual one-off syncs from the terminal.
"""

import logging

from database import ensure_indexes
from sheets_sync import sync_sheet


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    print("Fetching rows from Google Sheet...")
    ensure_indexes()
    stats = sync_sheet()
    print(f"Done: {stats.summary()} ({stats.skipped} empty rows skipped)")


if __name__ == "__main__":
    main()
