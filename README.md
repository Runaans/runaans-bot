# runaans-bot

Discord bot for posting Runaans Locks daily recaps. The bot pulls play/result data from a Google Sheet, mirrors it into MongoDB, and posts formatted recap embeds in a Discord channel.

## Commands

All commands are owner-only and reply privately unless they post to the recap channel.

| Command | What it does |
| --- | --- |
| `/recap [date] [preview]` | Posts the daily recap. Syncs the sheet first, so it's always current. `date` autocompletes to dates that actually have plays and accepts `2026-08-19`, `8/19/2026`, `8/19/26`, etc.; blank = today (Eastern). `preview: True` shows it only to you first. |
| `/stats [period] [capper] [post]` | Record, units, and ROI for today / last 7 days / month / year / all-time, with a per-capper leaderboard. `capper` autocompletes from names in the sheet and narrows to one person; `post: True` shares it in the recap channel. |
| `/pending` | Lists every play that hasn't been graded yet, with odds, units, and capper. |
| `/sync` | Pulls the latest sheet data into MongoDB on demand. |
| `/status` | Health check: last sync result and time, auto-sync/auto-recap settings, play counts, uptime. |

## Features

- Background auto-sync every 15 minutes (configurable), so the database always mirrors the sheet — including rows that were edited, deleted, or moved
- Optional scheduled daily recap post (off by default)
- Retries rate-limited Google Sheets reads with backoff, so a busy moment doesn't turn `/recap` into stale data
- DMs you if the background sync fails repeatedly, reminds you while it stays broken, and tells you when it recovers
- Rolling log file in `logs/bot.log` so overnight failures leave a trail
- Daily record (W-L-P), pending count, and monthly / yearly / all-time unit totals
- Embeds turn green on winning days and red on losing days
- Warns you when a sheet row is skipped because its date can't be read, instead of dropping the play silently
- Owner-only command protection
- Uses environment variables to keep tokens and credentials private

## Setup

1. Install dependencies:

   ```
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill it in:

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
   | `SHEET_RETRIES` | no | Retries for a rate-limited Sheets read (default `3`) |
   | `BRAND` | no | Name in embed titles/footers (default `Runaans Locks`) |

3. Run the bot:

   ```
   python main.py
   ```

## Running it unattended

`run_bot.bat` starts the bot and restarts it if it exits, giving up after 10 restarts so a broken `.env` doesn't spin forever. To have it survive reboots, add it to Windows Task Scheduler with the "At log on" trigger and "Restart if the task fails" enabled.

## Scripts

- `python pull_sheets.py` — one-off manual sheet sync (the bot normally handles this itself)
- `python reset_sheet.py` — deletes all imported plays from MongoDB (asks for confirmation; `--yes` to skip)

## Tests

```
python -m unittest
```

No Discord, MongoDB, or Google connection is needed — the environment is faked and the database is stubbed.
