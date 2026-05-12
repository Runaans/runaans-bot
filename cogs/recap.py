# imports
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime

from config import GUILD_ID, RECAP_CHANNEL, PURPLE, EASTERN
from database import plays_col
from helpers import owner_only


VALID_RESULTS = {"W", "L", "P"}

# Makes sure recaps only come from the google sheet
SOURCE_FILTER = {"source": "sheet"}

def clean_result(value):
    return (value or "").upper().strip()

# Converts value into a number
def as_float(value):
    try:
        if value is None or value == "":
            return 0.0

        return float(
            str(value)
            .replace(",", "")
            .replace("u", "")
            .strip()
        )
    except (ValueError, TypeError):
        return 0.0

## Formats unit totals
def fmt(value):
    return f"+{value:.2f}u" if value >= 0 else f"{value:.2f}u"

## Creates the Discord Cog
class RecapCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="recap", description="Post the Recaps")
    @owner_only()
    @app_commands.describe(
        date="YYYY-MM-DD format, leave blank for today"
    )
    async def recap(
        self,
        interaction: discord.Interaction,
        date: str = None,
    ):
        await interaction.response.defer(ephemeral=True)

        target_date = date or datetime.now(EASTERN).strftime("%Y-%m-%d")

        # MongoDB Query filter for the day
        day_query = {
            "$and": [
                SOURCE_FILTER,
                {"date": target_date},
                {"title": {"$ne": "Untitled"}},
            ]
        }

        # Searches MongoDB
        day_plays = list(plays_col.find(
            day_query,
            sort=[("timestamp", 1), ("sheet_row", 1)]
        ))

        # If no plays are found, sends a message
        if not day_plays:
            await interaction.followup.send(
                f"No sheet plays found for **{target_date}**.",
                ephemeral=True
            )
            return

        # Recap Counters
        total_profit = 0.0
        rows = []

        # Loop through each play 
        for p in day_plays:
            title = p.get("title", "Untitled")
            result = clean_result(p.get("result"))
            profit = as_float(p.get("profit"))

            # If invalid result, it is pending
            if result not in VALID_RESULTS:
                rows.append(f"⏳ {title}")
                continue
            
            # Adds the play's profit to daily total
            total_profit += profit
            profit_str = fmt(profit)

            if result == "W":
                rows.append(f"✅ {title} {profit_str}")
            elif result == "L":
                rows.append(f"❌ {title} {profit_str}")
            else:
                rows.append(f"➖ {title} {profit_str}")

        # Date Formatting
        try:
            dt = datetime.strptime(target_date, "%Y-%m-%d")
            pretty_date = f"{dt.month}/{dt.day}/{dt.year}"
            month_str = target_date[:7]
            year_str = target_date[:4]
        # If Date format is invalid
        except ValueError:
            dt = datetime.now(EASTERN)
            pretty_date = target_date
            month_str = dt.strftime("%Y-%m")
            year_str = dt.strftime("%Y")

        # Calculates Monthly, Yearly, All-time Total
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
                        "resultUpper": {"$in": ["W", "L", "P"]}
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

        month_total = sum_profit({"date": {"$regex": f"^{month_str}"}})
        yearly_total = sum_profit({"date": {"$regex": f"^{year_str}"}})
        alltime_total = sum_profit({})

        # Combines all plays into one block
        plays_text = "\n".join(rows) if rows else "No settled plays."

        # Builds the total section
        totals_text = (
            f"**{pretty_date}: {fmt(total_profit)}**\n"
            f"**THIS MONTH: {fmt(month_total)}**\n"
            f"**YEARLY: {fmt(yearly_total)}**\n"
            f"**ALL-TIME: {fmt(alltime_total)}**"
        )

        embed = discord.Embed(
            title=f"RunaansLocks {pretty_date} Recap",
            color=PURPLE,
            timestamp=datetime.now(EASTERN),
        )

        embed.add_field(name="\u200b", value=plays_text, inline=False)
        embed.add_field(name="\u200b", value=totals_text, inline=False)
        embed.set_footer(text="Runaans Locks")

        channel = self.bot.get_channel(RECAP_CHANNEL)

        if channel is None:
            channel = await self.bot.fetch_channel(RECAP_CHANNEL)

        await channel.send(embed=embed)
        await interaction.followup.send("Posted Recap", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RecapCog(bot), guild=GUILD_ID)