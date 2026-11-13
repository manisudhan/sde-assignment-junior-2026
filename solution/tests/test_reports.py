"""The output contract: exact columns, ordering, formatting and PII rules.

These files are diffed against expected output, so the shape matters as much as
the numbers.
"""
from __future__ import annotations

import json

from solution.ledger.config import REPORT_FILES
from solution.ledger.report import connectivity_rate
from solution.tests.conftest import MOTOR_HEADER, call_events

EXPECTED_HEADERS = {
    "batch_summary.csv": "workflow_id,batch_id,total_leads,yet_to_reach_out,"
                         "in_progress_telephony_failed,in_progress_dialed,completed",
    "daily_overview.csv": "workflow_id,date,total_calls,connected_calls,connectivity_rate,"
                          "success_leads",
    "lead_report.csv": "workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,"
                       "connected_calls,last_call_at,current_disposition,ever_success",
    "rejections.csv": "workflow_id,batch_id,row_number,reason",
}


def motor(phone, policy, premium="12000") -> list[str]:
    return ["Aarav Menon", phone, policy, "2026-11-21", premium, "en"]


def seed(harness) -> None:
    """Two workflows, three batches, a rejection, and calls across two dates."""
    harness.write_leads("drop_1", "motor_renewal", "batch_001", MOTOR_HEADER, [
        motor("9876543210", "MOT/2"),
        motor("9876543211", "MOT/1"),
        motor("", "MOT/3"),  # rejected: missing phone
    ])
    harness.write_leads("drop_1", "motor_renewal", "batch_002", MOTOR_HEADER,
                        [motor("9876543212", "MOT/4")])
    harness.write_leads("drop_1", "policy_feedback", "batch_001",
                        ["customer_name", "phone", "order_id"],
                        [["Meera Iyer", "9800000001", "ORD-1"]])
    harness.write_events("drop_1", "events_0001.jsonl",
                         call_events("a", "call_a", "2026-10-01", phone="9876543210",
                                     disposition="renewed")
                         + call_events("b", "call_b", "2026-10-01", phone="9876543211",
                                       connected=False)
                         + call_events("c", "call_c", "2026-10-02", phone="9876543212",
                                       telephony_failed=True)
                         + call_events("d", "call_d", "2026-10-02", phone="9800000001",
                                       workflow="policy_feedback",
                                       disposition="feedback_captured"))
    harness.ingest("drop_1")


def test_all_five_files_are_written(harness):
    seed(harness)
    directory = harness.reports()["_dir"]
    assert sorted(p.name for p in directory.iterdir()) == sorted(REPORT_FILES)


def test_headers_match_the_contract_exactly(harness):
    seed(harness)
    directory = harness.reports()["_dir"]
    for name, expected in EXPECTED_HEADERS.items():
        first_line = (directory / name).read_text(encoding="utf-8").splitlines()[0]
        assert first_line == expected, name


def test_csvs_have_no_index_column_and_unix_line_endings(harness):
    seed(harness)
    directory = harness.reports()["_dir"]
    for name in EXPECTED_HEADERS:
        raw = (directory / name).read_bytes()
        assert b"\r\n" not in raw, name
        assert not raw.startswith(b","), name


def test_batch_summary_is_sorted_and_buckets_add_up(harness):
    seed(harness)
    rows = harness.reports()["batch_summary"]
    assert [(r["workflow_id"], r["batch_id"]) for r in rows] == [
        ("motor_renewal", "batch_001"),
        ("motor_renewal", "batch_002"),
        ("policy_feedback", "batch_001"),
    ]
    for row in rows:
        buckets = ("yet_to_reach_out", "in_progress_telephony_failed",
                   "in_progress_dialed", "completed")
        assert int(row["total_leads"]) == sum(int(row[b]) for b in buckets)


def test_daily_overview_has_one_row_per_workflow_per_date_with_a_call(harness):
    seed(harness)
    rows = harness.reports()["daily_overview"]
    assert [(r["workflow_id"], r["date"]) for r in rows] == [
        ("motor_renewal", "2026-10-01"),
        ("motor_renewal", "2026-10-02"),
        ("policy_feedback", "2026-10-02"),
    ]
    first = rows[0]
    assert (first["total_calls"], first["connected_calls"]) == ("2", "1")
    assert first["connectivity_rate"] == "0.5000"
    assert first["success_leads"] == "1"


def test_success_leads_counts_the_date_of_the_successful_call(harness):
    """A lead that succeeded on the 1st must not inflate the 5th, even though
    `ever_success` stays true for the lead."""
    harness.write_leads("drop_1", "motor_renewal", "batch_001", MOTOR_HEADER,
                        [motor("9876543210", "MOT/1")])
    harness.write_events("drop_1", "events_0001.jsonl",
                         call_events("a", "call_a", "2026-10-01", disposition="renewed")
                         + call_events("b", "call_b", "2026-10-05", connected=False))
    harness.ingest("drop_1")
    by_date = {r["date"]: r["success_leads"] for r in harness.reports()["daily_overview"]}
    assert by_date == {"2026-10-01": "1", "2026-10-05": "0"}
    assert harness.lead_row("MOT/1")["ever_success"] == "true"


def test_connectivity_rate_always_has_exactly_four_decimals():
    assert connectivity_rate(9, 16) == "0.5625"   # the example from the spec
    assert connectivity_rate(1, 3) == "0.3333"
    assert connectivity_rate(2, 3) == "0.6667"
    assert connectivity_rate(0, 7) == "0.0000"
    assert connectivity_rate(7, 7) == "1.0000"
    assert connectivity_rate(1, 1) == "1.0000"
    for rate in (connectivity_rate(a, 97) for a in range(0, 98)):
        assert len(rate.split(".")[1]) == 4


def test_lead_report_is_sorted_by_workflow_then_reference(harness):
    seed(harness)
    rows = harness.reports()["lead_report"]
    assert [(r["workflow_id"], r["external_ref"]) for r in rows] == [
        ("motor_renewal", "MOT/1"),
        ("motor_renewal", "MOT/2"),
        ("motor_renewal", "MOT/4"),
        ("policy_feedback", "ORD-1"),
    ]


def test_lead_report_masks_the_phone_and_shows_nothing_else_identifying(harness):
    seed(harness)
    rows = harness.reports()["lead_report"]
    for row in rows:
        assert row["phone_masked"].startswith("+91******")
        assert len(row["phone_masked"]) == 13
        assert "Aarav" not in str(row) and "Meera" not in str(row)
    assert {r["phone_masked"] for r in rows} >= {"+91******3210", "+91******0001"}


def test_booleans_are_lower_case_true_or_false(harness):
    seed(harness)
    values = {r["ever_success"] for r in harness.reports()["lead_report"]}
    assert values <= {"true", "false"}
    assert values == {"true", "false"}  # both appear in this fixture


def test_last_call_at_is_empty_rather_than_null_when_there_are_no_calls(harness):
    harness.write_leads("drop_1", "motor_renewal", "batch_001", MOTOR_HEADER,
                        [motor("9876543210", "MOT/1")])
    harness.ingest("drop_1")
    row = harness.lead_row("MOT/1")
    assert row["last_call_at"] == ""
    assert row["current_disposition"] == ""


def test_rejections_are_sorted_and_free_of_pii(harness):
    seed(harness)
    directory = harness.reports()["_dir"]
    text = (directory / "rejections.csv").read_text(encoding="utf-8")
    assert "Aarav" not in text and "9876543210" not in text
    rows = harness.reports()["rejections"]
    assert [(r["workflow_id"], r["batch_id"], r["row_number"]) for r in rows] == [
        ("motor_renewal", "batch_001", "3")
    ]
    assert rows[0]["reason"] == "missing_required:phone"


def test_data_quality_has_exactly_the_contract_keys_in_order(harness):
    seed(harness)
    directory = harness.reports()["_dir"]
    quality = json.loads((directory / "data_quality.json").read_text())
    assert list(quality) == [
        "lead_rows_total", "lead_rows_rejected", "rejections_by_reason",
        "duplicate_lead_rows_merged", "field_warnings", "events_total",
        "malformed_events", "duplicate_events_dropped", "orphan_events",
        "unknown_disposition_events",
    ]
    assert all(isinstance(v, int) for k, v in quality.items() if k != "rejections_by_reason")


def test_reports_can_be_written_twice_to_the_same_directory(harness):
    seed(harness)
    first = harness.reports()
    second = harness.reports()
    assert {k: v for k, v in first.items() if k != "_dir"} == \
           {k: v for k, v in second.items() if k != "_dir"}


def test_reports_on_an_empty_database_are_headers_only(harness):
    directory = harness.reports()["_dir"]
    for name, header in EXPECTED_HEADERS.items():
        assert (directory / name).read_text(encoding="utf-8") == header + "\n"
    quality = json.loads((directory / "data_quality.json").read_text())
    assert quality["lead_rows_total"] == 0 and quality["rejections_by_reason"] == {}
