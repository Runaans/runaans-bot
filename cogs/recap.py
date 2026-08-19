# imports
import asyncio
import logging
import re
from datetime import datetime

import discord
from discord.ext import commands, tasks
from discord import app_commands

from config import (
    GUILD_ID,
    RECAP_CHANNEL,
    PURPLE,
    GREEN,
    RED,
    EASTERN,
    AUTO_RECAP_ENABLED,
    AUTO_RECAP_TIME,
)
from database import plays_col
from helpers import owner_only, normalize_date, parse_units, SHEET_FILTER, VALID_RESULTS
from sheets_sync import sync_sheet

log = logging.getLogger("runaans.recap")

# Makes sure recaps only come from the google sheet
SOURCE_FILTER = SHEET_FILTER

# Discord embed limits
DESCRIPTION_LIMIT = 4096


def clean_result(value):
    return (value or "").upper().strip()


## Formats unit totals
def fmt(value):
    # `+ 0.0` collapses -0.0 to 0.0, which would otherwise render "+-0.00u"
    value = value + 0.0
    return f"{value:+.2f}u"


# Calculates Monthly, Yearly, All-time totals (blocking - run in a thread)
def sum_profit(extra_query=None):
    extra_query = extra_query or {}

    # MongoDB aggregation pipeline
    pipeline = [
        # Filter Records
        {
            "$match": {
                "$and": [
                    SOURCE_FILTER,
                    {"title": {"$ne": "Untitled"}},
                    extra_query,
                ]
            }
        },
        {
            "$addFields": {
                "resultUpper": {
                    "$toUpper": {
                        "$ifNull": ["$result", ""]
                    }
                },
                "profitNum": {
                    "$convert": {
                        "input": "$profit",
                        "to": "double",
                        "onError": 0,
                        "onNull": 0,
                    }
                }
            }
        },
        {
            "$match": {
                "resultUpper": {"$in": list(VALID_RESULTS)}
            }
        },
        {
            "$group": {
                "_id": None,
                "total": {"$sum": "$profitNum"}
            }
        }
    ]

    result = list(plays_col.aggregate(pipeline))
    return result[0]["total"] if result else 0.0


# Recent dates that actually have plays, for /recap autocomplete
# (blocking - run in a thread)
def recent_dates(prefix: str = "") -> list[tuple[str, int]]:
    match = {"$and": [SOURCE_FILTER, {"title": {"$ne": "Untitled"}}]}

    if prefix:
        match["$and"].append({"date": {"$regex": f"^{re.escape(prefix)}"}})

    pipeline = [
        {"$match": match},
        {"$group": {"_id": "$date", "plays": {"$sum": 1}}},
        {"$sort": {"_id": -1}},
        {"$limit": 25},
    ]

    return [(doc["_id"], doc["plays"]) for doc in plays_col.aggregate(pipeline)]


# Gathers everything a recap needs from MongoDB (blocking - run in a thread)
def gather_recap_data(target_date: str) -> dict | None:
    day_query = {
        "$and": [
            SOURCE_FILTER,
            {"date": target_date},
            {"title": {"$ne": "Untitled"}},
        ]
    }

    day_plays = list(plays_col.find(
        day_query,
        sort=[("timestamp", 1), ("sheet_row", 1)]
    ))

    if not day_plays:
        return None

    month_str = target_date[:7]
    year_str = target_date[:4]

    return {
        "target_date": target_date,
        "day_plays": day_plays,
        "month_total": sum_profit({"date": {"$regex": f"^{month_str}"}}),
        "yearly_total": sum_profit({"date": {"$regex": f"^{year_str}"}}),
        "alltime_total": sum_profit({}),
    }


# Builds the recap embed from gathered data
def build_recap_embed(data: dict) -> discord.Embed:
    target_date = data["target_date"]

    total_profit = 0.0
    wins = losses = pushes = pending = 0
    rows = []

    # Loop through each play
    for p in data["day_plays"]:
        title = p.get("title", "Untitled")
        result = clean_result(p.get("result"))
        profit = parse_units(p.get("profit")) or 0.0

        # If invalid result, it is pending
        if result not in VALID_RESULTS:
            pending += 1
            rows.append(f"⏳ {title}")
            continue

        # Adds the play's profit to daily total
        total_profit += profit
        profit_str = fmt(profit)

        if result == "W":
            wins += 1
            rows.append(f"✅ {title} {profit_str}")
        elif result == "L":
            losses += 1
            rows.append(f"❌ {title} {profit_str}")
        else:
            pushes += 1
            rows.append(f"➖ {title} {profit_str}")

    # Date Formatting
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    pretty_date = f"{dt.month}/{dt.day}/{dt.year}"

    # Combine plays, staying under Discord's description limit
    plays_text = "\n".join(rows) if rows else "No settled plays."

    if len(plays_text) > DESCRIPTION_LIMIT:
        budget = DESCRIPTION_LIMIT - 40
        kept = []
        used = 0

        for line in rows:
            # Hard-trim a single monster line so it can't swallow the budget
            if len(line) > budget:
                line = line[: budget - 1] + "…"

            if used + len(line) + 1 > budget:
                break

            kept.append(line)
            used += len(line) + 1

        plays_text = "\n".join(kept) + f"\n… and {len(rows) - len(kept)} more plays"

    # Daily record, e.g. 3-2 or 3-2-1 when there are pushes
    record = f"{wins}-{losses}"
    if pushes:
        record += f"-{pushes}"

    day_line = f"**{pretty_date}: {fmt(total_profit)} ({record})**"
    if pending:
        day_line += f"\n⏳ {pending} pending"

    # Builds the total section
    totals_text = (
        f"{day_line}\n"
        f"**THIS MONTH: {fmt(data['month_total'])}**\n"
        f"**YEARLY: {fmt(data['yearly_total'])}**\n"
        f"**ALL-TIME: {fmt(data['alltime_total'])}**"
    )

    # Green on a winning day, red on a losing day, purple otherwise
    settled = wins + losses + pushes
    if settled and total_profit > 0:
        color = GREEN
    elif settled and total_profit < 0:
        color = RED
    else:
        color = PURPLE

    embed = discord.Embed(
        title=f"RunaansLocks {pretty_date} Recap",
        description=plays_text,
        color=color,
        timestamp=datetime.now(EASTERN),
    )

    embed.add_field(name="​", value=totals_text, inline=False)
    embed.set_footer(text="Runaans Locks")

    return embed


## Creates the Discord Cog
class RecapCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        if AUTO_RECAP_ENABLED:
            self.auto_recap.start()
            log.info(
                "Auto-recap enabled, posting daily at %s Eastern",
                AUTO_RECAP_TIME.strftime("%H:%M"),
            )

    async def cog_unload(self):
        self.auto_recap.cancel()

    async def _get_recap_channel(self):
        channel = self.bot.get_channel(RECAP_CHANNEL)

        if channel is None:
            channel = await self.bot.fetch_channel(RECAP_CHANNEL)

        return channel

    # Suggests dates that actually have plays, newest first
    async def date_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        try:
            dates = await asyncio.to_thread(recent_dates, current.strip())
        except Exception:
            log.exception("Date autocomplete failed")
            return []

        return [
            app_commands.Choice(
                name=f"{d} ({n} play{'s' if n != 1 else ''})", value=d
            )
            for d, n in dates
        ]

    @app_commands.command(name="recap", description="Post the Recaps")
    @owner_only()
    @app_commands.describe(
        date="Date of the recap (e.g. 2026-08-19 or 8/19/2026), leave blank for today",
        preview="Show the recap only to you instead of posting it",
    )
    @app_commands.autocomplete(date=date_autocomplete)
    async def recap(
        self,
        interaction: discord.Interaction,
        date: str | None = None,
        preview: bool = False,
    ):
        await interaction.response.defer(ephemeral=True)

        if date:
            target_date = normalize_date(date)
            if not target_date:
                await interaction.followup.send(
                    f"Couldn't understand the date **{date}**. "
                    "Use a format like `2026-08-19` or `8/19/2026`.",
                    ephemeral=True,
                )
                return
        else:
            target_date = datetime.now(EASTERN).strftime("%Y-%m-%d")

        # Pull the latest sheet data first so the recap is always current
        sync_note = ""
        try:
            await asyncio.to_thread(sync_sheet)
        except Exception:
            log.exception("Sheet sync failed during /recap")
            sync_note = "\n⚠️ Sheet sync failed - recap uses last-synced data."

        data = await asyncio.to_thread(gather_recap_data, target_date)

        # If no plays are found, sends a message
        if data is None:
            await interaction.followup.send(
                f"No sheet plays found for **{target_date}**.{sync_note}",
                ephemeral=True,
            )
            return

        embed = build_recap_embed(data)

        if preview:
            await interaction.followup.send(
                content=f"Preview only - not posted.{sync_note}",
                embed=embed,
                ephemeral=True,
            )
            return

        try:
            channel = await self._get_recap_channel()
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.exception("Failed to post recap to channel %s", RECAP_CHANNEL)
            await interaction.followup.send(
                "Couldn't post to the recap channel - check the bot's "
                f"permissions and RECAP_CHANNEL.{sync_note}",
                ephemeral=True,
            )
            return

        await interaction.followup.send(f"Posted Recap{sync_note}", ephemeral=True)

    # Posts the recap automatically every day (if enabled in .env)
    @tasks.loop(time=AUTO_RECAP_TIME)
    async def auto_recap(self):
        target_date = datetime.now(EASTERN).strftime("%Y-%m-%d")

        try:
            await asyncio.to_thread(sync_sheet)
        except Exception:
            log.exception("Sheet sync failed before auto-recap")

        try:
            data = await asyncio.to_thread(gather_recap_data, target_date)

            if data is None:
                log.info("Auto-recap: no plays for %s, skipping", target_date)
                return

            channel = await self._get_recap_channel()
            await channel.send(embed=build_recap_embed(data))
            log.info("Auto-recap posted for %s", target_date)
        except Exception:
            log.exception("Auto-recap failed for %s", target_date)

    @auto_recap.before_loop
    async def before_auto_recap(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(RecapCog(bot), guild=GUILD_ID)
