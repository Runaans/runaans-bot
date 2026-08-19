import os
import discord
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

def get_required_env(name: str) -> str:
    value = os.getenv(name)

    if value is None or value.strip() == "":
        raise RuntimeError(f"Missing required environment variable: {name}")

    return value.strip()

EASTERN = ZoneInfo("America/Toronto")

GUILD_ID = discord.Object(id=int(get_required_env("GUILD_ID")))
RECAP_CHANNEL = int(get_required_env("RECAP_CHANNEL"))
OWNER_ID = int(get_required_env("OWNER_ID"))

PURPLE = discord.Color.from_rgb(138, 43, 226)
GREEN = discord.Color.from_rgb(0, 200, 100)
RED = discord.Color.from_rgb(220, 50, 50)

MONGO_URI = get_required_env("MONGO_URI")
TOKEN = get_required_env("DISCORD_TOKEN")

CREDENTIALS_FILE = get_required_env("GOOGLE_CREDENTIALS_FILE")
SPREADSHEET_ID = get_required_env("SPREADSHEET_ID")
SHEET_NAME = os.getenv("SHEET_NAME", "TOKEN PICKS")
DB_NAME = os.getenv("DB_NAME", "runaanslocks")

def _get_int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _get_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in ("1", "true", "yes", "on")


def _get_time_env(name: str, default: str):
    from datetime import time as dtime

    raw = os.getenv(name, default).strip()

    try:
        hour, minute = raw.split(":")
        return dtime(hour=int(hour), minute=int(minute), tzinfo=EASTERN)
    except (ValueError, AttributeError):
        hour, minute = default.split(":")
        return dtime(hour=int(hour), minute=int(minute), tzinfo=EASTERN)


# First data row of the sheet (1-based, rows above are headers/summary).
# Floored at 1: a value of 0 would slice the sheet down to a single row and
# make the sync treat every other play as deleted.
SHEET_DATA_START_ROW = max(1, _get_int_env("SHEET_DATA_START_ROW", 12))

# Background sheet -> MongoDB sync interval in minutes (0 disables it)
AUTO_SYNC_MINUTES = _get_int_env("AUTO_SYNC_MINUTES", 15)

# Automatic daily recap post (off by default so nothing posts unexpectedly)
AUTO_RECAP_ENABLED = _get_bool_env("AUTO_RECAP_ENABLED", False)
AUTO_RECAP_TIME = _get_time_env("AUTO_RECAP_TIME", "23:00")