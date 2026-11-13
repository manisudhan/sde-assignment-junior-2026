"""Event ingestion: malformed lines, dedupe, linking, orphans."""
from __future__ import annotations

import json

from solution.tests.conftest import MOTOR_HEADER, call_events, event


def lead_row(phone="9876543210", policy="MOT/1") -> list[str]:
    return ["Aarav Menon", phone, policy, "2026-11-21", "12000", "en"]


def seed_lead(harness, drop="drop_1", batch="batch_001", phone="9876543210", policy="MOT/1"):
    harness.write_leads("drop_1" if drop == "drop_1" else drop, "motor_renewal", batch,
                        MOTOR_HEADER, [lead_row(phone, policy)])


# ---------------------------------------------------------------------------
# Malformed
# ---------------------------------------------------------------------------

def test_malformed_lines_are_counted_and_skipped(harness):
    seed_lead(harness)
    harness.write_events("drop_1", "events_0001.jsonl", [
        event("evt_1", "call.initiated", "2026-10-01T10:00:00+05:30"),
        '{"event_id": "evt_2", "event_type": "call.ini',   # truncated JSON
        "not json at all",
        json.dumps({"event_type": "call.initiated", "phone": "9876543210"}),  # no event_id
        json.dumps({"event_id": "", "event_type": "call.initiated"}),         # empty event_id
        json.dumps([1, 2, 3]),                                                # not an object
    ])
    harness.ingest("drop_1")
    quality = harness.quality()
    assert quality["events_total"] == 6
    assert quality["malformed_events"] == 5
    assert harness.query("SELECT COUNT(*) AS n FROM interaction_event")[0]["n"] == 1


def test_blank_lines_are_not_events(harness):
    seed_lead(harness)
    path = harness.write_events("drop_1", "events_0001.jsonl", [
        event("evt_1", "call.initiated", "2026-10-01T10:00:00+05:30"),
    ])
    path.write_text(path.read_text() + "\n   \n\n", encoding="utf-8")
    harness.ingest("drop_1")
    assert harness.quality()["events_total"] == 1


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------

def test_duplicate_event_ids_are_dropped_within_a_file(harness):
    seed_lead(harness)
    duplicate = event("evt_1", "call.initiated", "2026-10-01T10:00:00+05:30")
    harness.write_events("drop_1", "events_0001.jsonl", [duplicate, duplicate, duplicate])
    harness.ingest("drop_1")
    quality = harness.quality()
    assert quality["events_total"] == 3
    assert quality["duplicate_events_dropped"] == 2
    assert harness.query("SELECT COUNT(*) AS n FROM interaction_event")[0]["n"] == 1


def test_duplicate_event_ids_are_dropped_across_drops(harness):
    seed_lead(harness)
    duplicate = event("evt_1", "call.initiated", "2026-10-01T10:00:00+05:30")
    harness.write_events("drop_1", "events_0001.jsonl", [duplicate])
    harness.ingest("drop_1")
    harness.write_events("drop_2", "events_1001.jsonl", [duplicate])
    harness.ingest("drop_2")
    quality = harness.quality()
    assert quality["events_total"] == 2
    assert quality["duplicate_events_dropped"] == 1
    assert harness.query("SELECT COUNT(*) AS n FROM interaction_event")[0]["n"] == 1


def test_the_first_delivery_wins_and_history_is_not_rewritten(harness):
    seed_lead(harness)
    first = event("evt_1", "call.initiated", "2026-10-01T10:00:00+05:30")
    tampered = dict(first, event_type="call.connected", occurred_at="2026-10-09T10:00:00+05:30")
    harness.write_events("drop_1", "events_0001.jsonl", [first, tampered])
    harness.ingest("drop_1")
    stored = harness.query("SELECT event_type, occurred_at_ist FROM interaction_event")[0]
    assert stored["event_type"] == "call.initiated"
    assert stored["occurred_at_ist"] == "2026-10-01T10:00:00+05:30"


# ---------------------------------------------------------------------------
# Linking and orphans
# ---------------------------------------------------------------------------

def test_events_link_through_any_phone_format(harness):
    seed_lead(harness, phone="9876543210")
    harness.write_events("drop_1", "events_0001.jsonl", [
        event("evt_1", "call.initiated", "2026-10-01T10:00:00+05:30", phone="91-98765-43210"),
        event("evt_2", "call.connected", "2026-10-01T10:00:10+05:30", phone="(+91) 9876543210"),
        event("evt_3", "call.ended", "2026-10-01T10:05:00+05:30", phone="09876543210"),
    ])
    harness.ingest("drop_1")
    assert harness.quality()["orphan_events"] == 0


def test_events_for_a_number_never_uploaded_are_orphans(harness):
    seed_lead(harness, phone="9876543210")
    harness.write_events("drop_1", "events_0001.jsonl",
                         call_events("c1", "call_9", "2026-10-01", phone="9000000001"))
    harness.ingest("drop_1")
    assert harness.quality()["orphan_events"] == 3  # initiated, connected, ended
    assert harness.lead_row("MOT/1")["total_calls"] == "0"


def test_events_in_the_wrong_workflow_do_not_link(harness):
    seed_lead(harness, phone="9876543210")
    harness.write_events("drop_1", "events_0001.jsonl", [
        event("evt_1", "call.initiated", "2026-10-01T10:00:00+05:30", workflow="policy_feedback"),
    ])
    harness.ingest("drop_1")
    assert harness.quality()["orphan_events"] == 1


def test_an_event_arriving_before_its_lead_counts_once_the_lead_lands(harness):
    """The headline case: drop 1's orphans must be adopted by drop 2's leads,
    and the orphan count must reflect the final state, not the first guess."""
    harness.write_events("drop_1", "events_0001.jsonl",
                         call_events("c1", "call_1", "2026-10-01", disposition="renewed"))
    harness.ingest("drop_1")
    assert harness.quality()["orphan_events"] == 4
    assert harness.reports()["lead_report"] == []

    harness.write_leads("drop_2", "motor_renewal", "batch_003", MOTOR_HEADER, [lead_row()])
    harness.ingest("drop_2")

    assert harness.quality()["orphan_events"] == 0
    lead = harness.lead_row("MOT/1")
    assert lead["total_calls"] == "1"
    assert lead["connected_calls"] == "1"
    assert lead["current_disposition"] == "renewed"
    assert lead["ever_success"] == "true"


def test_an_override_links_through_its_call_id_not_a_phone(harness):
    seed_lead(harness)
    harness.write_events("drop_1", "events_0001.jsonl", call_events(
        "c1", "call_1", "2026-10-01", disposition="callback_requested", override="renewed"))
    harness.ingest("drop_1")
    assert harness.quality()["orphan_events"] == 0
    override = harness.query(
        "SELECT link_via FROM event_link k JOIN interaction_event e USING (event_id) "
        "WHERE e.event_type = 'disposition.override'"
    )[0]
    assert override["link_via"] == "call"
    assert harness.lead_row("MOT/1")["current_disposition"] == "renewed"


def test_an_override_whose_call_is_unknown_stays_an_orphan(harness):
    seed_lead(harness)
    harness.write_events("drop_1", "events_0001.jsonl", [
        event("evt_ovr", "disposition.override", "2026-10-01T12:00:00+05:30",
              call_id="call_nobody_knows", phone=None,
              payload={"disposition_code": "renewed", "overridden_by": "supervisor_1"}),
    ])
    harness.ingest("drop_1")
    assert harness.quality()["orphan_events"] == 1


def test_an_override_arriving_before_its_call_is_adopted_later(harness):
    """Overrides can land in a drop before the call they correct."""
    seed_lead(harness)
    harness.write_events("drop_1", "events_0001.jsonl", [
        event("evt_ovr", "disposition.override", "2026-10-02T12:00:00+05:30",
              call_id="call_1", phone=None,
              payload={"disposition_code": "renewed", "overridden_by": "supervisor_1"}),
    ])
    harness.ingest("drop_1")
    assert harness.quality()["orphan_events"] == 1

    harness.write_events("drop_2", "events_1001.jsonl", call_events(
        "c1", "call_1", "2026-10-01", disposition="not_interested"))
    harness.ingest("drop_2")
    assert harness.quality()["orphan_events"] == 0
    assert harness.lead_row("MOT/1")["current_disposition"] == "renewed"


def test_events_whose_phone_is_invalid_are_orphans(harness):
    seed_lead(harness)
    harness.write_events("drop_1", "events_0001.jsonl", [
        event("evt_1", "call.initiated", "2026-10-01T10:00:00+05:30", phone="5876543210"),
    ])
    harness.ingest("drop_1")
    assert harness.quality()["orphan_events"] == 1


def test_unknown_disposition_codes_are_counted_and_ignored(harness):
    seed_lead(harness)
    harness.write_events("drop_1", "events_0001.jsonl", call_events(
        "c1", "call_1", "2026-10-01", disposition="RENEWD"))
    harness.ingest("drop_1")
    quality = harness.quality()
    assert quality["unknown_disposition_events"] == 1
    assert quality["orphan_events"] == 0  # it linked fine, its code is just unknown
    assert harness.lead_row("MOT/1")["current_disposition"] == ""


def test_a_disposition_code_from_another_workflow_is_unknown(harness):
    """`feedback_captured` is a policy_feedback code; it means nothing here."""
    seed_lead(harness)
    harness.write_events("drop_1", "events_0001.jsonl", call_events(
        "c1", "call_1", "2026-10-01", disposition="feedback_captured"))
    harness.ingest("drop_1")
    assert harness.quality()["unknown_disposition_events"] == 1
    assert harness.lead_row("MOT/1")["current_disposition"] == ""


def test_an_unlinked_unknown_code_is_an_orphan_not_an_unknown_disposition(harness):
    """Orphan is checked before unknown disposition, so the two never
    double-count the same event."""
    seed_lead(harness)
    harness.write_events("drop_1", "events_0001.jsonl", call_events(
        "c1", "call_9", "2026-10-01", phone="9000000001", disposition="RENEWD"))
    harness.ingest("drop_1")
    quality = harness.quality()
    assert quality["orphan_events"] == 4
    assert quality["unknown_disposition_events"] == 0
