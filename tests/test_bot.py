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
        self.assertEqual(embed.title, "Runaans 8/19/2026 Recap")
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

    def test_a_role_ping_is_not_part_of_the_write_up(self):
        from cogs.bets import parse_post

        # The post does its own @Premium ping. One typed into the trigger
        # message is how the room was addressed, and it used to arrive in the
        # card as the quoted headline - a green pill sitting above the note.
        parsed = parse_post("<@&555> <@999> 1u\nCash the +676 FD Play too")
        self.assertEqual(
            parsed,
            {"units": ["1U"], "headline": None, "note": "Cash the +676 FD Play too"},
        )

    def test_everyone_and_here_are_pings_too(self):
        from cogs.bets import parse_post

        self.assertEqual(parse_post("<@999> @everyone 1u lock")["note"], "lock")
        self.assertEqual(parse_post("<@999> @here 1u lock")["note"], "lock")


class TestUnitsWithWords(unittest.TestCase):
    def test_a_colon_after_the_amount_gives_it_words(self):
        from cogs.bets import parse_post

        parsed = parse_post(
            "<@999> <@&555>\n"
            "0.2u: 3/2/3 RR Combo\n"
            "0.1U : on EACH RR combo\n"
            "Yes Herbert has been bad but there is value in his lines"
        )
        self.assertEqual(parsed["units"], ["0.2U 3/2/3 RR Combo", "0.1U on EACH RR combo"])
        self.assertIsNone(parsed["headline"])
        self.assertEqual(parsed["note"], "Yes Herbert has been bad but there is value in his lines")

    def test_several_on_one_line(self):
        from cogs.bets import parse_post

        parsed = parse_post(
            "<@999> 0.2u: the 3/2/3 RR Combo, 0.1u: the 4/3/4 RR, .05u: the 4/3/4 Parlay"
        )
        self.assertEqual(parsed["units"], [
            "0.2U the 3/2/3 RR Combo",
            "0.1U the 4/3/4 RR",
            "0.05U the 4/3/4 Parlay",
        ])
        self.assertIsNone(parsed["note"])

    def test_colons_between_units_separate_them(self):
        from cogs.bets import parse_post

        parsed = parse_post(
            "<@999> 0.2u on the 3/2/3 Combo: 0.1u RR on the 4/3/4: 0.05u Parlay on the 4/3/4"
        )
        self.assertEqual(parsed["units"], [
            "0.2U on the 3/2/3 Combo",
            "0.1U RR on the 4/3/4",
            "0.05U Parlay on the 4/3/4",
        ])
        self.assertIsNone(parsed["note"])

    def test_one_unit_to_a_line_needs_no_colon_on_the_last(self):
        from cogs.bets import parse_post

        parsed = parse_post(
            "<@999> 0.2u on the 3/2/3 Combo:\n0.1u RR on the 4/3/4:\n.05u Parlay on the 4/3/4\nwrite-up"
        )
        self.assertEqual(parsed["units"], [
            "0.2U on the 3/2/3 Combo",
            "0.1U RR on the 4/3/4",
            "0.05U Parlay on the 4/3/4",
        ])
        self.assertEqual(parsed["note"], "write-up")

    def test_without_a_colon_an_amount_is_just_an_amount(self):
        from cogs.bets import parse_post

        parsed = parse_post("<@999> 0.25u on the parlay\nlove it")
        self.assertEqual(parsed["units"], ["0.25U"])
        self.assertEqual(parsed["headline"], "on the parlay")
        self.assertEqual(parsed["note"], "love it")

    def test_plain_amounts_still_read_as_before(self):
        from cogs.bets import parse_post

        parsed = parse_post("<@999> 1.15u 1.06u .25u\nlock it in")
        self.assertEqual(parsed["units"], ["1.15U", "1.06U", "0.25U"])
        self.assertEqual(parsed["note"], "lock it in")


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


GAMBLY_ID = config.GAMBLY_BOT_ID
GAMBLY_URL = "https://gambly.com/betslip/k3Jd9"


def fake_gambly_reply(original_id, log, url=GAMBLY_URL, label="Place this bet with Gambly",
                      description=None, content=None, reply_to="original"):
    """Gambly's answer as fetched over REST.

    With `url` it carries the link button; with url=None and a `description`
    it's a placeholder or the "couldn't match" message. `reply_to` is the
    message it replies to: the original by default, None for an answer posted
    in the channel without a reply, or another id."""

    async def delete():
        log.append(("delete_gambly", None))

    buttons = [types.SimpleNamespace(label=label, url=url)] if url else []
    embeds = [types.SimpleNamespace(description=description)] if description else []
    target = original_id if reply_to == "original" else reply_to

    return types.SimpleNamespace(
        id=777,
        author=types.SimpleNamespace(id=GAMBLY_ID, bot=True),
        channel=types.SimpleNamespace(id=1),
        reference=None if target is None else types.SimpleNamespace(message_id=target),
        components=[types.SimpleNamespace(children=buttons)] if buttons else [],
        embeds=embeds,
        content=content,
        delete=delete,
    )


GAMBLY_NO_MATCH = "Sorry, we couldn't find those selections at any sportsbook right now."


def fake_message(*, author_id=111, mention_bot=True, attachments=None, roles=None,
                 content="<@999> 1u lock", fail_send=False, mention_gambly=False,
                 gambly=None):
    """A message as the bets listener sees it, plus a fake bot. `log` records
    call order. `gambly` is Gambly's answer, or None for a timeout."""
    import asyncio
    import discord

    log = []
    bot_user = types.SimpleNamespace(id=999)

    # Gambly's answer records its deletion into this same log. A list means
    # successive states of the answer: what wait_for sees, then each re-fetch.
    if callable(gambly):
        gambly = gambly(log)
    states = list(gambly) if isinstance(gambly, list) else [gambly]

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
    if mention_gambly:
        mentions.append(types.SimpleNamespace(id=GAMBLY_ID))

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
        if not check(states[0]):
            log.append(("rejected", None))
            raise asyncio.TimeoutError
        return states[0]

    bot = types.SimpleNamespace(user=bot_user, wait_for=wait_for)
    return msg, bot, log


PREMIUM = types.SimpleNamespace(id=555, name="Premium", mention="<@&555>")


class TestBetListener(unittest.IsolatedAsyncioTestCase):
    def cog(self, bot):
        from cogs.bets import BetsCog

        return BetsCog(bot)

    async def test_no_gambly_tag_means_no_waiting(self):
        msg, bot, log = fake_message(roles=[PREMIUM])
        await self.cog(bot).on_message(msg)
        self.assertNotIn("wait", [name for name, _ in log])
        self.assertNotIn("view", log[0][1])

    async def test_gambly_link_becomes_button_and_its_card_is_deleted(self):
        msg, bot, log = fake_message(
            roles=[PREMIUM], mention_gambly=True,
            content="<@999> <@%d> Draftkings 1u lock" % GAMBLY_ID,
            gambly=lambda log: fake_gambly_reply(123, log),
        )
        await self.cog(bot).on_message(msg)

        names = [name for name, _ in log]
        self.assertEqual(names, ["wait", "fetch", "send", "delete", "delete_gambly"])
        sent = log[2][1]
        button = sent["view"].children[0]
        self.assertEqual(button.label, "Place this bet with Gambly")
        self.assertEqual(button.url, GAMBLY_URL)
        # "Draftkings" was for Gambly, not the note
        self.assertEqual(sent["embeds"][0].description, "lock")

    async def test_every_gambly_button_is_kept_one_per_bet(self):
        """If Gambly answers a multi-slip post with a button per bet, each one
        survives onto the clean post, numbered - not only the first."""
        def reply(log):
            r = fake_gambly_reply(123, log)
            r.components = [types.SimpleNamespace(children=[
                types.SimpleNamespace(label="Bet 1", url="https://gambly.com/share-bet/a"),
                types.SimpleNamespace(label="Bet 2", url="https://gambly.com/share-bet/b"),
                types.SimpleNamespace(label="Gambly", url="https://gambly.com/discord/get-gambly-bot"),
            ])]
            return r

        msg, bot, log = fake_message(roles=[PREMIUM], mention_gambly=True, gambly=reply)
        await self.cog(bot).on_message(msg)

        buttons = next(kw for name, kw in log if name == "send")["view"].children
        self.assertEqual([b.url for b in buttons],
                         ["https://gambly.com/share-bet/a", "https://gambly.com/share-bet/b"])
        self.assertEqual([b.label for b in buttons],
                         ["Place this bet with Gambly \u2013 Bet 1", "Place this bet with Gambly \u2013 Bet 2"])

    async def test_gambly_answering_in_the_channel_without_a_reply_counts(self):
        msg, bot, log = fake_message(
            roles=[PREMIUM], mention_gambly=True,
            gambly=lambda log: fake_gambly_reply(123, log, reply_to=None),
        )
        await self.cog(bot).on_message(msg)

        sent = next(kw for name, kw in log if name == "send")
        self.assertEqual(sent["view"].children[0].url, GAMBLY_URL)
        self.assertIn("delete_gambly", [name for name, _ in log])

    async def test_a_gambly_reply_to_someone_else_is_not_taken(self):
        msg, bot, log = fake_message(
            roles=[PREMIUM], mention_gambly=True,
            gambly=lambda log: fake_gambly_reply(123, log, reply_to=456),
        )
        await self.cog(bot).on_message(msg)

        names = [name for name, _ in log]
        self.assertIn("rejected", names)
        self.assertNotIn("delete_gambly", names)  # someone else's card stays
        self.assertNotIn("view", next(kw for name, kw in log if name == "send"))

    async def test_gambly_timeout_posts_without_button(self):
        msg, bot, log = fake_message(roles=[PREMIUM], mention_gambly=True, gambly=None)
        await self.cog(bot).on_message(msg)

        names = [name for name, _ in log]
        self.assertEqual(names, ["wait", "send", "delete"])
        self.assertNotIn("view", log[1][1])

    async def test_gambly_placeholder_is_polled_until_link_appears(self):
        """Gambly may answer with a placeholder and edit the link in later."""
        from cogs import bets

        def states(log):
            return [
                fake_gambly_reply(123, log, url=None, description="Reading your slip..."),
                fake_gambly_reply(123, log, url=None, description="Reading your slip..."),
                fake_gambly_reply(123, log),  # link edited in
            ]

        msg, bot, log = fake_message(roles=[PREMIUM], mention_gambly=True, gambly=states)
        original_poll = bets.GAMBLY_POLL_SECONDS
        bets.GAMBLY_POLL_SECONDS = 0
        try:
            await self.cog(bot).on_message(msg)
        finally:
            bets.GAMBLY_POLL_SECONDS = original_poll

        names = [name for name, _ in log]
        self.assertEqual(names.count("fetch"), 2)  # kept re-fetching until the link showed
        self.assertEqual(names[-3:], ["send", "delete", "delete_gambly"])
        sent = next(kw for name, kw in log if name == "send")
        self.assertEqual(sent["view"].children[0].url, GAMBLY_URL)

    async def test_gambly_giving_up_stops_polling_and_posts_without_button(self):
        from cogs import bets

        def states(log):
            return [
                fake_gambly_reply(123, log, url=None, description="Reading your slip..."),
                fake_gambly_reply(123, log, url=None, description=GAMBLY_NO_MATCH),
            ]

        msg, bot, log = fake_message(roles=[PREMIUM], mention_gambly=True, gambly=states)
        original_poll = bets.GAMBLY_POLL_SECONDS
        bets.GAMBLY_POLL_SECONDS = 0
        try:
            await self.cog(bot).on_message(msg)
        finally:
            bets.GAMBLY_POLL_SECONDS = original_poll

        names = [name for name, _ in log]
        self.assertEqual(names.count("fetch"), 1)  # gave up as soon as Gambly did
        sent = next(kw for name, kw in log if name == "send")
        self.assertNotIn("view", sent)
        self.assertIn("delete_gambly", names)  # its "couldn't match" card is cleaned up too

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

    async def test_a_reply_ping_is_not_a_trigger(self):
        # Replying to one of the bot's own posts with the ping left on puts the
        # bot in message.mentions without @RunaansLocks having been typed.
        msg, bot, log = fake_message(roles=[PREMIUM], content="1u another slip")
        await self.cog(bot).on_message(msg)
        self.assertEqual(log, [])

    async def test_posts_when_the_text_never_arrived(self):
        # Without the message-content intent an empty content is text the bot
        # was never given, not a message that failed to name it.
        msg, bot, log = fake_message(roles=[PREMIUM], content="")
        await self.cog(bot).on_message(msg)
        self.assertEqual([name for name, _ in log], ["send", "delete"])

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


class TestGamblyParsing(unittest.TestCase):
    def test_book_word_after_gambly_is_dropped(self):
        from cogs.bets import parse_post

        for text in (
            "<@999> <@%d> Draftkings 1u back from vacation" % GAMBLY_ID,
            "<@999> 1u back from vacation <@!%d> dk" % GAMBLY_ID,
            "<@999> 1u back from vacation <@%d>" % GAMBLY_ID,
        ):
            parsed = parse_post(text, GAMBLY_ID)
            self.assertEqual(parsed["note"], "back from vacation", text)
            self.assertEqual(parsed["units"], ["1U"])

    def test_book_words_elsewhere_are_kept(self):
        from cogs.bets import parse_post

        parsed = parse_post("<@999> 1u bet365 is giving a 100%% boost <@%d> bet365" % GAMBLY_ID, GAMBLY_ID)
        self.assertEqual(parsed["note"], "bet365 is giving a 100% boost")

    def test_extract_betslip_prefers_a_gambly_button_then_text(self):
        from cogs.bets import extract_betslip

        self.assertEqual(extract_betslip(fake_gambly_reply(1, [])),
                         {"url": GAMBLY_URL, "label": "Place this bet with Gambly"})

        embed = types.SimpleNamespace(
            description="Best price at FanDuel. [Open in Gambly](https://app.gambly.com/slip/zQMgh)")
        text_only = types.SimpleNamespace(components=[], embeds=[embed], content=None)
        self.assertEqual(extract_betslip(text_only)["url"], "https://app.gambly.com/slip/zQMgh")

        self.assertIsNone(extract_betslip(types.SimpleNamespace(components=[], embeds=[])))

    def test_gamblys_open_in_a_book_button_is_not_a_second_bet(self):
        from cogs.bets import extract_betslips

        reply = types.SimpleNamespace(components=[types.SimpleNamespace(children=[
            types.SimpleNamespace(label="View Your One-Click Betslip",
                                  url="https://gambly.com/share-bet/AZUn?id=1"),
            types.SimpleNamespace(label="Open in DraftKings",
                                  url="https://gambly.com/deeplink/deeplink-cache-v2-x"),
        ])], embeds=[], content=None)
        self.assertEqual(extract_betslips(reply), [
            {"url": "https://gambly.com/share-bet/AZUn?id=1", "label": "Place this bet with Gambly"},
        ])

    def test_a_sportsbook_link_or_the_install_page_is_not_a_betslip(self):
        from cogs.bets import extract_betslip

        sportsbook = fake_gambly_reply(1, [], url="https://sportsbook.fanduel.com/addToBetslip?x=1")
        self.assertIsNone(extract_betslip(sportsbook))

        install = fake_gambly_reply(1, [], url=None, content="Add me: https://gambly.com/discord/get-gambly-bot")
        self.assertIsNone(extract_betslip(install))

    def test_couldnt_match_message_is_giving_up(self):
        from cogs.bets import extract_betslip, gambly_gave_up

        reply = fake_gambly_reply(1, [], url=None, description=GAMBLY_NO_MATCH)
        self.assertIsNone(extract_betslip(reply))
        self.assertTrue(gambly_gave_up(reply))

        placeholder = fake_gambly_reply(1, [], url=None, description="Reading your slip...")
        self.assertIsNone(gambly_gave_up(placeholder))


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
