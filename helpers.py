from datetime import datetime

from discord import app_commands

from config import OWNER_ID


class OwnerOnly(app_commands.CheckFailure):
    pass


def owner_only():
    def predicate(interaction) -> bool:
        if interaction.user.id != OWNER_ID:
            raise OwnerOnly()
        return True
    return app_commands.check(predicate)


# Accepted input formats for dates (sheet cells and /recap input)
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y")


def normalize_date(date_str) -> str | None:
    """Parse a date in any accepted format and return it as YYYY-MM-DD."""
    date_str = str(date_str or "").strip()

    if not date_str:
        return None

    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    return None


def parse_units(val) -> float | None:
    """Parse a sheet/db value like '1.5u', '2,000', '(1.5)' into a float."""
    val = str(val if val is not None else "").strip()
    val = val.replace(",", "").replace("u", "").replace("U", "").strip()

    if not val:
        return None

    # Accounting-style negatives: (1.5) -> -1.5
    if val.startswith("(") and val.endswith(")"):
        val = "-" + val[1:-1]

    try:
        return float(val)
    except ValueError:
        return None
