# imports
import asyncio
import logging

import discord
from discord.ext import commands, tasks
from discord import app_commands

from config import GUILD_ID, OWNER_ID, AUTO_SYNC_MINUTES
from helpers import owner_only
import sheets_sync
from sheets_sync import sync_sheet

log = logging.getLogger("runaans.sync_cog")

# DM the owner after this many consecutive background sync failures
FAILURE_ALERT_THRESHOLD = 3

# Then remind every N further failures, so a permanently broken sync doesn't
# go quiet after a single DM
REALERT_EVERY = 12


class SyncCog(commands.Cog):
    """Keeps MongoDB in sync with the Google Sheet."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
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

            # Alert at the threshold, then again every REALERT_EVERY failures
            over = self.consecutive_failures - FAILURE_ALERT_THRESHOLD
            if over >= 0 and over % REALERT_EVERY == 0:
                await self._alert_owner()
            return

        if self.alerted:
            await self._notify_recovered()

        self.consecutive_failures = 0
        self.alerted = False

    # Both DM helpers are best-effort: a notification must never take the
    # loop down with it, so they swallow everything.
    async def _alert_owner(self):
        try:
            owner = await self.bot.fetch_user(OWNER_ID)
            await owner.send(
                f"⚠️ Sheet sync has failed {self.consecutive_failures} times in a "
                f"row. Last error: `{str(sheets_sync.last_sync['error'])[:500]}`\n"
                "Recaps may be stale until this is fixed."
            )
            self.alerted = True
        except Exception:
            log.exception("Could not DM owner about sync failures")

    async def _notify_recovered(self):
        try:
            owner = await self.bot.fetch_user(OWNER_ID)
            await owner.send("✅ Sheet sync has recovered and is working again.")
        except Exception:
            log.exception("Could not DM owner about sync recovery")

    @auto_sync.before_loop
    async def before_auto_sync(self):
        await self.bot.wait_until_ready()

    # A crashed loop would otherwise stay dead while /status still showed a
    # stale-but-successful last sync
    @auto_sync.error
    async def auto_sync_error(self, error: BaseException):
        log.error("Auto-sync loop crashed, restarting it", exc_info=error)
        self.auto_sync.restart()


async def setup(bot: commands.Bot):
    await bot.add_cog(SyncCog(bot), guild=GUILD_ID)
