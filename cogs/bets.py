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
    GAMBLY_BOT_ID,
    GAMBLY_WAIT_SECONDS,
)

log = logging.getLogger("runaans.bets")

# Discord allows 10 embeds per message, one screenshot each
MAX_SLIPS = 10

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# "1u", "0.25 u", ".5U", "2 units"
UNITS_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?|\.\d+)\s*u(?:nits?)?\b", re.IGNORECASE)
MENTION_RE = re.compile(r"<@!?\d+>")
MENTION_ID_RE = re.compile(r"<@!?(\d+)>")

# A role ping and @everyone/@here. The post does its own @Premium ping, so one
# typed into the trigger message is how the room was addressed rather than part
# of the write-up. MENTION_RE above does not reach it - a role mention is
# <@&id>, not <@id> - which is how the pill arrived inside the card, and, being
# a short first line, as its quoted headline.
PING_RE = re.compile(r"<@&\d+>|@everyone\b|@here\b")

# A sportsbook name typed right after @Gambly is for Gambly, not the note
BOOK_WORDS = (
    r"draftkings|dk|fanduel|fd|bet365|365|betmgm|mgm|thescore|caesars|"
    r"betrivers|fanatics|pointsbet|proline|espn\s*bet"
)

# A short first line followed by more text is shown as the quoted headline
HEADLINE_MAX = 60

# A colon straight after an amount gives it words in the Units field:
# "0.2u: 3/2/3 RR Combo" is the unit "0.2U: 3/2/3 RR Combo". The words run to the
# next amount or the end of the line, so several fit on one line. Without the
# colon an amount is just an amount, and the rest of the line is the headline
# or note ("0.25u FD 50% Boost Stimmy"). Guessing from words like "on" or
# "parlay" put too much in Units and too little, so it is the colon, nothing
# else.
UNIT_COLON_RE = re.compile(r"\s*:")

# Gambly reads the slip image, so it may answer with a placeholder and edit
# the betslip in later. Re-fetch its reply at this interval.
GAMBLY_POLL_SECONDS = 1.5


def _unit(match) -> str:
    return f"{float(match.group(1)):g}U"


def _units_in_line(line: str) -> tuple[str, list[str]]:
    """Split one line into (what is left for the note, its units)."""
    amounts = list(UNITS_RE.finditer(line))
    if not amounts:
        return line, []

    units = []
    rest = [line[:amounts[0].start()]]
    for i, m in enumerate(amounts):
        end = amounts[i + 1].start() if i + 1 < len(amounts) else len(line)
        after = line[m.end():end]
        colon = UNIT_COLON_RE.match(after)
        words = after[colon.end():].strip(" \t,;") if colon else ""
        if colon and words:
            units.append(re.sub(r"[ \t]+", " ", f"{_unit(m)}: {words}"))
        else:
            units.append(_unit(m))
            rest.append(after[colon.end():] if colon else after)

    return " ".join(r.strip(" \t,;") for r in rest if r.strip(" \t,;")), units


def parse_post(text: str, gambly_ids=()) -> dict:
    """Pull units out of a normal message and split it into headline + note.

        @Premium @RunaansLocks @GamblyBot 0.25u FD 50% Boost Stimmy
        back from vacation, time to lock in

    -> units ["0.25U"], headline "FD 50% Boost Stimmy", note "back from ..."

    Every ping goes: the bot's own mention, @GamblyBot, a role, @everyone.
    They are how the message was addressed, and the card says what was written.

    `gambly_ids` are the Gambly bot user ids; a sportsbook word typed right
    after a Gambly mention is dropped along with the mention.
    """
    text = text or ""

    if isinstance(gambly_ids, int):
        gambly_ids = [gambly_ids]
    for gid in gambly_ids:
        text = re.sub(
            rf"<@!?{gid}>\s*(?:(?:{BOOK_WORDS})\b)?", "", text, flags=re.IGNORECASE
        )
    text = MENTION_RE.sub("", text)
    text = PING_RE.sub("", text)

    units = []
    lines = []
    for line in text.splitlines():
        rest, line_units = _units_in_line(line)
        units += line_units
        lines.append(rest)

    lines = [re.sub(r"[ \t]+", " ", line).strip(" \t-–:|") for line in lines]
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


# Gambly's links point at its own site. Its install page lives there too, and
# is not a betslip - nor is its "Open in DraftKings" button, a gambly.com
# /deeplink/ into one sportsbook, which a multi-button reply once turned into
# a "Bet 2" that opened the same bets in DraftKings.
GAMBLY_URL_RE = re.compile(r"https://(?:[\w-]+\.)*gambly\.com/(?!discord/|deeplink/)\S+", re.IGNORECASE)
GAMBLY_LABEL = "Place this bet with Gambly"

# How a reply reads when it couldn't match the bet
NO_MATCH_RE = re.compile(
    r"not (?:yet )?available|couldn't find|could not find|no match|0 of \d+|unable to",
    re.IGNORECASE,
)


def extract_betslips(message) -> list[dict]:
    """Every betslip link in a Gambly reply, in order.

    Gambly's link buttons on gambly.com first; only if there are none, the
    gambly.com links in its embeds or text. With more than one, each is
    labelled 'Bet N' so the buttons say which bet they load."""
    urls = []
    for row in getattr(message, "components", None) or []:
        for comp in getattr(row, "children", None) or []:
            url = getattr(comp, "url", None)
            if url and GAMBLY_URL_RE.match(url) and url not in urls:
                urls.append(url)

    if not urls:
        texts = [getattr(message, "content", None) or ""]
        texts += [getattr(e, "description", None) or "" for e in getattr(message, "embeds", None) or []]
        for text in texts:
            for m in GAMBLY_URL_RE.finditer(text):
                url = m.group(0).rstrip(")>.")
                if url not in urls:
                    urls.append(url)

    if len(urls) == 1:
        return [{"url": urls[0], "label": GAMBLY_LABEL}]
    return [{"url": url, "label": f"{GAMBLY_LABEL} \u2013 Bet {i}"} for i, url in enumerate(urls, start=1)]


def gambly_buttons(message) -> list:
    """What Gambly's reply actually carried, for the log."""
    return [
        (getattr(c, "label", None), getattr(c, "url", None))
        for row in getattr(message, "components", None) or []
        for c in getattr(row, "children", None) or []
    ]


def extract_betslip(message) -> dict | None:
    """Find the betslip link in a Gambly reply.

    A link button on gambly.com first, then a gambly.com link in the embed or
    message text. Any other link it carries - a sportsbook's own page, its
    install page - is not what the button is for.
    """
    for row in getattr(message, "components", None) or []:
        for comp in getattr(row, "children", None) or []:
            url = getattr(comp, "url", None)
            if url and GAMBLY_URL_RE.match(url):
                return {"url": url, "label": GAMBLY_LABEL}

    texts = [getattr(message, "content", None) or ""]
    texts += [getattr(e, "description", None) or "" for e in getattr(message, "embeds", None) or []]
    for text in texts:
        m = GAMBLY_URL_RE.search(text)
        if m:
            return {"url": m.group(0).rstrip(")>."), "label": GAMBLY_LABEL}

    return None


def gambly_gave_up(message) -> str | None:
    """The text of Gambly's reply if it says it couldn't match the bet."""
    texts = [getattr(message, "content", None) or ""]
    texts += [getattr(e, "description", None) or "" for e in getattr(message, "embeds", None) or []]
    for text in texts:
        if NO_MATCH_RE.search(text):
            return text

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


def build_betslip_view(betslips) -> discord.ui.View | None:
    """One link button per betslip. Takes a single betslip or a list."""
    if isinstance(betslips, dict):
        betslips = [betslips]
    betslips = [b for b in betslips or [] if b]
    if not betslips:
        return None

    view = discord.ui.View(timeout=None)
    for betslip in betslips[:25]:
        view.add_item(discord.ui.Button(label=betslip.get("label", GAMBLY_LABEL)[:80], url=betslip["url"]))
    return view


class BetsCog(commands.Cog):
    """Turns '@RunaansLocks 1u <note> + screenshot' into a clean bet post."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.warned_missing_role = False
        # Seeded from config; any other bot named "Gambly" in the server is
        # added at startup so a tag on either counts
        self.gambly_ids = {GAMBLY_BOT_ID} if GAMBLY_BOT_ID else set()
        self._resolve_task = None

    async def cog_load(self):
        self._resolve_task = asyncio.create_task(self._resolve_gambly_ids())

    async def cog_unload(self):
        if self._resolve_task:
            self._resolve_task.cancel()

    async def _resolve_gambly_ids(self):
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            return  # bot never logged in (tests)

        for guild in self.bot.guilds:
            route = discord.http.Route(
                "GET", "/guilds/{guild_id}/members/search", guild_id=guild.id
            )
            try:
                found = await self.bot.http.request(route, params={"query": "gambly", "limit": 20})
            except discord.HTTPException:
                log.warning("Couldn't search %s for Gambly bots", guild, exc_info=True)
                continue

            for member in found:
                user = member.get("user", {})
                if user.get("bot"):
                    self.gambly_ids.add(int(user["id"]))

        log.info("Gambly bot ids: %s", sorted(self.gambly_ids))

    def _tagged_gambly(self, message: discord.Message) -> bool:
        ids = {u.id for u in message.mentions}
        ids |= {int(i) for i in MENTION_ID_RE.findall(message.content or "")}
        return bool(ids & self.gambly_ids)

    def _addressed(self, message: discord.Message) -> bool:
        """True when the bot was named in the text. That is the trigger.

        `message.mentions` answers a slightly different question: replying to
        one of the bot's own posts with the ping left on puts the bot in that
        list without @RunaansLocks having been typed anywhere.

        The mention is also what makes the text readable at all without the
        privileged message-content intent, so an empty content is text that
        never arrived rather than a message that failed to name the bot - and
        there the mention list is all there is to go on.
        """
        me = self.bot.user
        if me is None or me not in message.mentions:
            return False

        content = message.content or ""
        return not content or str(me.id) in MENTION_ID_RE.findall(content)

    def _ping_role(self, guild: discord.Guild) -> discord.Role | None:
        return discord.utils.find(
            lambda r: r.name.lower() == PING_ROLE.lower(), guild.roles
        )

    async def _wait_for_gambly(self, original: discord.Message):
        """Wait for Gambly's answer to `original`, then re-fetch it so its
        button/embed are populated (the live event omits them without the
        message-content intent).

        Gambly may answer in the channel rather than as a reply, so a message
        from it in the same channel that replies to nothing counts too. A
        reply to some other message does not."""

        def is_answer(m: discord.Message) -> bool:
            if m.author.id not in self.gambly_ids or m.channel.id != original.channel.id:
                return False
            return m.reference is None or m.reference.message_id == original.id

        started = time.monotonic()
        try:
            reply = await self.bot.wait_for("message", check=is_answer, timeout=GAMBLY_WAIT_SECONDS)
        except asyncio.TimeoutError:
            log.warning("Gambly didn't reply within %ss - posting without a link", GAMBLY_WAIT_SECONDS)
            return None

        log.info("Gambly replied after %.1fs", time.monotonic() - started)

        # The first reply may be a placeholder; keep re-fetching until the
        # betslip link has been edited in, or the wait budget runs out
        deadline = started + GAMBLY_WAIT_SECONDS
        latest = reply
        while True:
            try:
                latest = await original.channel.fetch_message(reply.id)
            except discord.NotFound:
                log.warning("Gambly removed its own reply")
                return None
            except discord.HTTPException:
                log.warning("Couldn't re-fetch Gambly's reply", exc_info=True)

            if extract_betslip(latest):
                log.info("Gambly link ready after %.1fs", time.monotonic() - started)
                return latest

            gave_up = gambly_gave_up(latest)
            if gave_up:
                log.warning("Gambly couldn't match this bet, no link: %s", gave_up[:160])
                return latest

            if time.monotonic() >= deadline:
                log.warning("Gambly never added a betslip link within %ss", GAMBLY_WAIT_SECONDS)
                return latest

            await asyncio.sleep(GAMBLY_POLL_SECONDS)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Only the owner's messages, in a server, that name the bot.
        if message.author.bot or message.guild is None:
            return
        if message.author.id != OWNER_ID or not self._addressed(message):
            return

        slips = [a for a in message.attachments if is_image(a)][:MAX_SLIPS]
        if not slips:
            await message.reply(
                "Attach the bet slip screenshot and I'll post it.",
                mention_author=False,
                delete_after=15,
            )
            return

        parsed = parse_post(message.content, self.gambly_ids)

        tagged = self._tagged_gambly(message)
        log.info(
            "Bet post from %s in #%s: %d slip(s), gambly tagged=%s, mentions=%s",
            message.author, message.channel, len(slips), tagged,
            [u.id for u in message.mentions],
        )
        if not tagged and "gambly" in (message.content or "").lower():
            log.warning(
                "Message says 'gambly' but no Gambly bot was mentioned "
                "(known Gambly ids: %s)", sorted(self.gambly_ids),
            )

        # If Gambly was tagged too, borrow its betslip link for the button
        gambly_msg = None
        if tagged and GAMBLY_WAIT_SECONDS:
            gambly_msg = await self._wait_for_gambly(message)

        betslips = extract_betslips(gambly_msg) if gambly_msg else []
        if gambly_msg is not None:
            log.info("Gambly buttons: %s", gambly_buttons(gambly_msg))
            log.info("Gambly betslip links: %s", [b["url"] for b in betslips])

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
        view = build_betslip_view(betslips)

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

        # The clean post replaces the original message and Gambly's card
        for old in (message, gambly_msg):
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
