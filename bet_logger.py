"""Reads bet slip screenshots with Claude and logs them to the Google Sheet.

Rows go into TOKEN PICKS exactly as if typed by hand: Date, sport emoji,
Selection, Stake, Odds - with Win left blank so the play stays pending
until it's graded. Payout/Profit formulas are already pre-filled in every
row below the data, so only A:E (and the note in K) are ever written.
"""

import base64
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from config import EASTERN, SPREADSHEET_ID, SHEET_NAME
import sheets_sync
from helpers import parse_units

log = logging.getLogger("runaans.bet_logger")

MODEL = "claude-opus-5"

# The sheet's Capper dropdown: 🏈 ⚽ 🏀 or "Other"
SPORT_EMOJI = {"football": "\U0001f3c8", "soccer": "⚽", "basketball": "\U0001f3c0"}

SYSTEM = (
    "You read sportsbook bet slip screenshots for a betting tracker. Return "
    "exactly what is on the slip; never guess a market that isn't shown. "
    "The selection should be a short label the way a capper logs it, e.g. "
    "'Kyren Williams Over 53.5 Rush Yds', 'Kittle + Ferguson ATD SGP', "
    "'Spain GK 4+ Saves', 'Chase Brown + Jeanty 30+ Rec Yds Parlay'. For a "
    "parlay, name the legs briefly and end with 'SGP' or 'Parlay'. Use the "
    "boosted odds if a profit boost is shown."
)

# Only one thread writes to the sheet at a time
_write_lock = threading.Lock()
_client = None


class SlipRead(BaseModel):
    is_betslip: bool = Field(description="True if the image shows a sportsbook bet slip or ticket.")
    selection: str = Field(description="Short label for the whole ticket, under 45 characters.")
    odds: str | None = Field(
        description="American odds for the whole ticket, e.g. '+716' or '-115'. Null if not visible."
    )
    sport: Literal["football", "soccer", "basketball", "baseball", "hockey", "other"]
    legs: int = Field(description="Number of legs on the ticket; 1 for a straight bet.")


@dataclass
class LoggedRow:
    row: int
    sport_emoji: str
    selection: str
    stake: float | None
    odds: str | None

    def line(self) -> str:
        bits = [f"{self.sport_emoji} {self.selection}"]
        if self.stake is not None:
            bits.append(f"{self.stake:g}u")
        if self.odds:
            bits.append(self.odds)
        return f"row {self.row} - " + " · ".join(bits)


def available() -> bool:
    """Whether Claude credentials are configured for reading slips."""
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def read_slip(image: bytes, media_type: str) -> SlipRead | None:
    """Ask Claude what's on one slip screenshot. None if it refused."""
    response = _get_client().messages.parse(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.standard_b64encode(image).decode("ascii"),
                    },
                },
                {"type": "text", "text": "Read this bet slip."},
            ],
        }],
        output_format=SlipRead,
    )

    if response.stop_reason == "refusal":
        log.warning("Claude declined to read a slip: %s", response.stop_details)
        return None

    return response.parsed_output


def read_slips(payloads: list[tuple[bytes, str]]) -> list[SlipRead | None]:
    return [read_slip(image, media_type) for image, media_type in payloads]


def pair_units(units: list[str], count: int) -> list[float | None]:
    """Match the units typed in the post to the slips, in order.

    '0.15u 0.1u' with two slips pairs them; a single unit value applies to
    every slip; otherwise the first value is used for all.
    """
    values = [parse_units(u) for u in units]

    if len(values) == count:
        return values
    if values:
        return [values[0]] * count
    return [None] * count


def odds_number(odds: str | None) -> int | str:
    """'+716' -> 716, '-115' -> -115, anything else -> '' (left blank)."""
    m = re.fullmatch(r"\s*([+-]?)\s*(\d{3,5})\s*", odds or "")
    if not m:
        return ""
    return -int(m.group(2)) if m.group(1) == "-" else int(m.group(2))


def sheet_date(now: datetime | None = None) -> str:
    d = now or datetime.now(EASTERN)
    return f"{d.month}/{d.day}/{d.year}"


def build_rows(reads: list[SlipRead | None], stakes: list[float | None], note: str | None,
               today: str) -> list[dict]:
    """One sheet row per readable slip: the A:E values plus the K note."""
    rows = []
    for read, stake in zip(reads, stakes):
        if read is None or not read.is_betslip:
            continue
        emoji = SPORT_EMOJI.get(read.sport, "Other")
        rows.append({
            "a_to_e": [today, emoji, read.selection[:60], stake if stake is not None else "",
                       odds_number(read.odds)],
            "note": note or "",
            "summary": LoggedRow(0, emoji, read.selection[:60], stake, read.odds),
        })
    return rows


def _worksheet():
    return sheets_sync._get_client().open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)


def next_free_row(ws) -> int:
    dates = ws.col_values(1)
    last = max((i + 1 for i, v in enumerate(dates) if str(v).strip()), default=0)
    return last + 1


def log_bets(rows: list[dict]) -> list[LoggedRow]:
    """Append the rows to the sheet. Returns what was written, with row numbers."""
    if not rows:
        return []

    with _write_lock:
        ws = _worksheet()
        start = next_free_row(ws)
        end = start + len(rows) - 1

        # A:E only - F (Win) stays blank and H:I keep their formulas
        ws.update(
            range_name=f"A{start}:E{end}",
            values=[r["a_to_e"] for r in rows],
            value_input_option="USER_ENTERED",
        )
        if any(r["note"] for r in rows):
            ws.update(range_name=f"K{start}:K{end}", values=[[r["note"]] for r in rows])

    logged = []
    for offset, r in enumerate(rows):
        s = r["summary"]
        logged.append(LoggedRow(start + offset, s.sport_emoji, s.selection, s.stake, s.odds))

    log.info("Logged %d bet(s) to the sheet at rows %d-%d", len(rows), start, end)
    return logged


def clear_rows(row_numbers: list[int]) -> None:
    """Undo: blank out what was written, leaving the formulas in H:I alone."""
    if not row_numbers:
        return

    with _write_lock:
        ws = _worksheet()
        ranges = []
        for r in row_numbers:
            ranges += [f"A{r}:E{r}", f"K{r}"]
        ws.batch_clear(ranges)

    log.info("Cleared sheet rows %s", row_numbers)
