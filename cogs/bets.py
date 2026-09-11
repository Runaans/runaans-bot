# imports
import asyncio
import io
import logging
import os
import re
import time
from datetime import datetime

import discord
from discord.ext import commands

from config import (
    OWNER_ID,
    BRAND,
    PURPLE,
    EASTERN,
    PING_ROLE,
    PLAYBOOK_BOT_ID,
    PLAYBOOK_WAIT_SECONDS,
)

log = logging.getLogger("runaans.bets")

# Discord allows 10 embeds per message, one screenshot each
MAX_SLIPS = 10

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# "1u", "0.25 u", ".5U", "2 units"
UNITS_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?|\.\d+)\s*u(?:nits?)?\b", re.IGNORECASE)
MENTION_RE = re.compile(r"<@!?\d+>")
MENTION_ID_RE = re.compile(r"<@!?(\d+)>")

# The sportsbook name typed after @Playbook is for Playbook, not the note
BOOK_WORDS = (
    r"draftkings|dk|fanduel|fd|bet365|365|betmgm|mgm|thescore|caesars|"
    r"betrivers|fanatics|pointsbet|proline|espn\s*bet"
)

# A short first line followed by more text is shown as the quoted headline
HEADLINE_MAX = 60

# Playbook's button reads "Find your DraftKings betslip at Playbook"
PLAYBOOK_LABEL_RE = re.compile(r"find your (.+?) betslip", re.IGNORECASE)
URL_RE = re.compile(r"https?://\S+")


def parse_post(text: str, playbook_ids=()) -> dict:
    """Pull units out of a normal message and split it into headline + note.

        @RunaansLocks @Playbook Draftkings 0.25u FD 50% Boost Stimmy
        back from vacation, time to lock in

    -> units ["0.25U"], headline "FD 50% Boost Stimmy", note "back from ..."

    `playbook_ids` are the Playbook bot user ids; the sportsbook word typed
    right after a Playbook mention is dropped along with the mention.
    """
    text = text or ""

    if isinstance(playbook_ids, int):
        playbook_ids = [playbook_ids]
    for pid in playbook_ids:
        text = re.sub(
            rf"<@!?{pid}>\s*(?:(?:{BOOK_WORDS})\b)?", "", text, flags=re.IGNORECASE
        )
    text = MENTION_RE.sub("", text)

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


def extract_betslip(message) -> dict | None:
    """Find the betslip link (and book name) in a Playbook reply.

    Prefers the link button; falls back to a URL in the embed text.
    """
    for row in getattr(message, "components", None) or []:
        for comp in getattr(row, "children", None) or []:
            url = getattr(comp, "url", None)
            if url:
                label = getattr(comp, "label", "") or ""
                m = PLAYBOOK_LABEL_RE.search(label)
                return {"url": url, "book": m.group(1) if m else None}

    for embed in getattr(message, "embeds", None) or []:
        m = URL_RE.search(embed.description or "")
        if m:
            return {"url": m.group(0).rstrip(")>."), "book": None}

    return None


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

    Each screenshot gets its own embed so they stack full-width in one
    column. (Giving the embeds a shared URL would merge them into a 2x2
    gallery that crops the slips, and would turn the title into a link.)
    """
    description = []
    if headline:
        description.append(f"> {headline}")
    if note:
        description.append(note)

    # No URL anywhere, so the title stays plain white text
    embed = discord.Embed(
        title=BRAND,
        description="\n\n".join(description)[:4096] or None,
        color=PURPLE,
        timestamp=datetime.now(EASTERN),
    )

    if units:
        embed.add_field(name="Units", value="\n".join(units), inline=True)

    if author:
        embed.set_footer(text=author, icon_url=author_icon)

    embeds = [embed]

    for i, name in enumerate(image_names):
        if i == 0:
            embed.set_image(url=f"attachment://{name}")
            continue

        extra = discord.Embed(color=PURPLE)
        extra.set_image(url=f"attachment://{name}")
        embeds.append(extra)

    return embeds


def build_betslip_view(betslip: dict | None) -> discord.ui.View | None:
    if not betslip:
        return None

    label = f"Place this bet at {betslip['book']}" if betslip.get("book") else "Place this bet"

    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label=label[:80], url=betslip["url"]))
    return view


class BetsCog(commands.Cog):
    """Turns '@RunaansLocks 1u <note> + screenshot' into a clean bet post."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.warned_missing_role = False
        # Seeded from config; any other bot named "Playbook" in the server is
        # added at startup so a tag on either counts
        self.playbook_ids = {PLAYBOOK_BOT_ID} if PLAYBOOK_BOT_ID else set()
        self._resolve_task = None

    async def cog_load(self):
        self._resolve_task = asyncio.create_task(self._resolve_playbook_ids())

    async def cog_unload(self):
        if self._resolve_task:
            self._resolve_task.cancel()

    async def _resolve_playbook_ids(self):
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            return  # bot never logged in (tests)

        for guild in self.bot.guilds:
            route = discord.http.Route(
                "GET", "/guilds/{guild_id}/members/search", guild_id=guild.id
            )
            try:
                found = await self.bot.http.request(route, params={"query": "playbook", "limit": 20})
            except discord.HTTPException:
                log.warning("Couldn't search %s for Playbook bots", guild, exc_info=True)
                continue

            for member in found:
                user = member.get("user", {})
                if user.get("bot"):
                    self.playbook_ids.add(int(user["id"]))

        log.info("Playbook bot ids: %s", sorted(self.playbook_ids))

    def _tagged_playbook(self, message: discord.Message) -> bool:
        ids = {u.id for u in message.mentions}
        ids |= {int(i) for i in MENTION_ID_RE.findall(message.content or "")}
        return bool(ids & self.playbook_ids)

    def _ping_role(self, guild: discord.Guild) -> discord.Role | None:
        return discord.utils.find(
            lambda r: r.name.lower() == PING_ROLE.lower(), guild.roles
        )

    async def _wait_for_playbook(self, original: discord.Message):
        """Wait for Playbook's reply to `original`, then re-fetch it so its
        button/embed are populated (the live event omits them without the
        message-content intent)."""

        def is_reply(m: discord.Message) -> bool:
            return (
                m.author.id in self.playbook_ids
                and m.channel.id == original.channel.id
                and m.reference is not None
                and m.reference.message_id == original.id
            )

        started = time.monotonic()
        try:
            reply = await self.bot.wait_for("message", check=is_reply, timeout=PLAYBOOK_WAIT_SECONDS)
        except asyncio.TimeoutError:
            log.warning("Playbook didn't reply within %ss - posting without a link", PLAYBOOK_WAIT_SECONDS)
            return None

        log.info("Playbook replied after %.1fs", time.monotonic() - started)
        try:
            return await original.channel.fetch_message(reply.id)
        except discord.HTTPException:
            return reply

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

        parsed = parse_post(message.content, self.playbook_ids)

        tagged = self._tagged_playbook(message)
        log.info(
            "Bet post from %s in #%s: %d slip(s), playbook tagged=%s, mentions=%s",
            message.author, message.channel, len(slips), tagged,
            [u.id for u in message.mentions],
        )
        if not tagged and "playbook" in (message.content or "").lower():
            log.warning(
                "Message says 'playbook' but no Playbook bot was mentioned "
                "(known Playbook ids: %s)", sorted(self.playbook_ids),
            )

        # If Playbook was tagged too, borrow its betslip link for the button
        playbook_msg = None
        if tagged and PLAYBOOK_WAIT_SECONDS:
            playbook_msg = await self._wait_for_playbook(message)

        betslip = extract_betslip(playbook_msg) if playbook_msg else None
        if playbook_msg is not None:
            log.info("Playbook betslip link: %s", betslip["url"] if betslip else None)

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
        view = build_betslip_view(betslip)

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
                **({"view": view} if view else {}),
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

        # The clean post replaces the original message and Playbook's card
        for old in (message, playbook_msg):
            if old is None:
                continue
            try:
                await old.delete()
            except discord.Forbidden:
                log.warning("Posted the bet but can't delete the original - missing Manage Messages")
            except discord.HTTPException:
                log.warning("Posted the bet but couldn't delete a message", exc_info=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BetsCog(bot))
