# imports
import asyncio
import logging
from datetime import datetime, timedelta

import discord
from discord.ext import commands
from discord import app_commands

from config import GUILD_ID, RECAP_CHANNEL, PURPLE, GREEN, RED, EASTERN
from database import plays_col
from helpers import owner_only

log = logging.getLogger("runaans.stats")

SOURCE_FILTER = {"source": "sheet"}
VALID_RESULTS = ("W", "L", "P")

# How many pending plays to list before truncating
PENDING_LIMIT = 25


def fmt(value):
    value = value + 0.0
    return f"{value:+.2f}u"


def period_query(period: str, today: str) -> tuple[dict, str]:
    """Returns (mongo date filter, human label) for a period choice."""
    if period == "today":
        return {"date": today}, "Today"

    if period == "week":
        start = (
            datetime.strptime(today, "%Y-%m-%d") - timedelta(days=6)
        ).strftime("%Y-%m-%d")
        return {"date": {"$gte": start, "$lte": today}}, "Last 7 Days"

    if period == "month":
        return {"date": {"$regex": f"^{today[:7]}"}}, "This Month"

    if period == "year":
        return {"date": {"$regex": f"^{today[:4]}"}}, "This Year"

    return {}, "All-Time"


# Blocking - run in a thread
def gather_stats(date_filter: dict, capper: str | None) -> list[dict]:
    """Per-capper record, units and ROI for the given period."""
    match = {"$and": [SOURCE_FILTER, {"title": {"$ne": "Untitled"}}, date_filter]}

    if capper:
        match["$and"].append({"capper": {"$regex": f"^{capper}$", "$options": "i"}})

    pipeline = [
        {"$match": match},
        {
            "$addFields": {
                "resultUpper": {"$toUpper": {"$ifNull": ["$result", ""]}},
                "profitNum": {
                    "$convert": {"input": "$profit", "to": "double", "onError": 0, "onNull": 0}
                },
                "unitsNum": {
                    "$convert": {"input": "$units", "to": "double", "onError": 0, "onNull": 0}
                },
            }
        },
        {"$match": {"resultUpper": {"$in": list(VALID_RESULTS)}}},
        {
            "$group": {
                "_id": {"$ifNull": ["$capper", ""]},
                "wins": {"$sum": {"$cond": [{"$eq": ["$resultUpper", "W"]}, 1, 0]}},
                "losses": {"$sum": {"$cond": [{"$eq": ["$resultUpper", "L"]}, 1, 0]}},
                "pushes": {"$sum": {"$cond": [{"$eq": ["$resultUpper", "P"]}, 1, 0]}},
                "profit": {"$sum": "$profitNum"},
                "staked": {"$sum": "$unitsNum"},
            }
        },
        {"$sort": {"profit": -1}},
    ]

    rows = []
    for doc in plays_col.aggregate(pipeline):
        staked = doc.get("staked") or 0.0
        rows.append(
            {
                "capper": doc["_id"] or "Unknown",
                "wins": doc["wins"],
                "losses": doc["losses"],
                "pushes": doc["pushes"],
                "profit": doc["profit"],
                "staked": staked,
                "roi": (doc["profit"] / staked * 100) if staked else None,
            }
        )

    return rows


# Blocking - run in a thread
def gather_pending() -> list[dict]:
    query = {
        "$and": [
            SOURCE_FILTER,
            {"title": {"$ne": "Untitled"}},
            {
                "$or": [
                    {"result": None},
                    {"result": ""},
                    {"result": {"$exists": False}},
                    {"result": {"$nin": list(VALID_RESULTS)}},
                ]
            },
        ]
    }

    return list(
        plays_col.find(query, sort=[("date", -1), ("sheet_row", 1)]).limit(
            PENDING_LIMIT + 1
        )
    )


def build_stats_embed(rows: list[dict], label: str, capper: str | None) -> discord.Embed:
    total_profit = sum(r["profit"] for r in rows)
    total_staked = sum(r["staked"] for r in rows)
    wins = sum(r["wins"] for r in rows)
    losses = sum(r["losses"] for r in rows)
    pushes = sum(r["pushes"] for r in rows)

    record = f"{wins}-{losses}" + (f"-{pushes}" if pushes else "")
    roi = (total_profit / total_staked * 100) if total_staked else None
    roi_text = f"{roi:+.1f}%" if roi is not None else "n/a"

    if total_profit > 0:
        color = GREEN
    elif total_profit < 0:
        color = RED
    else:
        color = PURPLE

    title = f"{capper} Stats - {label}" if capper else f"RunaansLocks Stats - {label}"
    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(EASTERN))

    if not rows:
        embed.description = "No settled plays in this period."
        embed.set_footer(text="Runaans Locks")
        return embed

    embed.description = (
        f"**Record:** {record}\n"
        f"**Units:** {fmt(total_profit)}\n"
        f"**ROI:** {roi_text} on {total_staked:.2f}u staked"
    )

    # Per-capper leaderboard, only when looking at everyone
    if not capper and len(rows) > 1:
        lines = []
        for i, r in enumerate(rows[:10], start=1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"`{i}.`")
            r_record = f"{r['wins']}-{r['losses']}" + (
                f"-{r['pushes']}" if r["pushes"] else ""
            )
            r_roi = f"{r['roi']:+.1f}%" if r["roi"] is not None else "n/a"
            lines.append(f"{medal} **{r['capper']}** {fmt(r['profit'])} ({r_record}, {r_roi})")

        embed.add_field(name="Leaderboard", value="\n".join(lines), inline=False)

    embed.set_footer(text="Runaans Locks")
    return embed


def build_pending_embed(plays: list[dict]) -> discord.Embed:
    embed = discord.Embed(
        title="Pending Plays", color=PURPLE, timestamp=datetime.now(EASTERN)
    )

    if not plays:
        embed.description = "Nothing pending - every play is settled. 🎉"
        embed.set_footer(text="Runaans Locks")
        return embed

    truncated = len(plays) > PENDING_LIMIT
    shown = plays[:PENDING_LIMIT]

    lines = []
    for p in shown:
        bits = [f"⏳ **{p.get('date', '?')}** {p.get('title', 'Untitled')}"]

        details = []
        if p.get("odds"):
            details.append(str(p["odds"]))
        if p.get("units"):
            details.append(f"{float(p['units']):.2f}u")
        if p.get("capper"):
            details.append(str(p["capper"]))
        if details:
            bits.append(f"({', '.join(details)})")

        lines.append(" ".join(bits))

    if truncated:
        lines.append(f"… and more (showing first {PENDING_LIMIT})")

    embed.description = "\n".join(lines)[:4096]
    embed.set_footer(text=f"{len(shown)}{'+' if truncated else ''} pending")
    return embed


class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="stats",
        description="Record, units and ROI for a period",
    )
    @owner_only()
    @app_commands.describe(
        period="Time range to summarize",
        capper="Only show one capper's numbers",
        post="Post publicly in the recap channel instead of showing only you",
    )
    @app_commands.choices(
        period=[
            app_commands.Choice(name="Today", value="today"),
            app_commands.Choice(name="Last 7 days", value="week"),
            app_commands.Choice(name="This month", value="month"),
            app_commands.Choice(name="This year", value="year"),
            app_commands.Choice(name="All-time", value="all"),
        ]
    )
    async def stats(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str] = None,
        capper: str | None = None,
        post: bool = False,
    ):
        await interaction.response.defer(ephemeral=not post)

        period_value = period.value if period else "month"
        today = datetime.now(EASTERN).strftime("%Y-%m-%d")
        date_filter, label = period_query(period_value, today)

        rows = await asyncio.to_thread(gather_stats, date_filter, capper)
        embed = build_stats_embed(rows, label, capper)

        if not post:
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        try:
            channel = self.bot.get_channel(RECAP_CHANNEL) or await self.bot.fetch_channel(
                RECAP_CHANNEL
            )
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.exception("Failed to post stats to channel %s", RECAP_CHANNEL)
            await interaction.followup.send(
                "Couldn't post to the recap channel - check the bot's permissions."
            )
            return

        await interaction.followup.send("Posted stats.")

    @app_commands.command(
        name="pending",
        description="Show plays that haven't been graded yet",
    )
    @owner_only()
    async def pending(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        plays = await asyncio.to_thread(gather_pending)
        await interaction.followup.send(embed=build_pending_embed(plays), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(StatsCog(bot), guild=GUILD_ID)
