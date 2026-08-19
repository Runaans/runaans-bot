# runaans-bot

Discord bot for posting Runaans Locks daily recaps. The bot pulls play/result data from a Google Sheet, mirrors it into MongoDB, and posts formatted recap embeds in a Discord channel.

## Features

- `/recap [date] [preview]` — posts the daily recap (syncs the sheet first, so it's always current)
  - `date` accepts `2026-08-19`, `8/19/2026`, `8/19/26`, and similar formats; blank = today (Eastern)
  - `preview: True` shows the recap only to you before posting it publicly
- `/sync` — pulls the latest sheet data into MongoDB on demand
- Background auto-sync every 15 minutes (configurable), so the database always matches the sheet — including rows that were edited or deleted
- Optional scheduled daily recap post (off by default)
- Daily record (W-L-P), pending count, and monthly / yearly / all-time unit totals
- Embed turns green on winning days and red on losing days
- Owner-only command protection
- Uses environment variables to keep tokens and credentials private

## Setup

1. Install dependencies:

   ```
   pip install -r requirements.txt
   ```

2. Create a `.env` file with:

   | Variable | Required | Description |
   | --- | --- | --- |
   | `DISCORD_TOKEN` | yes | Bot token |
   | `GUILD_ID` | yes | Server ID the commands sync to |
   | `RECAP_CHANNEL` | yes | Channel ID recaps are posted in |
   | `OWNER_ID` | yes | Your Discord user ID |
   | `MONGO_URI` | yes | MongoDB connection string |
   | `GOOGLE_CREDENTIALS_FILE` | yes | Path to the service-account JSON |
   | `SPREADSHEET_ID` | yes | Google Sheet ID |
   | `SHEET_NAME` | no | Worksheet name (default `TOKEN PICKS`) |
   | `DB_NAME` | no | Mongo database name (default `runaanslocks`) |
   | `SHEET_DATA_START_ROW` | no | First data row in the sheet (default `12`) |
   | `AUTO_SYNC_MINUTES` | no | Background sync interval, `0` disables (default `15`) |
   | `AUTO_RECAP_ENABLED` | no | `1` to auto-post the recap daily (default off) |
   | `AUTO_RECAP_TIME` | no | Daily auto-recap time in Eastern, `HH:MM` (default `23:00`) |

3. Run the bot:

   ```
   python main.py
   ```

## Scripts

- `python pull_sheets.py` — one-off manual sheet sync (the bot normally handles this itself)
- `python reset_sheet.py` — deletes all imported plays from MongoDB (asks for confirmation; `--yes` to skip)
