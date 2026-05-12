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