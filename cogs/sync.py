# imports
import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from discord import app_commands

from config import (
    GUILD_ID,
    OWNER_ID,
    PURPLE,
    AUTO_SYNC_MINUTES,
    AUTO_RECAP_ENABLED,
    AUTO_RECAP_TIME,
)
from database import plays_col
from helpers import owner_only, SHEET_FILTER, PENDING_FILTER
import sheets_sync
from sheets_sync import sync_sheet

log = logging.getLogger("runaans.sync_cog")

# DM the owner after this many consecutive background sync failures
FAILURE_ALERT_THRESHOLD = 3


# Blocking - run in a thread
def gather_status_counts() -> dict:
    total = plays_col.count_documents(SHEET_FILTER)
    pending = plays_col.count_documents({"$and": [SHEET_FILTER, PENDING_FILTER]})
    return {"total": total, "pending": pending}


class SyncCog(commands.Cog):
    """Keeps MongoDB in sync with the Google Sheet."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.started_at = datetime.now(timezone.utc)
        self.consecutive_failures = 0
        self.alerted = False

    async def cog_load(self):
        if AUTO_SYNC_MINUTES > 0:
            self.auto_sync.change_interval(minutes=AUTO_SYNC_MINUTES)
            self.auto_sync.start()
            log.info("Auto-sync enabled every %s minutes", AUTO_SYNC_MINUTES)

    async def cog_unload(self):
        self.auto_sync.cancel()

    @app_commands.command(
        name="sync",
        description="Pull the latest plays from the Google Sheet",
    )
    @owner_only()
    async def sync(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            stats = await asyncio.to_thread(sync_sheet)
        except Exception:
            log.exception("Manual /sync failed")
            await interaction.followup.send(
                "Sync failed - check the bot logs.", ephemeral=True
            )
            return

        await interaction.followup.send(
            f"Sheet synced: {stats.summary()}.", ephemeral=True
        )

    @app_commands.command(
        name="status",
        description="Bot health: last sync, uptime, play counts",
    )
    @owner_only()
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        counts = await asyncio.to_thread(gather_status_counts)
        last = sheets_sync.last_sync

        if last["time"] is None:
            sync_line = "No sync has run yet."
        else:
            stamp = f"<t:{int(last['time'].timestamp())}:R>"
            if last["error"]:
                sync_line = f"❌ Failed {stamp}: `{last['error'][:200]}`"
            else:
                sync_line = f"✅ {stamp} - {last['stats'].summary()}"

        auto_sync_line = (
            f"every {AUTO_SYNC_MINUTES} min" if AUTO_SYNC_MINUTES > 0 else "off"
        )
        auto_recap_line = (
            f"daily at {AUTO_RECAP_TIME.strftime('%H:%M')} ET"
            if AUTO_RECAP_ENABLED
            else "off"
        )

        embed = discord.Embed(title="Bot Status", color=PURPLE)
        embed.add_field(name="Last sync", value=sync_line, inline=False)
        embed.add_field(name="Auto-sync", value=auto_sync_line, inline=True)
        embed.add_field(name="Auto-recap", value=auto_recap_line, inline=True)
        embed.add_field(
            name="Plays",
            value=f"{counts['total']} total, {counts['pending']} pending",
            inline=True,
        )
        embed.add_field(
            name="Online since",
            value=f"<t:{int(self.started_at.timestamp())}:R>",
            inline=True,
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # Background sheet sync so recap data is always fresh
    @tasks.loop(minutes=15)
    async def auto_sync(self):
        try:
            await asyncio.to_thread(sync_sheet)
        except Exception:
            self.consecutive_failures += 1
            log.exception(
                "Background sheet sync failed (%s in a row)",
                self.consecutive_failures,
            )
            if self.consecutive_failures >= FAILURE_ALERT_THRESHOLD and not self.alerted:
                await self._alert_owner()
            return

        if self.alerted:
            await self._notify_recovered()

        self.consecutive_failures = 0
        self.alerted = False

    async def _alert_owner(self):
        try:
            owner = await self.bot.fetch_user(OWNER_ID)
            await owner.send(
                f"⚠️ Sheet sync has failed {self.consecutive_failures} times in a "
                f"row. Last error: `{str(sheets_sync.last_sync['error'])[:500]}`\n"
                "Recaps may be stale until this is fixed."
            )
            self.alerted = True
        except discord.HTTPException:
            log.exception("Could not DM owner about sync failures")

    async def _notify_recovered(self):
        try:
            owner = await self.bot.fetch_user(OWNER_ID)
            await owner.send("✅ Sheet sync has recovered and is working again.")
        except discord.HTTPException:
            log.exception("Could not DM owner about sync recovery")

    @auto_sync.before_loop
    async def before_auto_sync(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(SyncCog(bot), guild=GUILD_ID)
