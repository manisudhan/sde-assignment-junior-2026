"""Idempotency, incrementality and the safety rails around re-runs."""
from __future__ import annotations

import sqlite3

import pytest

from solution.ledger.crypto import PiiCipher
from solution.ledger.db import assert_key_matches
from solution.ledger.pipeline import ingest_drop
from solution.tests.conftest import MOTOR_HEADER, call_events


def lead_row(phone="9876543210", policy="MOT/1", premium="12000") -> list[str]:
    return ["Aarav Menon", phone, policy, "2026-11-21", premium, "en"]


def seed_two_drops(harness) -> None:
    """drop_1: a lead plus its calls. drop_2: a duplicate of that lead, a new
    lead, and the events belonging to a lead that only arrives in drop_2."""
    harness.write_leads("drop_1", "motor_renewal", "batch_001", MOTOR_HEADER,
                        [lead_row(policy="MOT/1", premium="12000")])
    harness.write_events("drop_1", "events_0001.jsonl",
                         call_events("c1", "call_1", "2026-10-01", disposition="payment_link_sent")
                         + call_events("c9", "call_9", "2026-10-02", phone="9000000002",
                                       disposition="renewed"))
    harness.write_leads("drop_2", "motor_renewal", "batch_003", MOTOR_HEADER,
                        [lead_row(policy="MOT/1", premium="15000"),
                         lead_row(phone="9000000002", policy="MOT/2")])
    harness.write_events("drop_2", "events_1001.jsonl",
                         call_events("c2", "call_2", "2026-10-05", override="renewed"))


def snapshot(harness) -> dict:
    reports = harness.reports()
    return {k: v for k, v in reports.items() if k != "_dir"}


def test_re_ingesting_a_drop_changes_nothing(harness):
    seed_two_drops(harness)
    harness.ingest("drop_1")
    harness.ingest("drop_2")
    before = snapshot(harness)

    again = harness.ingest("drop_1")
    assert again.lead_files == 0 and again.event_files == 0
    assert again.skipped_files == 2
    harness.ingest("drop_2")

    assert snapshot(harness) == before


def test_re_ingesting_does_not_double_count_the_quality_numbers(harness):
    seed_two_drops(harness)
    harness.ingest("drop_1")
    before = harness.quality()
    harness.ingest("drop_1")
    harness.ingest("drop_1")
    assert harness.quality() == before


def test_a_changed_file_under_a_known_path_stops_the_run(harness):
    """Silently re-reading it would double-count; silently skipping it would
    hide a correction. Neither is safe to guess at, so we stop."""
    harness.write_leads("drop_1", "motor_renewal", "batch_001", MOTOR_HEADER, [lead_row()])
    harness.ingest("drop_1")
    harness.write_leads("drop_1", "motor_renewal", "batch_001", MOTOR_HEADER,
                        [lead_row(), lead_row(phone="9876543211", policy="MOT/2")])
    with pytest.raises(SystemExit, match="different content"):
        harness.ingest("drop_1")


def test_an_interrupted_run_recovers_on_the_next_ingest(harness):
    """Each file commits on its own and the re-link pass is a pure function of
    the ledger, so a crash between the two is repaired by simply running again."""
    seed_two_drops(harness)
    harness.ingest("drop_1")
    harness.conn.execute("DELETE FROM event_link")  # as if the run died before linking
    assert harness.quality()["orphan_events"] > 0
    harness.ingest("drop_1")  # every file is skipped, but linking still runs
    assert harness.lead_row("MOT/1")["total_calls"] == "1"


def test_ingesting_a_drop_at_a_time_matches_ingesting_both_at_once(harness, tmp_path):
    """The property the spec asks for: drop_1 then drop_2 == everything at once."""
    seed_two_drops(harness)
    harness.ingest("drop_1")
    harness.ingest("drop_2")
    incremental = snapshot(harness)

    # Same inputs, fresh database, both drops in one run.
    from solution.ledger.db import connect
    other = connect(tmp_path / "all_at_once.db")
    try:
        cipher = PiiCipher(b"pytest-fixed-key")
        for name in ("drop_1", "drop_2"):
            ingest_drop(other, cipher, harness.catalogue, harness.root / name)
        from solution.ledger.report import write_reports
        write_reports(other, tmp_path / "reports_all")
    finally:
        other.close()

    import csv
    import json
    at_once = {}
    for name in ("batch_summary", "daily_overview", "lead_report", "rejections"):
        with open(tmp_path / "reports_all" / f"{name}.csv", newline="", encoding="utf-8") as fh:
            at_once[name] = list(csv.DictReader(fh))
    at_once["data_quality"] = json.loads(
        (tmp_path / "reports_all" / "data_quality.json").read_text()
    )
    assert at_once == incremental


def test_the_ledger_refuses_to_be_rewritten(harness):
    seed_two_drops(harness)
    harness.ingest("drop_1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        harness.conn.execute("UPDATE interaction_event SET event_type = 'tampered'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        harness.conn.execute("DELETE FROM interaction_event")


def test_the_wrong_pii_key_is_refused_rather_than_duplicating_every_lead(harness):
    harness.write_leads("drop_1", "motor_renewal", "batch_001", MOTOR_HEADER, [lead_row()])
    harness.ingest("drop_1")
    with pytest.raises(SystemExit, match="different LEDGER_PII_KEY"):
        assert_key_matches(harness.conn, PiiCipher(b"a-completely-different-key").fingerprint)


def test_an_empty_drop_is_not_an_error(harness):
    harness.drop("drop_1")
    summary = harness.ingest("drop_1")
    assert (summary.lead_files, summary.event_files) == (0, 0)
    assert harness.reports()["lead_report"] == []


def test_a_workflow_with_no_schema_is_skipped_loudly(harness):
    harness.write_leads("drop_1", "mystery_workflow", "batch_001",
                        ["customer_name", "phone"], [["Someone", "9876543210"]])
    summary = harness.ingest("drop_1")
    assert summary.unknown_workflows == ["mystery_workflow"]
    assert harness.reports()["lead_report"] == []
