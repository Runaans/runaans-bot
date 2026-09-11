"""Bot test suite. Run from the repo root with:

    python -m unittest

No Discord, MongoDB, or Google connection is needed - the environment is
faked and the database module is stubbed out before anything imports it.
"""

import os
import re
import sys
import types
import unittest

# Fake environment BEFORE config is imported (real values are never needed)
FAKE_ENV = {
    "GUILD_ID": "123456789",
    "RECAP_CHANNEL": "987654321",
    "OWNER_ID": "111",
    "MONGO_URI": "mongodb://localhost:27017",
    "DISCORD_TOKEN": "fake-token",
    "GOOGLE_CREDENTIALS_FILE": "fake.json",
    "SPREADSHEET_ID": "fake-sheet-id",
    "AUTO_SYNC_MINUTES": "10",
    "AUTO_RECAP_TIME": "23:30",
    # The listener tests exercise sheet logging directly, not via the post
    "SHEET_LOG_ENABLED": "0",
}
for key, value in FAKE_ENV.items():
    os.environ[key] = value


class FakeCollection:
    """Stands in for the Mongo plays collection."""

    def __init__(self):
        self.docs = []

    def find(self, *args, **kwargs):
        return list(self.docs)

    def aggregate(self, *args, **kwargs):
        return []


# Stub the database module before cogs/sheets_sync import it
fake_database = types.ModuleType("database")
fake_database.plays_col = FakeCollection()
fake_database.ensure_indexes = lambda: None
sys.modules["database"] = fake_database

import config
import sheets_sync
from helpers import normalize_date, parse_units
from cogs import recap
from cogs import stats as stats_cog


class TestConfig(unittest.TestCase):
    def test_optional_env_parsing(self):
        self.assertEqual(config.AUTO_SYNC_MINUTES, 10)
        self.assertEqual(
            (config.AUTO_RECAP_TIME.hour, config.AUTO_RECAP_TIME.minute), (23, 30)
        )
        self.assertFalse(config.AUTO_RECAP_ENABLED)
        self.assertEqual(config.SHEET_DATA_START_ROW, 12)


class TestNormalizeDate(unittest.TestCase):
    def test_accepted_formats(self):
        for raw in ("2026-08-19", "8/19/2026", "08/19/2026", "8/19/26", "8-19-2026"):
            self.assertEqual(normalize_date(raw), "2026-08-19", raw)

    def test_rejects_garbage(self):
        for raw in ("yesterday", "", None, "13/45/2026", "2026-8-19x"):
            self.assertIsNone(normalize_date(raw), raw)


class TestParseUnits(unittest.TestCase):
    def test_valid_values(self):
        cases = {
            "1.5": 1.5,
            "1.5u": 1.5,
            "2U": 2.0,
            "2,000": 2000.0,
            "(1.5)": -1.5,
            "-3": -3.0,
            "+1.91": 1.91,
            2.5: 2.5,
        }
        for raw, expected in cases.items():
            self.assertEqual(parse_units(raw), expected, raw)

    def test_invalid_values(self):
        for raw in ("", None, "abc", "PENDING"):
            self.assertIsNone(parse_units(raw), raw)


class TestMapResult(unittest.TestCase):
    def test_explicit_and_inferred(self):
        self.assertEqual(sheets_sync.map_result("w", None), "W")
        self.assertEqual(sheets_sync.map_result("L", 5.0), "L")
        self.assertEqual(sheets_sync.map_result("", 1.5), "W")
        self.assertEqual(sheets_sync.map_result("", -1.0), "L")
        self.assertEqual(sheets_sync.map_result("", 0.0), "P")
        self.assertIsNone(sheets_sync.map_result("", None))


def play(title, result, profit, row=1):
    return {"title": title, "result": result, "profit": profit, "sheet_row": row}


def recap_data(day_plays, **totals):
    return {
        "target_date": "2026-08-19",
        "day_plays": day_plays,
        "month_total": totals.get("month", 0.0),
        "yearly_total": totals.get("year", 0.0),
        "alltime_total": totals.get("alltime", 0.0),
    }


class TestRecapEmbed(unittest.TestCase):
    def test_mixed_day(self):
        data = recap_data(
            [
                play("Lakers ML", "W", 1.5, 1),
                play("Jets +3.5", "L", "-1u", 2),
                play("Over 8.5", "P", 0.0, 3),
                play("Parlay", None, None, 4),
            ],
            month=12.34, year=-5.0, alltime=100.0,
        )
        embed = recap.build_recap_embed(data)
        plays, totals = embed.fields[0].value, embed.fields[1].value

        # Original layout: plain title, two blank-named fields, purple bar
        self.assertEqual(embed.title, "RunaansLocks 8/19/2026 Recap")
        self.assertEqual([f.name for f in embed.fields], ["\u200b", "\u200b"])
        self.assertIsNone(embed.description)
        self.assertEqual(embed.footer.text, "Runaans Locks")

        self.assertIn("✅ Lakers ML +1.50u", plays)
        self.assertIn("❌ Jets +3.5 -1.00u", plays)
        self.assertIn("➖ Over 8.5 +0.00u", plays)
        self.assertIn("⏳ Parlay", plays)
        self.assertEqual(
            totals,
            "**8/19/2026: +0.50u**\n"
            "**THIS MONTH: +12.34u**\n"
            "**YEARLY: -5.00u**\n"
            "**ALL-TIME: +100.00u**",
        )

    def test_always_purple(self):
        for plays in ([play("A", "W", 2.0)], [play("A", "L", -2.0)], [play("A", None, None)]):
            embed = recap.build_recap_embed(recap_data(plays))
            self.assertEqual(embed.color, recap.PURPLE)

    def test_many_plays_stay_under_discord_limits(self):
        plays = [
            play(f"Some Very Long Play Title Number {i} With Extra Words", "W", 1.0, i)
            for i in range(300)
        ]
        embed = recap.build_recap_embed(recap_data(plays))

        # A field value is capped at 1024 characters by Discord
        self.assertLessEqual(len(embed.fields[0].value), 1024)
        self.assertIn("more plays", embed.fields[0].value)
        # Totals still count every play, not just the displayed ones
        self.assertIn("+300.00u", embed.fields[1].value)

        total_len = (
            len(embed.title)
            + sum(len(f.name) + len(f.value) for f in embed.fields)
            + len(embed.footer.text)
        )
        self.assertLessEqual(total_len, 6000)


class TestFmt(unittest.TestCase):
    def test_signs(self):
        self.assertEqual(recap.fmt(1.5), "+1.50u")
        self.assertEqual(recap.fmt(-1.5), "-1.50u")
        self.assertEqual(recap.fmt(0.0), "+0.00u")

    def test_negative_zero_is_not_garbled(self):
        # -0.0 reaches fmt from sheet cells like "-0.00" or "(0)"
        self.assertEqual(recap.fmt(-0.0), "+0.00u")
        self.assertEqual(recap.fmt(parse_units("-0.00")), "+0.00u")


class TestTruncation(unittest.TestCase):
    def test_oversized_first_line_still_shows_plays(self):
        plays = [play("X" * 5000, "W", 1.0, 1)] + [
            play(f"Short play {i}", "W", 1.0, i + 2) for i in range(5)
        ]
        embed = recap.build_recap_embed(recap_data(plays))

        self.assertLessEqual(len(embed.fields[0].value), 1024)
        # The monster line is trimmed, not allowed to swallow everything
        self.assertIn("…", embed.fields[0].value)
        self.assertIn("+6.00u", embed.fields[1].value)


class TestSyncStats(unittest.TestCase):
    def test_summary_reports_unreadable_dates(self):
        stats = sheets_sync.SyncStats(parsed=5, inserted=1)
        self.assertNotIn("unreadable", stats.summary())

        stats.bad_date_rows = [14, 22]
        summary = stats.summary()
        self.assertIn("2 row(s) skipped", summary)
        self.assertIn("14, 22", summary)

    def test_summary_is_console_safe(self):
        """pull_sheets.py prints this; a Windows console is cp1252, so a
        stray emoji here would crash the CLI with UnicodeEncodeError."""
        stats = sheets_sync.SyncStats(parsed=3, bad_date_rows=[14, 22, 30, 31, 32, 33])
        stats.summary().encode("cp1252")


class TestGatherRecapData(unittest.TestCase):
    def test_returns_none_when_day_is_empty(self):
        self.assertIsNone(recap.gather_recap_data("2026-01-01"))


class TestPeriodQuery(unittest.TestCase):
    def test_periods(self):
        today = "2026-08-19"

        self.assertEqual(stats_cog.period_query("today", today)[0], {"date": today})
        self.assertEqual(
            stats_cog.period_query("week", today)[0],
            {"date": {"$gte": "2026-08-13", "$lte": "2026-08-19"}},
        )
        self.assertEqual(
            stats_cog.period_query("month", today)[0],
            {"date": {"$regex": "^2026-08"}},
        )
        self.assertEqual(
            stats_cog.period_query("year", today)[0],
            {"date": {"$regex": "^2026"}},
        )
        self.assertEqual(stats_cog.period_query("all", today), ({}, "All-Time"))


def capper_row(name, wins, losses, profit, staked, pushes=0):
    return {
        "capper": name,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "profit": profit,
        "staked": staked,
        "roi": (profit / staked * 100) if staked else None,
    }


class TestStatsEmbed(unittest.TestCase):
    def test_leaderboard_and_totals(self):
        rows = [
            capper_row("Runaan", 6, 4, 5.0, 10.0),
            capper_row("Ghost", 2, 3, -1.0, 5.0),
        ]
        embed = stats_cog.build_stats_embed(rows, "This Month", None)

        self.assertIn("Record:** 8-7", embed.description)
        self.assertIn("+4.00u", embed.description)
        # ROI = 4.0 profit / 15.0 staked
        self.assertIn("+26.7%", embed.description)
        self.assertEqual(embed.color, stats_cog.GREEN)

        board = embed.fields[0].value
        self.assertIn("🥇 **Runaan** +5.00u (6-4, +50.0%)", board)
        self.assertIn("🥈 **Ghost** -1.00u (2-3, -20.0%)", board)

    def test_single_capper_has_no_leaderboard(self):
        embed = stats_cog.build_stats_embed(
            [capper_row("Runaan", 1, 0, 1.0, 1.0)], "Today", "Runaan"
        )
        self.assertEqual(embed.fields, [])
        self.assertIn("Runaan", embed.title)
        self.assertIn("Today", embed.title)

    def test_empty_period(self):
        embed = stats_cog.build_stats_embed([], "Today", None)
        self.assertIn("No settled plays", embed.description)
        self.assertEqual(embed.color, stats_cog.PURPLE)

    def test_zero_staked_roi_is_not_a_crash(self):
        embed = stats_cog.build_stats_embed(
            [capper_row("Runaan", 0, 0, 0.0, 0.0, pushes=1)], "Today", None
        )
        self.assertIn("ROI:** n/a", embed.description)


class TestCapperFiltering(unittest.TestCase):
    def test_capper_name_with_regex_characters_is_escaped(self):
        """A name like "J (the Kid)" must not blow up the Mongo regex."""
        captured = {}

        class CapturingCollection:
            def aggregate(self, pipeline):
                captured["pipeline"] = pipeline
                return []

        original = stats_cog.plays_col
        stats_cog.plays_col = CapturingCollection()
        try:
            stats_cog.gather_stats({}, "J (the Kid)")
        finally:
            stats_cog.plays_col = original

        clauses = captured["pipeline"][0]["$match"]["$and"]
        pattern = next(c for c in clauses if "capper" in c)["capper"]["$regex"]

        # The parens must arrive escaped, not as a regex group
        self.assertEqual(pattern, "^" + re.escape("J (the Kid)") + "$")
        self.assertIn(r"\(", pattern)
        re.compile(pattern)  # would raise if the name leaked through unescaped
        self.assertTrue(re.fullmatch(pattern, "J (the Kid)"))

    def test_grouping_is_case_insensitive(self):
        captured = {}

        class CapturingCollection:
            def aggregate(self, pipeline):
                captured["pipeline"] = pipeline
                return []

        original = stats_cog.plays_col
        stats_cog.plays_col = CapturingCollection()
        try:
            stats_cog.gather_stats({}, None)
        finally:
            stats_cog.plays_col = original

        group = next(s for s in captured["pipeline"] if "$group" in s)["$group"]
        self.assertEqual(group["_id"], {"$toUpper": {"$ifNull": ["$capper", ""]}})
        self.assertIn("display", group)


class TestSheetRetry(unittest.TestCase):
    def test_retries_on_429_then_succeeds(self):
        import gspread

        calls = {"n": 0}

        class FakeResponse:
            status_code = 429
            headers = {"Retry-After": "0"}
            text = "rate limited"

            @staticmethod
            def json():
                return {"error": {"message": "rate limited", "code": 429}}

        def flaky_client():
            calls["n"] += 1
            if calls["n"] < 3:
                raise gspread.exceptions.APIError(FakeResponse())
            return FakeSheetClient()

        class FakeSheetClient:
            def open_by_key(self, _):
                return self

            def worksheet(self, _):
                return self

            def get_all_values(self):
                return [["ok"]]

        original = sheets_sync._get_client
        sheets_sync._get_client = flaky_client
        try:
            self.assertEqual(sheets_sync.fetch_rows(), [["ok"]])
        finally:
            sheets_sync._get_client = original

        self.assertEqual(calls["n"], 3)

    def test_does_not_retry_permission_errors(self):
        import gspread

        calls = {"n": 0}

        class FakeResponse:
            status_code = 403
            headers = {}
            text = "forbidden"

            @staticmethod
            def json():
                return {"error": {"message": "forbidden", "code": 403}}

        def denied_client():
            calls["n"] += 1
            raise gspread.exceptions.APIError(FakeResponse())

        original = sheets_sync._get_client
        sheets_sync._get_client = denied_client
        try:
            with self.assertRaises(gspread.exceptions.APIError):
                sheets_sync.fetch_rows()
        finally:
            sheets_sync._get_client = original

        # 403 will never succeed - fail immediately instead of burning retries
        self.assertEqual(calls["n"], 1)


class TestBetParsing(unittest.TestCase):
    def test_units_headline_and_note(self):
        from cogs.bets import parse_post

        parsed = parse_post(
            "<@123> 0.25u FD 50% Boost Stimmy\nback from vacation, time to lock in"
        )
        self.assertEqual(
            parsed,
            {
                "units": ["0.25U"],
                "headline": "FD 50% Boost Stimmy",
                "note": "back from vacation, time to lock in",
            },
        )

    def test_multiple_units_and_single_line(self):
        from cogs.bets import parse_post

        parsed = parse_post("<@!123> 0.15u 0.1U  The Unc Stimmy - 365 is giving us a 100% boost")
        self.assertEqual(parsed["units"], ["0.15U", "0.1U"])
        self.assertIsNone(parsed["headline"])
        self.assertEqual(parsed["note"], "The Unc Stimmy - 365 is giving us a 100% boost")

    def test_long_first_line_is_not_a_headline(self):
        from cogs.bets import parse_post

        long_line = "x" * 61
        parsed = parse_post(f"1u {long_line}\nsecond line")
        self.assertIsNone(parsed["headline"])
        self.assertEqual(parsed["note"], f"{long_line}\nsecond line")

    def test_units_written_as_words(self):
        from cogs.bets import parse_post

        self.assertEqual(parse_post("2 units on this")["units"], ["2U"])
        self.assertEqual(parse_post(".5 u lean")["units"], ["0.5U"])

    def test_mention_only(self):
        from cogs.bets import parse_post

        self.assertEqual(parse_post("<@123>"), {"units": [], "headline": None, "note": None})


class TestBetEmbeds(unittest.TestCase):
    def test_single_slip_block(self):
        from cogs.bets import build_bet_embeds

        embeds = build_bet_embeds(
            units=["0.25U"],
            headline="FD 50% Boost Stimmy",
            note="back from vacation, time to lock in",
            image_names=["slip1.png"],
            author="Runaans",
        )

        self.assertEqual(len(embeds), 1)
        embed = embeds[0]
        # Plain white title: no URL on the embed, or Discord makes it a blue link
        self.assertEqual(embed.title, "Runaans Locks")
        self.assertIsNone(embed.url)
        self.assertEqual(
            embed.description, "> FD 50% Boost Stimmy\n\nback from vacation, time to lock in"
        )
        self.assertEqual([(f.name, f.value) for f in embed.fields], [("Units", "0.25U")])
        self.assertEqual(embed.image.url, "attachment://slip1.png")
        self.assertEqual(embed.footer.text, "Runaans")

    def test_no_units_means_no_units_field(self):
        from cogs.bets import build_bet_embeds

        embed = build_bet_embeds(units=[], note="lean", image_names=["slip1.png"])[0]
        self.assertEqual(embed.fields, [])

    def test_multiple_slips_stack_full_width_in_one_column(self):
        from cogs.bets import build_bet_embeds

        embeds = build_bet_embeds(
            units=["0.15U", "0.1U"], image_names=["slip1.png", "slip2.jpg", "slip3.png"]
        )

        # No URLs: a shared URL would merge them into a cropped 2x2 gallery
        self.assertEqual([e.url for e in embeds], [None, None, None])
        self.assertEqual(len(embeds), 3)
        self.assertEqual(
            [e.image.url for e in embeds],
            ["attachment://slip1.png", "attachment://slip2.jpg", "attachment://slip3.png"],
        )
        self.assertEqual([e.fields for e in embeds[1:]], [[], []])

    def test_slip_filenames(self):
        from cogs.bets import slip_filename

        self.assertEqual(slip_filename(1, "IMG_2231.JPG"), "slip1.jpg")
        self.assertEqual(slip_filename(2, "weird.heic"), "slip2.png")


class FakeAttachment:
    def __init__(self, filename="slip.png", content_type="image/png"):
        self.filename, self.content_type = filename, content_type

    async def read(self):
        return b"\x89PNG\r\n\x1a\n"


PLAYBOOK_ID = config.PLAYBOOK_BOT_ID


def fake_playbook_reply(original_id, log, url="https://links.playbook.actionnetwork.com/nSNdo",
                        label="Find your DraftKings betslip at Playbook", description=None):
    """Playbook AI's reply as fetched over REST, replying to the original.

    With `url` it carries the link button; with url=None and a `description`
    it's the placeholder or the "not available" message."""

    async def delete():
        log.append(("delete_playbook", None))

    buttons = [types.SimpleNamespace(label=label, url=url)] if url else []
    embeds = [types.SimpleNamespace(description=description)] if description else []

    return types.SimpleNamespace(
        id=777,
        author=types.SimpleNamespace(id=PLAYBOOK_ID, bot=True),
        channel=types.SimpleNamespace(id=1),
        reference=types.SimpleNamespace(message_id=original_id),
        components=[types.SimpleNamespace(children=buttons)] if buttons else [],
        embeds=embeds,
        delete=delete,
    )


PLAYBOOK_NO_MATCH = ("Those selections are not yet available in our system. Stay tuned as we "
                     "continue expanding. Learn more about Playbook [here](https://playbookbot.com).")


def fake_message(*, author_id=111, mention_bot=True, attachments=None, roles=None,
                 content="<@999> 1u lock", fail_send=False, mention_playbook=False,
                 playbook=None):
    """A message as the bets listener sees it, plus a fake bot. `log` records
    call order. `playbook` is Playbook's reply, or None for a timeout."""
    import asyncio
    import discord

    log = []
    bot_user = types.SimpleNamespace(id=999)

    # Playbook's reply records its deletion into this same log. A list means
    # successive states of the reply: what wait_for sees, then each re-fetch.
    if callable(playbook):
        playbook = playbook(log)
    states = list(playbook) if isinstance(playbook, list) else [playbook]

    async def channel_send(**kw):
        log.append(("send", kw))
        if fail_send:
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="x"), "nope")

    async def fetch_message(message_id):
        log.append(("fetch", message_id))
        if len(states) > 1:
            states.pop(0)
        return states[0]

    async def reply(text, **kw):
        log.append(("reply", text))

    async def delete():
        log.append(("delete", None))

    async def dm(text):
        log.append(("dm", text))

    mentions = [bot_user] if mention_bot else []
    if mention_playbook:
        mentions.append(types.SimpleNamespace(id=PLAYBOOK_ID))

    msg = types.SimpleNamespace(
        id=123,
        author=types.SimpleNamespace(
            id=author_id, bot=False, display_name="Runaans", send=dm,
            display_avatar=types.SimpleNamespace(url="https://cdn/x.png")),
        guild=types.SimpleNamespace(name="Runaans Locks", roles=roles or []),
        mentions=mentions,
        attachments=[FakeAttachment()] if attachments is None else attachments,
        content=content,
        channel=types.SimpleNamespace(id=1, send=channel_send, fetch_message=fetch_message),
        reply=reply,
        delete=delete,
    )

    async def wait_for(event, *, check, timeout):
        log.append(("wait", timeout))
        if states[0] is None:
            raise asyncio.TimeoutError
        assert check(states[0]), "listener's check rejected Playbook's own reply"
        return states[0]

    bot = types.SimpleNamespace(user=bot_user, wait_for=wait_for)
    return msg, bot, log


PREMIUM = types.SimpleNamespace(id=555, name="Premium", mention="<@&555>")


class TestBetListener(unittest.IsolatedAsyncioTestCase):
    def cog(self, bot):
        from cogs.bets import BetsCog

        return BetsCog(bot)

    async def test_no_playbook_tag_means_no_waiting(self):
        msg, bot, log = fake_message(roles=[PREMIUM])
        await self.cog(bot).on_message(msg)
        self.assertNotIn("wait", [name for name, _ in log])
        self.assertNotIn("view", log[0][1])

    async def test_playbook_link_becomes_button_and_its_card_is_deleted(self):
        msg, bot, log = fake_message(
            roles=[PREMIUM], mention_playbook=True,
            content="<@999> <@%d> Draftkings 1u lock" % PLAYBOOK_ID,
            playbook=lambda log: fake_playbook_reply(123, log),
        )
        await self.cog(bot).on_message(msg)

        names = [name for name, _ in log]
        self.assertEqual(names, ["wait", "fetch", "send", "delete", "delete_playbook"])
        sent = log[2][1]
        button = sent["view"].children[0]
        self.assertEqual(button.label, "Place this bet at DraftKings")
        self.assertEqual(button.url, "https://links.playbook.actionnetwork.com/nSNdo")
        # "Draftkings" was for Playbook, not the note
        self.assertEqual(sent["embeds"][0].description, "lock")

    async def test_playbook_timeout_posts_without_button(self):
        msg, bot, log = fake_message(roles=[PREMIUM], mention_playbook=True, playbook=None)
        await self.cog(bot).on_message(msg)

        names = [name for name, _ in log]
        self.assertEqual(names, ["wait", "send", "delete"])
        self.assertNotIn("view", log[1][1])

    async def test_playbook_placeholder_is_polled_until_link_appears(self):
        """Playbook replies instantly with a placeholder and edits the link in later."""
        from cogs import bets

        def states(log):
            return [
                fake_playbook_reply(123, log, url=None, description="Working on your betslip..."),
                fake_playbook_reply(123, log, url=None, description="Working on your betslip..."),
                fake_playbook_reply(123, log),  # link edited in
            ]

        msg, bot, log = fake_message(roles=[PREMIUM], mention_playbook=True, playbook=states)
        original_poll = bets.PLAYBOOK_POLL_SECONDS
        bets.PLAYBOOK_POLL_SECONDS = 0
        try:
            await self.cog(bot).on_message(msg)
        finally:
            bets.PLAYBOOK_POLL_SECONDS = original_poll

        names = [name for name, _ in log]
        self.assertEqual(names.count("fetch"), 2)  # kept re-fetching until the link showed
        self.assertEqual(names[-3:], ["send", "delete", "delete_playbook"])
        sent = next(kw for name, kw in log if name == "send")
        self.assertEqual(sent["view"].children[0].url, "https://links.playbook.actionnetwork.com/nSNdo")

    async def test_playbook_giving_up_stops_polling_and_posts_without_button(self):
        from cogs import bets

        def states(log):
            return [
                fake_playbook_reply(123, log, url=None, description="Working on your betslip..."),
                fake_playbook_reply(123, log, url=None, description=PLAYBOOK_NO_MATCH),
            ]

        msg, bot, log = fake_message(roles=[PREMIUM], mention_playbook=True, playbook=states)
        original_poll = bets.PLAYBOOK_POLL_SECONDS
        bets.PLAYBOOK_POLL_SECONDS = 0
        try:
            await self.cog(bot).on_message(msg)
        finally:
            bets.PLAYBOOK_POLL_SECONDS = original_poll

        names = [name for name, _ in log]
        self.assertEqual(names.count("fetch"), 1)  # gave up as soon as Playbook did
        sent = next(kw for name, kw in log if name == "send")
        self.assertNotIn("view", sent)
        self.assertIn("delete_playbook", names)  # its "not available" card is cleaned up too


    async def test_posts_pings_premium_then_deletes_original(self):
        msg, bot, log = fake_message(roles=[PREMIUM])
        await self.cog(bot).on_message(msg)

        self.assertEqual([name for name, _ in log], ["send", "delete"])
        sent = log[0][1]
        self.assertEqual(sent["content"], "<@&555>")
        self.assertEqual(sent["allowed_mentions"].to_dict()["roles"], [555])
        self.assertEqual([f.filename for f in sent["files"]], ["slip1.png"])
        self.assertEqual(sent["embeds"][0].fields[0].value, "1U")

    async def test_ignores_other_people_and_unmentioned_messages(self):
        for kwargs in ({"author_id": 222}, {"mention_bot": False}):
            msg, bot, log = fake_message(roles=[PREMIUM], **kwargs)
            await self.cog(bot).on_message(msg)
            self.assertEqual(log, [], kwargs)

    async def test_no_screenshot_asks_for_one_and_keeps_message(self):
        msg, bot, log = fake_message(roles=[PREMIUM], attachments=[])
        await self.cog(bot).on_message(msg)
        self.assertEqual([name for name, _ in log], ["reply"])

    async def test_missing_role_posts_without_ping_and_dms_owner_once(self):
        msg, bot, log = fake_message(roles=[])
        cog = self.cog(bot)
        await cog.on_message(msg)
        await cog.on_message(msg)

        sends = [kw for name, kw in log if name == "send"]
        self.assertEqual(len(sends), 2)
        self.assertIsNone(sends[0]["content"])
        self.assertEqual(sends[0]["allowed_mentions"].to_dict().get("roles", []), [])
        self.assertEqual(sum(1 for name, _ in log if name == "dm"), 1)

    async def test_failed_post_keeps_original_message(self):
        msg, bot, log = fake_message(roles=[PREMIUM], fail_send=True)
        await self.cog(bot).on_message(msg)

        names = [name for name, _ in log]
        self.assertIn("reply", names)
        self.assertNotIn("delete", names)


class TestPlaybookParsing(unittest.TestCase):
    def test_book_word_after_playbook_is_dropped(self):
        from cogs.bets import parse_post

        for text in (
            "<@999> <@%d> Draftkings 1u back from vacation" % PLAYBOOK_ID,
            "<@999> 1u back from vacation <@!%d> dk" % PLAYBOOK_ID,
            "<@999> 1u back from vacation <@%d>" % PLAYBOOK_ID,
        ):
            parsed = parse_post(text, PLAYBOOK_ID)
            self.assertEqual(parsed["note"], "back from vacation", text)
            self.assertEqual(parsed["units"], ["1U"])

    def test_book_words_elsewhere_are_kept(self):
        from cogs.bets import parse_post

        parsed = parse_post("<@999> 1u bet365 is giving a 100%% boost <@%d> bet365" % PLAYBOOK_ID, PLAYBOOK_ID)
        self.assertEqual(parsed["note"], "bet365 is giving a 100% boost")

    def test_extract_betslip_prefers_button_then_embed_text(self):
        from cogs.bets import extract_betslip

        with_button = fake_playbook_reply(1, [], label="Find your bet365 betslip at Action")
        self.assertEqual(extract_betslip(with_button),
                         {"url": "https://links.playbook.actionnetwork.com/nSNdo", "book": "bet365"})

        embed = types.SimpleNamespace(
            description="We matched 1 of 1 markets! [Click here](https://links.playbook.actionnetwork.com/zQMgh)")
        text_only = types.SimpleNamespace(components=[], embeds=[embed])
        self.assertEqual(extract_betslip(text_only),
                         {"url": "https://links.playbook.actionnetwork.com/zQMgh", "book": None})

        self.assertIsNone(extract_betslip(types.SimpleNamespace(components=[], embeds=[])))

    def test_not_available_message_is_not_a_betslip_link(self):
        from cogs.bets import extract_betslip, playbook_gave_up

        reply = fake_playbook_reply(1, [], url=None, description=PLAYBOOK_NO_MATCH)
        # It links to playbookbot.com, which is not a betslip
        self.assertIsNone(extract_betslip(reply))
        self.assertTrue(playbook_gave_up(reply))

        placeholder = fake_playbook_reply(1, [], url=None, description="Working on your betslip...")
        self.assertIsNone(playbook_gave_up(placeholder))


class TestBetLogger(unittest.TestCase):
    def test_pair_units(self):
        from bet_logger import pair_units

        self.assertEqual(pair_units(["0.15U", "0.1U"], 2), [0.15, 0.1])
        self.assertEqual(pair_units(["1U"], 3), [1.0, 1.0, 1.0])
        self.assertEqual(pair_units(["1U", "2U"], 3), [1.0, 1.0, 1.0])
        self.assertEqual(pair_units([], 2), [None, None])

    def test_odds_number_matches_sheet_convention(self):
        from bet_logger import odds_number

        self.assertEqual(odds_number("+716"), 716)
        self.assertEqual(odds_number("-115"), -115)
        self.assertEqual(odds_number("137"), 137)
        self.assertEqual(odds_number(None), "")
        self.assertEqual(odds_number("1.91"), "")  # decimal odds aren't logged

    def test_build_rows(self):
        from bet_logger import SlipRead, build_rows

        reads = [
            SlipRead(is_betslip=True, selection="Kyren Williams Over 53.5 Rush Yds",
                     odds="-115", sport="football", legs=1),
            SlipRead(is_betslip=False, selection="", odds=None, sport="other", legs=0),
            SlipRead(is_betslip=True, selection="Spain GK 4+ Saves", odds=None,
                     sport="soccer", legs=1),
            SlipRead(is_betslip=True, selection="Leafs ML", odds="+120", sport="hockey", legs=1),
        ]
        rows = build_rows(reads, [0.25, None, 1.0, 0.5], "FD 50% Boost", "9/11/2026")

        # The non-slip image is skipped; sports map to the dropdown's values
        self.assertEqual(
            [r["a_to_e"] for r in rows],
            [
                ["9/11/2026", "\U0001f3c8", "Kyren Williams Over 53.5 Rush Yds", 0.25, -115],
                ["9/11/2026", "⚽", "Spain GK 4+ Saves", 1.0, ""],
                ["9/11/2026", "Other", "Leafs ML", 0.5, 120],
            ],
        )
        self.assertEqual([r["note"] for r in rows], ["FD 50% Boost"] * 3)

    def test_log_bets_writes_only_a_to_e_and_k(self):
        import bet_logger
        from bet_logger import SlipRead, build_rows

        calls = []

        class FakeWorksheet:
            def col_values(self, col):
                return ["", "", "9/10/2026", "9/10/2026", "", ""]  # last date in row 4

            def update(self, *, range_name, values, value_input_option=None):
                calls.append(("update", range_name, values, value_input_option))

            def batch_clear(self, ranges):
                calls.append(("clear", ranges))

        rows = build_rows(
            [SlipRead(is_betslip=True, selection="Kane SOA", odds="+250", sport="soccer", legs=1)],
            [1.0], "", "9/11/2026",
        )
        original = bet_logger._worksheet
        bet_logger._worksheet = lambda: FakeWorksheet()
        try:
            logged = bet_logger.log_bets(rows)
            bet_logger.clear_rows([r.row for r in logged])
        finally:
            bet_logger._worksheet = original

        self.assertEqual(logged[0].row, 5)
        self.assertEqual(logged[0].line(), "row 5 - ⚽ Kane SOA · 1u · +250")
        self.assertEqual(
            calls,
            [
                ("update", "A5:E5", [["9/11/2026", "⚽", "Kane SOA", 1.0, 250]], "USER_ENTERED"),
                ("clear", ["A5:E5", "K5"]),
            ],
        )

    def test_read_slip_uses_structured_output_and_handles_refusal(self):
        import bet_logger
        from bet_logger import SlipRead

        seen = {}

        class FakeMessages:
            def parse(self, **kw):
                seen.update(kw)
                return types.SimpleNamespace(
                    stop_reason="end_turn",
                    parsed_output=SlipRead(is_betslip=True, selection="Kane SOA", odds="+250",
                                           sport="soccer", legs=1),
                )

        original = bet_logger._get_client
        bet_logger._get_client = lambda: types.SimpleNamespace(messages=FakeMessages())
        try:
            read = bet_logger.read_slip(b"\x89PNG", "image/png")
        finally:
            bet_logger._get_client = original

        self.assertEqual(read.selection, "Kane SOA")
        self.assertEqual(seen["model"], "claude-opus-5")
        self.assertIs(seen["output_format"], SlipRead)
        image = seen["messages"][0]["content"][0]
        self.assertEqual(image["source"]["media_type"], "image/png")
        self.assertEqual(image["source"]["data"], "iVBORw==")  # base64 of the bytes

        class Refusing:
            def parse(self, **kw):
                return types.SimpleNamespace(stop_reason="refusal", stop_details=None, parsed_output=None)

        bet_logger._get_client = lambda: types.SimpleNamespace(messages=Refusing())
        try:
            self.assertIsNone(bet_logger.read_slip(b"x", "image/png"))
        finally:
            bet_logger._get_client = original


class TestSheetLoggingFlow(unittest.IsolatedAsyncioTestCase):
    """The cog's end-to-end logging step with Claude and the sheet faked."""

    def setUp(self):
        import bet_logger
        self.bet_logger = bet_logger
        self.saved = (bet_logger.available, bet_logger.read_slips, bet_logger.log_bets)

    def tearDown(self):
        (self.bet_logger.available, self.bet_logger.read_slips,
         self.bet_logger.log_bets) = self.saved

    def owner(self):
        dms = []

        async def send(text, **kw):
            dms.append((text, kw))

        return types.SimpleNamespace(id=111, send=send), dms

    async def test_logs_and_dms_with_undo(self):
        from cogs import bets
        from bet_logger import SlipRead, LoggedRow

        self.bet_logger.available = lambda: True
        self.bet_logger.read_slips = lambda payloads: [
            SlipRead(is_betslip=True, selection="Kane SOA", odds="+250", sport="soccer", legs=1)
        ]
        written = {}

        def fake_log(rows):
            written["rows"] = rows
            return [LoggedRow(1387, "⚽", "Kane SOA", 1.0, "+250")]

        self.bet_logger.log_bets = fake_log
        synced = []
        original_sync = bets.sync_sheet
        bets.sync_sheet = lambda: synced.append(True)

        owner, dms = self.owner()
        cog = bets.BetsCog(types.SimpleNamespace(user=types.SimpleNamespace(id=999)))
        try:
            await cog._log_to_sheet(owner, [(b"img", "image/png")],
                                    {"units": ["1U"], "headline": "Kane SOA", "note": "lock"})
        finally:
            bets.sync_sheet = original_sync

        self.assertEqual(written["rows"][0]["a_to_e"][2:], ["Kane SOA", 1.0, 250])
        self.assertEqual(written["rows"][0]["note"], "Kane SOA - lock")
        self.assertEqual(len(dms), 1)
        text, kw = dms[0]
        self.assertIn("row 1387", text)
        self.assertIsInstance(kw["view"], bets.UndoLogView)
        self.assertEqual(kw["view"].rows, [1387])
        self.assertEqual(synced, [True])  # /recap sees it right away

    async def test_without_api_key_dms_setup_hint_once(self):
        from cogs import bets

        self.bet_logger.available = lambda: False
        owner, dms = self.owner()
        cog = bets.BetsCog(types.SimpleNamespace(user=types.SimpleNamespace(id=999)))

        await cog._log_to_sheet(owner, [(b"img", "image/png")], {"units": [], "headline": None, "note": None})
        await cog._log_to_sheet(owner, [(b"img", "image/png")], {"units": [], "headline": None, "note": None})

        self.assertEqual(len(dms), 1)
        self.assertIn("ANTHROPIC_API_KEY", dms[0][0])

    async def test_unreadable_slip_asks_to_log_by_hand(self):
        from cogs import bets

        self.bet_logger.available = lambda: True

        def boom(payloads):
            raise RuntimeError("api down")

        self.bet_logger.read_slips = boom
        owner, dms = self.owner()
        cog = bets.BetsCog(types.SimpleNamespace(user=types.SimpleNamespace(id=999)))

        await cog._log_to_sheet(owner, [(b"img", "image/png")], {"units": ["1U"], "headline": None, "note": None})

        self.assertEqual(len(dms), 1)
        self.assertIn("by hand", dms[0][0])


class TestCogsLoad(unittest.IsolatedAsyncioTestCase):
    """Loads every cog into a real Bot - catches command registration,
    decorator and signature errors without connecting to Discord."""

    async def test_all_cogs_load_and_register_commands(self):
        import discord
        from discord.ext import commands as dpy_commands
        from config import GUILD_ID

        intents = discord.Intents.default()
        intents.guilds = True
        bot = dpy_commands.Bot(command_prefix="!", intents=intents)

        try:
            for cog in ("cogs.recap", "cogs.sync", "cogs.stats", "cogs.bets"):
                await bot.load_extension(cog)

            names = {c.name for c in bot.tree.get_commands(guild=GUILD_ID)}
            self.assertEqual(names, {"recap", "sync", "stats"})
            # Bets are posted by mentioning the bot, not a slash command
            self.assertIsNotNone(bot.get_cog("BetsCog"))

            # /recap must offer autocomplete on its date option
            recap_cmd = next(
                c for c in bot.tree.get_commands(guild=GUILD_ID) if c.name == "recap"
            )
            date_param = next(p for p in recap_cmd.parameters if p.name == "date")
            self.assertTrue(date_param.autocomplete)
        finally:
            await bot.close()


if __name__ == "__main__":
    unittest.main()
