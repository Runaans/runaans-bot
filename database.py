import logging

from pymongo import MongoClient
from config import MONGO_URI, DB_NAME

log = logging.getLogger("runaans.database")

# Fail fast instead of blocking a worker thread (and the sync lock behind it)
# for the default 30s when Mongo is unreachable.
mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = mongo[DB_NAME]
plays_col = db["plays"]


def ensure_indexes():
    """Create the indexes the bot relies on. Safe to call repeatedly."""
    try:
        plays_col.create_index(
            [("spreadsheet_id", 1), ("sheet_name", 1), ("sheet_row", 1)],
            unique=True,
            partialFilterExpression={"source": "sheet"},
            name="uniq_sheet_row",
        )
        plays_col.create_index([("date", 1)], name="date_idx")
    except Exception:
        log.warning("Could not create MongoDB indexes", exc_info=True)
