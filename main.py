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
from helpers import OwnerOnly

intents = discord.Intents.default()
intents.guilds = True

COGS = ["cogs.recap",]

class Client(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)


    async def setup_hook(self):
        # Load all cogs
        for cog in COGS:
            await self.load_extension(cog)
            print(f"Loaded {cog}")

        await self.tree.sync(guild=GUILD_ID)
        print(f"Synced commands to guild {GUILD_ID.id}")

client = Client()

# Handle owner-only errors 
@client.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, OwnerOnly):
        try:
            await interaction.response.send_message("\u200b", ephemeral=True)
        except Exception:
            pass
    else:
        print(f"Command error: {error}")


client.run(TOKEN)