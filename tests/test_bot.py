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
        self.assertEqual(embed.title, "Runaans Locks")
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

    def test_multiple_slips_share_one_gallery_block(self):
        from cogs.bets import build_bet_embeds

        embeds = build_bet_embeds(
            units=["0.15U", "0.1U"], image_names=["slip1.png", "slip2.jpg", "slip3.png"]
        )

        # Same URL on every embed is what makes Discord merge them into one block
        self.assertEqual(len({e.url for e in embeds}), 1)
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


def fake_message(*, author_id=111, mention_bot=True, attachments=None, roles=None,
                 content="<@999> 1u lock", fail_send=False):
    """A message as the bets listener sees it. `log` records call order."""
    import discord

    log = []
    bot_user = types.SimpleNamespace(id=999)

    async def channel_send(**kw):
        log.append(("send", kw))
        if fail_send:
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason="x"), "nope")

    async def reply(text, **kw):
        log.append(("reply", text))

    async def delete():
        log.append(("delete", None))

    async def dm(text):
        log.append(("dm", text))

    msg = types.SimpleNamespace(
        author=types.SimpleNamespace(
            id=author_id, bot=False, display_name="Runaans", send=dm,
            display_avatar=types.SimpleNamespace(url="https://cdn/x.png")),
        guild=types.SimpleNamespace(name="Runaans Locks", roles=roles or []),
        mentions=[bot_user] if mention_bot else [],
        attachments=[FakeAttachment()] if attachments is None else attachments,
        content=content,
        channel=types.SimpleNamespace(send=channel_send),
        reply=reply,
        delete=delete,
    )
    return msg, bot_user, log


PREMIUM = types.SimpleNamespace(id=555, name="Premium", mention="<@&555>")


class TestBetListener(unittest.IsolatedAsyncioTestCase):
    def cog(self, bot_user):
        from cogs.bets import BetsCog

        return BetsCog(types.SimpleNamespace(user=bot_user))

    async def test_posts_pings_premium_then_deletes_original(self):
        msg, bot_user, log = fake_message(roles=[PREMIUM])
        await self.cog(bot_user).on_message(msg)

        self.assertEqual([name for name, _ in log], ["send", "delete"])
        sent = log[0][1]
        self.assertEqual(sent["content"], "<@&555>")
        self.assertEqual(sent["allowed_mentions"].to_dict()["roles"], [555])
        self.assertEqual([f.filename for f in sent["files"]], ["slip1.png"])
        self.assertEqual(sent["embeds"][0].fields[0].value, "1U")

    async def test_ignores_other_people_and_unmentioned_messages(self):
        for kwargs in ({"author_id": 222}, {"mention_bot": False}):
            msg, bot_user, log = fake_message(roles=[PREMIUM], **kwargs)
            await self.cog(bot_user).on_message(msg)
            self.assertEqual(log, [], kwargs)

    async def test_no_screenshot_asks_for_one_and_keeps_message(self):
        msg, bot_user, log = fake_message(roles=[PREMIUM], attachments=[])
        await self.cog(bot_user).on_message(msg)
        self.assertEqual([name for name, _ in log], ["reply"])

    async def test_missing_role_posts_without_ping_and_dms_owner_once(self):
        msg, bot_user, log = fake_message(roles=[])
        cog = self.cog(bot_user)
        await cog.on_message(msg)
        await cog.on_message(msg)

        sends = [kw for name, kw in log if name == "send"]
        self.assertEqual(len(sends), 2)
        self.assertIsNone(sends[0]["content"])
        self.assertEqual(sends[0]["allowed_mentions"].to_dict().get("roles", []), [])
        self.assertEqual(sum(1 for name, _ in log if name == "dm"), 1)

    async def test_failed_post_keeps_original_message(self):
        msg, bot_user, log = fake_message(roles=[PREMIUM], fail_send=True)
        await self.cog(bot_user).on_message(msg)

        names = [name for name, _ in log]
        self.assertIn("reply", names)
        self.assertNotIn("delete", names)


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
