"""Value normalisers for messy lead and event data.

Every function here is pure and total: it returns a normalised value or
``None``. Nothing raises on bad input, because "bad input" is the normal case.
These are the rules most likely to break, so they live in one small module with
a dedicated test file.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from solution.ledger.config import IST

# --------------------------------------------------------------------------
# Headers
# --------------------------------------------------------------------------

_WS_OR_UNDERSCORE = re.compile(r"[_\s]+")


def normalize_header(header: str) -> str:
    """Lower-case, trim, collapse every run of ``_``/whitespace to one space."""
    return _WS_OR_UNDERSCORE.sub(" ", header.strip().lower()).strip()


# --------------------------------------------------------------------------
# Phone
# --------------------------------------------------------------------------

_NON_DIGITS = re.compile(r"\D")


def normalize_phone(raw: str | None) -> str | None:
    """Canonicalise an Indian mobile number to ``+91XXXXXXXXXX``.

    Strip every non-digit; drop a leading ``91`` from 12 digits or a leading
    ``0`` from 11 digits; the result is valid only as exactly 10 digits starting
    with 6, 7, 8 or 9.
    """
    if raw is None:
        return None
    digits = _NON_DIGITS.sub("", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


def phone_last4(canonical_phone: str) -> str:
    return canonical_phone[-4:]


def mask_phone(canonical_phone_last4: str) -> str:
    """``+91******1234`` - the only phone representation any report may show."""
    return "+91******" + canonical_phone_last4


# --------------------------------------------------------------------------
# Date
# --------------------------------------------------------------------------

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_RE_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_RE_DMY_SLASH = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_RE_DMY_DASH = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$")
_RE_D_MON_Y = re.compile(r"^(\d{1,2})-([A-Za-z]{3,9})-(\d{4})$")


def normalize_date(raw: str | None) -> str | None:
    """Parse a day-first date into ``YYYY-MM-DD``; impossible dates are invalid.

    Accepts ``YYYY-MM-DD``, ``DD/MM/YYYY``, ``DD-MM-YYYY`` and ``DD-Mon-YYYY``.
    Parsed by explicit pattern rather than ``strptime`` so that month names are
    locale-independent and ``31/02`` fails instead of silently rolling over.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None

    for pattern, order in ((_RE_ISO, "ymd"), (_RE_DMY_SLASH, "dmy"), (_RE_DMY_DASH, "dmy")):
        m = pattern.match(text)
        if m:
            a, b, c = (int(x) for x in m.groups())
            y, mo, d = (a, b, c) if order == "ymd" else (c, b, a)
            return _build_date(y, mo, d)

    m = _RE_D_MON_Y.match(text)
    if m:
        day_s, month_s, year_s = m.groups()
        month = _MONTHS.get(month_s[:3].lower())
        if month is None:
            return None
        return _build_date(int(year_s), month, int(day_s))

    return None


def _build_date(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Decimal
# --------------------------------------------------------------------------

_RE_MONEY = re.compile(
    r"^(?:\u20b9|rs\.?|inr)?\s*(\d{1,3}(?:,\d{2,3})*(?:\.\d+)?|\d+(?:\.\d+)?)$",
    re.IGNORECASE,
)
_CENTS = Decimal("0.01")


def normalize_decimal(raw: str | None) -> str | None:
    """Parse an optionally rupee-prefixed amount; must be >= 0, rounded to 2dp.

    Returned as a string so the exact decimal survives the trip through JSON and
    SQLite without becoming a float. Indian grouping (``1,23,456``) is accepted
    alongside ``1,234``.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    m = _RE_MONEY.match(text)
    if not m:
        return None
    try:
        value = Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    if value < 0:  # unreachable via the regex, kept as an explicit guard
        return None
    return str(value.quantize(_CENTS, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------
# Enum / string
# --------------------------------------------------------------------------

def normalize_enum(raw: str | None, values: list[str]) -> str | None:
    """Case-insensitive match against ``values``; stored lower-case."""
    if raw is None:
        return None
    text = raw.strip().lower()
    if not text:
        return None
    allowed = {v.strip().lower() for v in values}
    return text if text in allowed else None


def normalize_string(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = raw.strip()
    return text or None


# --------------------------------------------------------------------------
# Timestamps
# --------------------------------------------------------------------------

def parse_timestamp(raw: str | None) -> datetime | None:
    """Parse UTC (``...Z``), offset (``...+05:30``) and naive forms.

    Naive means IST, per the spec. The result is always timezone-aware.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed


def utc_key(moment: datetime) -> str:
    """``YYYY-MM-DDTHH:MM:SSZ`` - lexicographic order equals chronological order.

    Stored as the ledger's ordering key so SQLite can sort events with a plain
    ``ORDER BY`` on an indexed TEXT column.
    """
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ist_iso(moment: datetime) -> str:
    """``2026-10-14T17:55:24+05:30`` - the report-facing representation."""
    return moment.astimezone(IST).isoformat()


def ist_date(moment: datetime) -> str:
    """The IST calendar date; every date in every report is an IST date."""
    return moment.astimezone(IST).strftime("%Y-%m-%d")
