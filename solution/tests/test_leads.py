"""Lead ingestion: header mapping, row numbering, rejection, merging, defaults."""
from __future__ import annotations

import json

from solution.tests.conftest import MOTOR_HEADER


def row(name="Aarav Menon", phone="9876543210", policy="MOT/1", expiry="2026-11-21",
        premium="12000", language="en") -> list[str]:
    return [name, phone, policy, expiry, premium, language]


# ---------------------------------------------------------------------------
# Header matching
# ---------------------------------------------------------------------------

def test_aliased_reordered_and_extra_columns_all_map_correctly(harness):
    """batch_003 renames every column, reorders them and adds an extra one."""
    harness.write_leads(
        "drop_1", "motor_renewal", "batch_003",
        ["Language", "Insured Name", "Policy No", "Mobile No", "Expiry Date", "Premium", "agent_notes"],
        [["HI", "Ananya Rao", "MOT/2026/100402", "(+91) 9240580085", "05/11/2026", "INR 18678.3", "VIP"]],
    )
    harness.ingest("drop_1")
    lead = harness.lead_row("MOT/2026/100402")
    assert lead["phone_masked"] == "+91******0085"
    assert lead["batch_id"] == "batch_003"
    attributes = json.loads(harness.query("SELECT attributes FROM leads")[0]["attributes"])
    assert attributes["policy_expiry"] == "2026-11-05"
    assert attributes["premium_amount"] == "18678.30"
    assert attributes["preferred_language"] == "hi"  # stored lower-case


def test_unmatched_columns_are_ignored_not_rejected(harness):
    harness.write_leads(
        "drop_1", "motor_renewal", "batch_001",
        MOTOR_HEADER + ["agent_notes", "internal_score"],
        [row() + ["called before", "77"]],
    )
    harness.ingest("drop_1")
    assert harness.quality()["lead_rows_rejected"] == 0
    assert "agent_notes" not in json.loads(harness.query("SELECT attributes FROM leads")[0]["attributes"])


def test_a_missing_optional_column_is_simply_missing(harness):
    harness.write_leads(
        "drop_1", "motor_renewal", "batch_001",
        ["customer_name", "phone", "policy_number"],
        [["Aarav Menon", "9876543210", "MOT/1"]],
    )
    harness.ingest("drop_1")
    attributes = json.loads(harness.query("SELECT attributes FROM leads")[0]["attributes"])
    assert attributes["policy_expiry"] is None
    assert attributes["preferred_language"] == "en"  # the default still applies


# ---------------------------------------------------------------------------
# Blank rows and row numbering
# ---------------------------------------------------------------------------

def test_blank_rows_are_skipped_and_never_counted(harness):
    harness.write_leads(
        "drop_1", "motor_renewal", "batch_001", MOTOR_HEADER,
        [
            row(policy="MOT/1"),
            ["", "", "", "", "", ""],      # all empty
            ["  ", "\t", "", " ", "", ""],  # all whitespace
            row(phone="9876543211", policy="MOT/2"),
        ],
    )
    harness.ingest("drop_1")
    quality = harness.quality()
    assert quality["lead_rows_total"] == 2
    assert quality["lead_rows_rejected"] == 0


def test_row_numbers_count_non_blank_data_rows_from_one(harness):
    """The header is not a row, and blank rows do not consume a number."""
    harness.write_leads(
        "drop_1", "motor_renewal", "batch_001", MOTOR_HEADER,
        [
            row(policy="MOT/1"),                       # row 1, valid
            ["", "", "", "", "", ""],                  # blank, not numbered
            row(name="", phone="9876543212", policy="MOT/3"),  # row 2, rejected
            row(phone="bad", policy="MOT/4"),          # row 3, rejected
        ],
    )
    harness.ingest("drop_1")
    rejections = harness.reports()["rejections"]
    assert [(r["row_number"], r["reason"]) for r in rejections] == [
        ("2", "missing_required:customer_name"),
        ("3", "invalid_phone"),
    ]


# ---------------------------------------------------------------------------
# Rejection
# ---------------------------------------------------------------------------

def test_first_failure_in_schema_order_is_the_single_reason(harness):
    """A row missing its name *and* carrying a bad phone is a name rejection:
    customer_name comes before phone in the schema."""
    harness.write_leads(
        "drop_1", "motor_renewal", "batch_001", MOTOR_HEADER,
        [["   ", "not-a-phone", "", "soon", "N/A", "Klingon"]],
    )
    harness.ingest("drop_1")
    rejections = harness.reports()["rejections"]
    assert len(rejections) == 1
    assert rejections[0]["reason"] == "missing_required:customer_name"


def test_each_required_field_produces_its_own_reason(harness):
    harness.write_leads(
        "drop_1", "motor_renewal", "batch_001", MOTOR_HEADER,
        [
            row(name="  ", phone="9876543210", policy="MOT/1"),
            row(name="Ok", phone="", policy="MOT/2"),
            row(name="Ok", phone="9876543212", policy="  "),
            row(name="Ok", phone="022-2000-1234", policy="MOT/4"),
        ],
    )
    harness.ingest("drop_1")
    assert harness.quality()["rejections_by_reason"] == {
        "invalid_phone": 1,
        "missing_required:customer_name": 1,
        "missing_required:phone": 1,
        "missing_required:policy_number": 1,
    }


def test_rejected_rows_are_not_leads_and_carry_no_pii(harness):
    harness.write_leads(
        "drop_1", "motor_renewal", "batch_001", MOTOR_HEADER,
        [row(name="Rejected Person", phone="5876543210", policy="MOT/9")],
    )
    harness.ingest("drop_1")
    assert harness.query("SELECT COUNT(*) AS n FROM leads")[0]["n"] == 0
    rejection = harness.reports()["rejections"][0]
    assert set(rejection) == {"workflow_id", "batch_id", "row_number", "reason"}
    assert "Rejected Person" not in str(rejection)


def test_an_empty_value_counts_as_missing_not_as_a_warning(harness):
    harness.write_leads(
        "drop_1", "motor_renewal", "batch_001", MOTOR_HEADER,
        [row(expiry="   ", premium="", language="")],
    )
    harness.ingest("drop_1")
    assert harness.quality()["field_warnings"] == 0


# ---------------------------------------------------------------------------
# Field warnings
# ---------------------------------------------------------------------------

def test_invalid_optional_fields_become_null_and_warn_once_each(harness):
    harness.write_leads(
        "drop_1", "motor_renewal", "batch_001", MOTOR_HEADER,
        [row(expiry="31/02/2026", premium="twelve thousand", language="Hindi")],
    )
    harness.ingest("drop_1")
    assert harness.quality()["field_warnings"] == 3
    attributes = json.loads(harness.query("SELECT attributes FROM leads")[0]["attributes"])
    assert attributes["policy_expiry"] is None
    assert attributes["premium_amount"] is None
    assert attributes["preferred_language"] == "en"  # invalid -> null -> default


def test_warnings_are_not_counted_for_rejected_rows(harness):
    """This row has three invalid optional fields, but it is rejected, so none
    of them count."""
    harness.write_leads(
        "drop_1", "motor_renewal", "batch_001", MOTOR_HEADER,
        [row(name="", expiry="soon", premium="N/A", language="Klingon")],
    )
    harness.ingest("drop_1")
    quality = harness.quality()
    assert quality["lead_rows_rejected"] == 1
    assert quality["field_warnings"] == 0


# ---------------------------------------------------------------------------
# Identity, duplicates and merging
# ---------------------------------------------------------------------------

def test_identity_is_workflow_plus_canonical_phone(harness):
    """The same number in two formats is one lead; the same number in two
    workflows is two."""
    harness.write_leads(
        "drop_1", "motor_renewal", "batch_001", MOTOR_HEADER,
        [row(phone="9876543210", policy="MOT/1"), row(phone="+91 98765 43210", policy="MOT/1")],
    )
    harness.write_leads(
        "drop_1", "policy_feedback", "batch_001",
        ["customer_name", "phone", "order_id"],
        [["Aarav Menon", "09876543210", "ORD-1"]],
    )
    harness.ingest("drop_1")
    assert harness.query("SELECT COUNT(*) AS n FROM leads")[0]["n"] == 2
    assert harness.quality()["duplicate_lead_rows_merged"] == 1


def test_first_valid_row_fixes_the_batch_permanently(harness):
    harness.write_leads("drop_1", "motor_renewal", "batch_001", MOTOR_HEADER,
                        [row(policy="MOT/1")])
    harness.write_leads("drop_1", "motor_renewal", "batch_002", MOTOR_HEADER,
                        [row(policy="MOT/1")])
    harness.ingest("drop_1")
    harness.write_leads("drop_2", "motor_renewal", "batch_003", MOTOR_HEADER,
                        [row(policy="MOT/1")])
    harness.ingest("drop_2")
    assert harness.lead_row("MOT/1")["batch_id"] == "batch_001"
    assert harness.quality()["duplicate_lead_rows_merged"] == 2


def test_a_rejected_row_does_not_claim_the_batch(harness):
    """batch_001's row is rejected, so batch_002's valid row creates the lead
    and owns the batch."""
    harness.write_leads("drop_1", "motor_renewal", "batch_001", MOTOR_HEADER,
                        [row(name="", policy="MOT/1")])
    harness.write_leads("drop_1", "motor_renewal", "batch_002", MOTOR_HEADER,
                        [row(policy="MOT/1")])
    harness.ingest("drop_1")
    assert harness.lead_row("MOT/1")["batch_id"] == "batch_002"


def test_merge_overwrites_only_non_null_fields(harness):
    harness.write_leads(
        "drop_1", "motor_renewal", "batch_001", MOTOR_HEADER,
        [row(name="Original Name", expiry="2026-11-21", premium="12000", language="ta")],
    )
    harness.ingest("drop_1")
    harness.write_leads(
        "drop_2", "motor_renewal", "batch_003", MOTOR_HEADER,
        # new premium, blank expiry, invalid language
        [row(name="Updated Name", expiry="", premium="15500.75", language="Klingon")],
    )
    harness.ingest("drop_2")
    attributes = json.loads(harness.query("SELECT attributes FROM leads")[0]["attributes"])
    assert attributes["premium_amount"] == "15500.75"   # overwritten
    assert attributes["policy_expiry"] == "2026-11-21"  # blank did not erase it
    assert attributes["preferred_language"] == "ta"     # invalid did not erase it


def test_merge_updates_encrypted_pii_too(harness):
    harness.write_leads("drop_1", "motor_renewal", "batch_001", MOTOR_HEADER,
                        [row(name="Original Name")])
    harness.ingest("drop_1")
    harness.write_leads("drop_2", "motor_renewal", "batch_003", MOTOR_HEADER,
                        [row(name="Updated Name")])
    harness.ingest("drop_2")
    blob = harness.query("SELECT pii_enc FROM leads")[0]["pii_enc"]
    assert json.loads(harness.cipher.decrypt(blob))["customer_name"] == "Updated Name"


def test_duplicates_within_one_file_merge_in_row_order(harness):
    harness.write_leads(
        "drop_1", "motor_renewal", "batch_001", MOTOR_HEADER,
        [row(premium="100"), row(premium="200"), row(premium="300")],
    )
    harness.ingest("drop_1")
    attributes = json.loads(harness.query("SELECT attributes FROM leads")[0]["attributes"])
    assert attributes["premium_amount"] == "300.00"  # last write wins
    assert harness.quality()["duplicate_lead_rows_merged"] == 2


def test_lead_rows_total_accounts_for_every_non_blank_row(harness):
    harness.write_leads(
        "drop_1", "motor_renewal", "batch_001", MOTOR_HEADER,
        [row(policy="MOT/1"), row(policy="MOT/1"), row(name="", phone="9876543219")],
    )
    harness.ingest("drop_1")
    quality = harness.quality()
    leads = harness.query("SELECT COUNT(*) AS n FROM leads")[0]["n"]
    assert quality["lead_rows_total"] == (
        leads + quality["lead_rows_rejected"] + quality["duplicate_lead_rows_merged"]
    )
