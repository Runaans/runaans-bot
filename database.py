import logging

from pymongo import MongoClient
from pymongo.errors import ConfigurationError
from config import MONGO_URI, DB_NAME

log = logging.getLogger("runaans.database")

# A mongodb+srv:// URI is resolved via DNS right here, at construction, so a
# deleted or renamed Atlas cluster fails on import with a pymongo traceback.
# Translate it into something that says what to actually do about it.
try:
    # Fail fast instead of blocking a worker thread (and the sync lock behind
    # it) for the default 30s when Mongo is unreachable.
    mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
except ConfigurationError as e:
    host = MONGO_URI.split("@")[-1].split("/")[0]
    raise RuntimeError(
        f"Could not resolve the MongoDB host '{host}' from MONGO_URI.\n"
        "The cluster was most likely deleted or renamed - an idle Atlas free "
        "cluster gets paused and can eventually be removed.\n"
        "Check the cluster in MongoDB Atlas and update MONGO_URI in .env.\n"
        f"(underlying error: {e})"
    ) from e

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
