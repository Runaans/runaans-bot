# imports
import asyncio
import logging

import discord
from discord.ext import commands, tasks
from discord import app_commands

from config import GUILD_ID, AUTO_SYNC_MINUTES
from helpers import owner_only
from sheets_sync import sync_sheet

log = logging.getLogger("runaans.sync_cog")


class SyncCog(commands.Cog):
    """Keeps MongoDB in sync with the Google Sheet."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

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
            log.exception("Background sheet sync failed")

    @auto_sync.before_loop
    async def before_auto_sync(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(SyncCog(bot), guild=GUILD_ID)
