# runaans-bot
Discord bot for posting Runaans Locks daily recaps. The bot pulls play/result data from a Google Sheet, stores it in MongoDB, and posts formatted recap embeds in a Discord channel using the `/recap` command.

## Features

- `/recap` slash command for posting daily betting recaps
- Pulls data from Google Sheets into MongoDB
- Calculates daily, monthly, yearly, and all-time unit totals
- Owner-only command protection
- Uses environment variables to keep tokens and credentials private
