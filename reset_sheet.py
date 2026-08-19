"""Deletes ALL sheet-imported plays from MongoDB. Use with care.

Run `python reset_sheet.py` and confirm, or pass --yes to skip the prompt.
"""

import sys

from config import DB_NAME
from database import plays_col

QUERY = {
    "$or": [
        {"posted_by": "imported"},
        {"source": "sheet"},
    ]
}


def main():
    count = plays_col.count_documents(QUERY)

    if count == 0:
        print(f"No imported plays found in '{DB_NAME}'. Nothing to delete.")
        return

    if "--yes" not in sys.argv:
        try:
            answer = input(
                f"This will permanently delete {count} imported plays "
                f"from '{DB_NAME}'. Type 'yes' to continue: "
            )
        except (EOFError, KeyboardInterrupt):
            # No interactive stdin (scheduler, piped input) - never assume yes
            print("\nAborted (no confirmation received). Pass --yes to skip the prompt.")
            return

        if answer.strip().lower() != "yes":
            print("Aborted.")
            return

    result = plays_col.delete_many(QUERY)
    print(f"Deleted {result.deleted_count} old plays.")


if __name__ == "__main__":
    main()
