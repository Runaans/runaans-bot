# imports
import io
import logging
import os
from datetime import datetime
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands

from config import GUILD_ID, BRAND, BRAND_URL, PURPLE, EASTERN
from helpers import owner_only, parse_units

log = logging.getLogger("runaans.bets")

# Discord merges up to 4 embed images into one gallery block, as long as the
# embeds share the same URL. That's how multiple slips stay in one post.
MAX_SLIPS = 4

BOOKS = [
    "DraftKings",
    "FanDuel",
    "bet365",
    "BetMGM",
    "theScore Bet",
    "BetRivers",
    "Caesars",
    "Other",
]

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def format_units(raw: str) -> str:
    """'1u' -> '1U', '0.15, 0.1u' -> '0.15U' and '0.1U' on separate lines.

    Anything that isn't a plain number is kept exactly as typed.
    """
    lines = []

    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue

        value = parse_units(part)
        lines.append(f"{value:g}U" if value is not None else part)

    return "\n".join(lines)


def is_valid_link(link: str | None) -> bool:
    # Discord rejects link buttons whose URL isn't http(s) or is over 512 chars
    return bool(link) and link.startswith(("http://", "https://")) and len(link) <= 512


def is_image(attachment: discord.Attachment) -> bool:
    if attachment.content_type:
        return attachment.content_type.startswith("image/")

    return attachment.filename.lower().endswith(IMAGE_EXTENSIONS)


def slip_filename(index: int, original: str) -> str:
    ext = os.path.splitext(original)[1].lower()
    return f"slip{index}{ext if ext in IMAGE_EXTENSIONS else '.png'}"


def build_bet_embeds(
    *,
    units: str,
    headline: str | None = None,
    note: str | None = None,
    odds: str | None = None,
    book: str | None = None,
    image_names: list[str] = (),
    author: str | None = None,
    author_icon: str | None = None,
) -> list[discord.Embed]:
    """One bet block: headline quote, write-up, units/odds/book, then slips.

    Every embed gets the same URL so Discord renders all slip images inside
    the first block as a gallery instead of stacking separate cards.
    """
    description = []
    if headline:
        description.append(f"> {headline}")
    if note:
        description.append(note)

    embed = discord.Embed(
        title=BRAND,
        url=BRAND_URL,
        description="\n\n".join(description) or None,
        color=PURPLE,
        timestamp=datetime.now(EASTERN),
    )

    embed.add_field(name="Units", value=format_units(units) or units, inline=True)
    if odds:
        embed.add_field(name="Odds", value=odds, inline=True)
    if book:
        embed.add_field(name="Book", value=book, inline=True)

    if author:
        embed.set_footer(text=author, icon_url=author_icon)

    embeds = [embed]

    for i, name in enumerate(image_names):
        if i == 0:
            embed.set_image(url=f"attachment://{name}")
            continue

        extra = discord.Embed(url=BRAND_URL, color=PURPLE)
        extra.set_image(url=f"attachment://{name}")
        embeds.append(extra)

    return embeds


def build_link_view(link: str | None, book: str | None) -> discord.ui.View | None:
    if not is_valid_link(link):
        return None

    label = f"Bet this at {book}" if book and book != "Other" else "View betslip"

    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label=label, url=link))
    return view


class BetsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="bet",
        description="Post a play as one clean block with its bet slip(s)",
    )
    @owner_only()
    @app_commands.describe(
        slip="Screenshot of the bet slip",
        units="Stake, e.g. 1u - use commas for several: 0.15u, 0.1u",
        headline="Short label shown as a quote, e.g. FD 50% Boost Stimmy",
        note="Your write-up for the play",
        odds="Odds, e.g. -115 or +1021",
        book="Sportsbook the bet is at",
        ping="Role to notify, e.g. @Premium",
        link="Betslip link for the button (e.g. from Playbook or the book's share link)",
        slip2="Extra screenshot",
        slip3="Extra screenshot",
        slip4="Extra screenshot",
        preview="Show it only to you instead of posting",
    )
    @app_commands.choices(book=[app_commands.Choice(name=b, value=b) for b in BOOKS])
    async def bet(
        self,
        interaction: discord.Interaction,
        slip: discord.Attachment,
        units: app_commands.Range[str, 1, 60],
        headline: Optional[app_commands.Range[str, 1, 200]] = None,
        note: Optional[app_commands.Range[str, 1, 1500]] = None,
        odds: Optional[app_commands.Range[str, 1, 20]] = None,
        book: Optional[app_commands.Choice[str]] = None,
        ping: Optional[discord.Role] = None,
        link: Optional[app_commands.Range[str, 1, 512]] = None,
        slip2: Optional[discord.Attachment] = None,
        slip3: Optional[discord.Attachment] = None,
        slip4: Optional[discord.Attachment] = None,
        preview: bool = False,
    ):
        await interaction.response.defer(ephemeral=True)

        slips = [a for a in (slip, slip2, slip3, slip4) if a is not None]

        not_images = [a.filename for a in slips if not is_image(a)]
        if not_images:
            await interaction.followup.send(
                f"These aren't images: {', '.join(not_images)}", ephemeral=True
            )
            return

        if link and not is_valid_link(link):
            await interaction.followup.send(
                "The link needs to start with http:// or https://", ephemeral=True
            )
            return

        book_name = book.value if book else None

        # Re-upload the screenshots with the post. Linking to the slash-command
        # attachment URLs would break once Discord expires them.
        files = []
        names = []
        for i, attachment in enumerate(slips, start=1):
            name = slip_filename(i, attachment.filename)
            data = await attachment.read()
            files.append(discord.File(io.BytesIO(data), filename=name))
            names.append(name)

        embeds = build_bet_embeds(
            units=units,
            headline=headline,
            note=note,
            odds=odds,
            book=book_name,
            image_names=names,
            author=interaction.user.display_name,
            author_icon=interaction.user.display_avatar.url,
        )
        view = build_link_view(link, book_name)

        if preview:
            kwargs = {"view": view} if view else {}
            await interaction.followup.send(
                content="Preview only - not posted."
                + (f" Would ping {ping.mention}." if ping else ""),
                embeds=embeds,
                files=files,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
                **kwargs,
            )
            return

        # The role mention has to be in the message text to actually notify
        kwargs = {"view": view} if view else {}
        try:
            message = await interaction.channel.send(
                content=ping.mention if ping else None,
                embeds=embeds,
                files=files,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, users=False, roles=[ping] if ping else False
                ),
                **kwargs,
            )
        except discord.HTTPException:
            log.exception("Failed to post bet in channel %s", interaction.channel_id)
            await interaction.followup.send(
                "Couldn't post here - the bot needs Send Messages, Embed Links "
                "and Attach Files in this channel.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Posted - [jump]({message.jump_url})", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(BetsCog(bot), guild=GUILD_ID)
