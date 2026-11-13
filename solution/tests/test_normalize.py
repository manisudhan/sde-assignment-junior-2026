"""Phone, date, money, enum and timestamp rules - the ones most likely to break."""
from __future__ import annotations

import pytest

from solution.ledger.normalize import (
    ist_date,
    ist_iso,
    mask_phone,
    normalize_date,
    normalize_decimal,
    normalize_enum,
    normalize_header,
    normalize_phone,
    parse_timestamp,
    phone_last4,
    utc_key,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        # the six shapes the upload data actually arrives in
        ("9876543210", "+919876543210"),
        ("+91 98765 43210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("91-98765-43210", "+919876543210"),
        ("(+91) 9876543210", "+919876543210"),
        ("+919876543210", "+919876543210"),
        # all four valid leading digits
        ("6876543210", "+916876543210"),
        ("7876543210", "+917876543210"),
        ("8876543210", "+918876543210"),
        # and the ways it goes wrong
        ("987654321", None),             # nine digits
        ("5876543210", None),            # leading 5 is not a mobile
        ("022-2000-1234", None),         # landline
        ("N/A", None),
        ("", None),
        ("+1 415 555 0134", None),       # eleven digits, does not start with 0
        ("0919876543210", None),         # thirteen digits: no rule applies
        ("919876543210987", None),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


def test_phone_masking_shows_only_the_last_four():
    canonical = normalize_phone("+91 98765 43210")
    assert phone_last4(canonical) == "3210"
    assert mask_phone(phone_last4(canonical)) == "+91******3210"
    assert len(mask_phone("3210")) == len("+91") + 6 + 4


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-10-05", "2026-10-05"),
        ("05/11/2026", "2026-11-05"),     # day-first, not month-first
        ("11-12-2026", "2026-12-11"),     # day-first, not ISO
        ("01-Dec-2026", "2026-12-01"),
        ("01-DEC-2026", "2026-12-01"),    # month name is case-insensitive
        ("1-dec-2026", "2026-12-01"),
        ("29/02/2024", "2024-02-29"),     # a real leap day
        ("31/02/2026", None),             # impossible, not rolled over
        ("29/02/2026", None),             # not a leap year
        ("2026-13-01", None),
        ("2026-02-30", None),
        ("soon", None),
        ("TBD", None),
        ("", None),
        ("05-Foo-2026", None),
    ],
)
def test_normalize_date(raw, expected):
    assert normalize_date(raw) == expected


def test_day_first_is_unambiguous_for_dates_that_could_be_either():
    # 03/04/2026 is 3 April, never 4 March.
    assert normalize_date("03/04/2026") == "2026-04-03"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("12000", "12000.00"),
        ("12000.5", "12000.50"),
        ("12,000", "12000.00"),
        ("₹27,554", "27554.00"),
        ("Rs. 24787", "24787.00"),
        ("Rs 24787", "24787.00"),
        ("rs.24787", "24787.00"),
        ("INR 18678.3", "18678.30"),
        ("inr 18678.3", "18678.30"),
        ("1,23,456.78", "123456.78"),     # Indian grouping
        ("0", "0.00"),
        ("12000.455", "12000.46"),        # half-up at the cent
        ("N/A", None),
        ("-", None),
        ("twelve thousand", None),
        ("-500", None),                   # must be >= 0
        ("", None),
        ("12.00.00", None),
    ],
)
def test_normalize_decimal(raw, expected):
    assert normalize_decimal(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("en", "en"), (" hi ", "hi"), ("HI", "hi"), ("Ta", "ta"),
     ("Hindi", None), ("english", None), ("", None)],
)
def test_normalize_enum(raw, expected):
    assert normalize_enum(raw, ["en", "hi", "ta", "kn", "te", "mr"]) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("customer_name", "customer name"),
        ("  Mobile_No  ", "mobile no"),
        ("Policy   No", "policy no"),
        ("insured__name", "insured name"),
        ("Order ID", "order id"),
        ("_phone_", "phone"),
        ("Premium\tAmount", "premium amount"),
    ],
)
def test_normalize_header(raw, expected):
    assert normalize_header(raw) == expected


def test_timestamps_from_all_three_forms_land_on_the_same_instant():
    utc = parse_timestamp("2026-10-03T04:31:00Z")
    offset = parse_timestamp("2026-10-03T10:01:00+05:30")
    naive = parse_timestamp("2026-10-03 10:01:00")  # naive means IST
    assert utc == offset == naive
    assert utc_key(utc) == "2026-10-03T04:31:00Z"
    assert ist_iso(utc) == "2026-10-03T10:01:00+05:30"
    assert ist_date(utc) == "2026-10-03"


def test_report_dates_are_ist_dates_not_utc_dates():
    # 20:00 UTC is already the next day in IST; reports must say the IST date.
    late = parse_timestamp("2026-10-03T20:00:00Z")
    assert ist_date(late) == "2026-10-04"
    assert ist_iso(late) == "2026-10-04T01:30:00+05:30"
    # and the mirror case: early IST is still the previous UTC day
    early = parse_timestamp("2026-10-04 02:00:00")
    assert utc_key(early) == "2026-10-03T20:30:00Z"
    assert ist_date(early) == "2026-10-04"


@pytest.mark.parametrize("raw", ["", "not-a-time", None, 42, "2026-99-99T00:00:00Z"])
def test_unparseable_timestamps_return_none_instead_of_raising(raw):
    assert parse_timestamp(raw) is None
