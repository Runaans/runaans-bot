import asyncio
import logging
import os
import sys

# Make sure project root is available for imports
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import discord
from discord.ext import commands
from discord import app_commands

from config import TOKEN, GUILD_ID
from database import ensure_indexes
from helpers import OwnerOnly

discord.utils.setup_logging(level=logging.INFO)
log = logging.getLogger("runaans")

intents = discord.Intents.default()
intents.guilds = True

COGS = ["cogs.recap", "cogs.sync", "cogs.stats"]

class Client(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)


    async def setup_hook(self):
        await asyncio.to_thread(ensure_indexes)

        # Load all cogs
        for cog in COGS:
            await self.load_extension(cog)
            log.info("Loaded %s", cog)

        await self.tree.sync(guild=GUILD_ID)
        log.info("Synced commands to guild %s", GUILD_ID.id)

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)

        try:
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching, name="the board 📈"
                )
            )
        except discord.HTTPException:
            log.warning("Could not set presence", exc_info=True)

client = Client()

# Handle command errors
@client.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, OwnerOnly):
        message = "You don't have permission to use this command."
    else:
        log.error("Command error in /%s", interaction.command.name if interaction.command else "?", exc_info=error)
        message = "Something went wrong running that command."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


client.run(TOKEN, log_handler=None)
