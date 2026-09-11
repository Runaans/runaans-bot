# imports
import io
import logging
import os
import re
from datetime import datetime

import discord
from discord.ext import commands

from config import OWNER_ID, BRAND, BRAND_URL, PURPLE, EASTERN, PING_ROLE

log = logging.getLogger("runaans.bets")

# Discord allows 10 embeds per message; the first 4 images render as one gallery
MAX_SLIPS = 10

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# "1u", "0.25 u", ".5U", "2 units"
UNITS_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?|\.\d+)\s*u(?:nits?)?\b", re.IGNORECASE)
MENTION_RE = re.compile(r"<@!?\d+>")

# A short first line followed by more text is shown as the quoted headline
HEADLINE_MAX = 60


def parse_post(text: str) -> dict:
    """Pull units out of a normal message and split it into headline + note.

        @RunaansLocks 0.25u FD 50% Boost Stimmy
        back from vacation, time to lock in

    -> units ["0.25U"], headline "FD 50% Boost Stimmy", note "back from ..."
    """
    text = MENTION_RE.sub("", text or "")

    units = [f"{float(m.group(1)):g}U" for m in UNITS_RE.finditer(text)]
    text = UNITS_RE.sub("", text)

    lines = [re.sub(r"[ \t]+", " ", line).strip(" \t-–:|") for line in text.splitlines()]
    lines = [line for line in lines if line]

    headline = note = None
    if len(lines) >= 2 and len(lines[0]) <= HEADLINE_MAX:
        headline, note = lines[0], "\n".join(lines[1:])
    elif lines:
        note = "\n".join(lines)

    return {"units": units, "headline": headline, "note": note}


def is_image(attachment: discord.Attachment) -> bool:
    if attachment.content_type:
        return attachment.content_type.startswith("image/")

    return attachment.filename.lower().endswith(IMAGE_EXTENSIONS)


def slip_filename(index: int, original: str) -> str:
    ext = os.path.splitext(original)[1].lower()
    return f"slip{index}{ext if ext in IMAGE_EXTENSIONS else '.png'}"


def build_bet_embeds(
    *,
    units: list[str],
    headline: str | None = None,
    note: str | None = None,
    image_names: list[str] = (),
    author: str | None = None,
    author_icon: str | None = None,
) -> list[discord.Embed]:
    """One bet block: headline quote, write-up, units, then the slips.

    Every embed gets the same URL so Discord renders all slip images inside
    the first block as a gallery instead of stacking separate cards.
    """
    description = []
    if headline:
        description.append(f"> {headline}")
    if note:
        description.append(note)

    # No title on purpose: the URL is needed for gallery grouping, and a
    # title would render as a blue link. The brand goes in the author line,
    # which shows as plain white text.
    embed = discord.Embed(
        url=BRAND_URL,
        description="\n\n".join(description)[:4096] or None,
        color=PURPLE,
        timestamp=datetime.now(EASTERN),
    )
    embed.set_author(name=BRAND)

    if units:
        embed.add_field(name="Units", value="\n".join(units), inline=True)

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


class BetsCog(commands.Cog):
    """Turns '@RunaansLocks 1u <note> + screenshot' into a clean bet post."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.warned_missing_role = False

    def _ping_role(self, guild: discord.Guild) -> discord.Role | None:
        return discord.utils.find(
            lambda r: r.name.lower() == PING_ROLE.lower(), guild.roles
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Only the owner's messages, in a server, that mention the bot.
        # (Mentioning the bot is also what makes message.content readable
        # without the privileged message-content intent.)
        if message.author.bot or message.guild is None:
            return
        if message.author.id != OWNER_ID or self.bot.user not in message.mentions:
            return

        slips = [a for a in message.attachments if is_image(a)][:MAX_SLIPS]
        if not slips:
            await message.reply(
                "Attach the bet slip screenshot and I'll post it.",
                mention_author=False,
                delete_after=15,
            )
            return

        parsed = parse_post(message.content)

        # Re-upload the screenshots with the new post so they survive the
        # original message being deleted
        files = []
        names = []
        for i, attachment in enumerate(slips, start=1):
            name = slip_filename(i, attachment.filename)
            files.append(discord.File(io.BytesIO(await attachment.read()), filename=name))
            names.append(name)

        embeds = build_bet_embeds(
            units=parsed["units"],
            headline=parsed["headline"],
            note=parsed["note"],
            image_names=names,
            author=message.author.display_name,
            author_icon=message.author.display_avatar.url,
        )

        role = self._ping_role(message.guild)
        if role is None and not self.warned_missing_role:
            self.warned_missing_role = True
            log.warning("No role named %r in %s - posting without a ping", PING_ROLE, message.guild)
            try:
                await message.author.send(
                    f"I couldn't find a role named **{PING_ROLE}** in {message.guild.name}, "
                    "so bet posts won't ping anyone. Set PING_ROLE in .env if it's called "
                    "something else."
                )
            except discord.HTTPException:
                pass

        try:
            await message.channel.send(
                content=role.mention if role else None,
                embeds=embeds,
                files=files,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, users=False, roles=[role] if role else False
                ),
            )
        except discord.HTTPException:
            log.exception("Failed to post bet in #%s", message.channel)
            await message.reply(
                "Couldn't post that - I need Send Messages, Embed Links and "
                "Attach Files in this channel.",
                mention_author=False,
                delete_after=20,
            )
            return

        # The clean post replaces the original message
        try:
            await message.delete()
        except discord.Forbidden:
            log.warning("Posted the bet but can't delete the original - missing Manage Messages")
        except discord.HTTPException:
            log.warning("Posted the bet but couldn't delete the original", exc_info=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BetsCog(bot))
