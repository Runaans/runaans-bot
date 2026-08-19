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
        totals = embed.fields[0].value

        # Title carries the brand and the weekday; 2026-08-19 is a Wednesday
        self.assertEqual(embed.title, "Runaans Locks · Wed 8/19/2026 Recap")
        self.assertEqual(embed.fields[0].name, "📊 Totals")
        self.assertIn("✅ Lakers ML +1.50u", embed.description)
        self.assertIn("❌ Jets +3.5 -1.00u", embed.description)
        self.assertIn("➖ Over 8.5 +0.00u", embed.description)
        self.assertIn("⏳ Parlay", embed.description)
        self.assertIn("8/19/2026: +0.50u", totals)
        self.assertIn("(1-1-1)", totals)
        self.assertIn("1 pending", totals)
        self.assertIn("THIS MONTH: +12.34u", totals)
        self.assertIn("YEARLY: -5.00u", totals)
        self.assertEqual(embed.color, recap.GREEN)

    def test_losing_day_is_red(self):
        embed = recap.build_recap_embed(recap_data([play("A", "L", -2.0)]))
        self.assertEqual(embed.color, recap.RED)

    def test_all_pending_is_purple(self):
        embed = recap.build_recap_embed(recap_data([play("A", None, None)]))
        self.assertEqual(embed.color, recap.PURPLE)
        self.assertIn("(0-0)", embed.fields[0].value)

    def test_many_plays_stay_under_discord_limits(self):
        plays = [
            play(f"Some Very Long Play Title Number {i} With Extra Words", "W", 1.0, i)
            for i in range(300)
        ]
        embed = recap.build_recap_embed(recap_data(plays))

        self.assertLessEqual(len(embed.description), 4096)
        self.assertIn("more plays", embed.description)
        # Totals still count every play, not just the displayed ones
        self.assertIn("+300.00u", embed.fields[0].value)
        self.assertIn("(300-0)", embed.fields[0].value)

        total_len = (
            len(embed.title)
            + len(embed.description)
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

        self.assertLessEqual(len(embed.description), 4096)
        # The monster line is trimmed, not allowed to swallow everything
        self.assertIn("…", embed.description)
        self.assertIn("+6.00u", embed.fields[0].value)


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


class TestPendingEmbed(unittest.TestCase):
    def test_lists_pending_with_details(self):
        plays = [
            {"date": "2026-08-19", "title": "Lakers ML", "odds": "-110",
             "units": 1.5, "capper": "Runaan"},
        ]
        embed = stats_cog.build_pending_embed(plays)
        self.assertIn("⏳ **2026-08-19** Lakers ML", embed.description)
        self.assertIn("-110, 1.50u, Runaan", embed.description)

    def test_empty_state(self):
        embed = stats_cog.build_pending_embed([])
        self.assertIn("Nothing pending", embed.description)

    def test_truncates_long_lists(self):
        plays = [
            {"date": "2026-08-19", "title": f"Play {i}"}
            for i in range(stats_cog.PENDING_LIMIT + 5)
        ]
        embed = stats_cog.build_pending_embed(plays)
        self.assertIn("and more", embed.description)
        self.assertLessEqual(len(embed.description), 4096)


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
            for cog in ("cogs.recap", "cogs.sync", "cogs.stats"):
                await bot.load_extension(cog)

            names = {c.name for c in bot.tree.get_commands(guild=GUILD_ID)}
            self.assertEqual(names, {"recap", "sync", "status", "stats", "pending"})

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
