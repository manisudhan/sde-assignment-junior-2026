"""Effective disposition and lead buckets.

These are the rules a client's renewal numbers hang off, and the ones with the
most ways to be subtly wrong: an override that arrives out of order, an unknown
code that should fall back rather than blank, a terminal outcome on an earlier
call, a later call with no outcome at all.
"""
from __future__ import annotations

from solution.tests.conftest import MOTOR_HEADER, call_events, event


def lead_row(phone="9876543210", policy="MOT/1") -> list[str]:
    return ["Aarav Menon", phone, policy, "2026-11-21", "12000", "en"]


def seed(harness, phone="9876543210", policy="MOT/1") -> None:
    harness.write_leads("drop_1", "motor_renewal", "batch_001", MOTOR_HEADER,
                        [lead_row(phone, policy)])


def run(harness, events, drop="drop_1") -> dict:
    harness.write_events(drop, "events_0001.jsonl", events)
    harness.ingest(drop)
    return harness.lead_row("MOT/1")


# ---------------------------------------------------------------------------
# Effective disposition
# ---------------------------------------------------------------------------

def test_latest_set_wins_when_there_is_no_override(harness):
    seed(harness)
    lead = run(harness, call_events("c1", "call_1", "2026-10-01", disposition="payment_link_sent") + [
        event("evt_late", "disposition.set", "2026-10-01T12:00:00+05:30",
              call_id="call_1", payload={"disposition_code": "renewed"}),
    ])
    assert lead["current_disposition"] == "renewed"


def test_an_override_beats_the_set_even_when_it_arrived_earlier(harness):
    """The rule is "latest override, else latest set" - an override outranks a
    set by kind, not by clock."""
    seed(harness)
    lead = run(harness, [
        event("evt_init", "call.initiated", "2026-10-01T10:00:00+05:30", call_id="call_1"),
        event("evt_ovr", "disposition.override", "2026-10-01T11:00:00+05:30",
              call_id="call_1", phone=None,
              payload={"disposition_code": "not_interested", "overridden_by": "sup_1"}),
        event("evt_set", "disposition.set", "2026-10-01T23:00:00+05:30",
              call_id="call_1", payload={"disposition_code": "renewed"}),
    ])
    assert lead["current_disposition"] == "not_interested"
    assert lead["ever_success"] == "false"


def test_the_latest_of_several_overrides_wins(harness):
    seed(harness)
    lead = run(harness, call_events("c1", "call_1", "2026-10-01", disposition="callback_requested") + [
        event("evt_o1", "disposition.override", "2026-10-02T10:00:00+05:30", call_id="call_1",
              phone=None, payload={"disposition_code": "not_interested", "overridden_by": "s1"}),
        event("evt_o2", "disposition.override", "2026-10-03T10:00:00+05:30", call_id="call_1",
              phone=None, payload={"disposition_code": "renewed", "overridden_by": "s2"}),
    ])
    assert lead["current_disposition"] == "renewed"
    assert lead["ever_success"] == "true"


def test_an_override_never_edits_or_deletes_the_original(harness):
    seed(harness)
    run(harness, call_events("c1", "call_1", "2026-10-01",
                             disposition="callback_requested", override="renewed"))
    codes = [
        (r["event_type"], r["disposition_code"])
        for r in harness.query(
            "SELECT event_type, disposition_code FROM interaction_event "
            "WHERE disposition_code IS NOT NULL ORDER BY event_id"
        )
    ]
    assert ("disposition.set", "callback_requested") in codes
    assert ("disposition.override", "renewed") in codes


def test_an_unknown_override_code_falls_back_to_the_valid_set(harness):
    """An unknown code is ignored, so it cannot blank out a known outcome."""
    seed(harness)
    lead = run(harness, call_events("c1", "call_1", "2026-10-01",
                                    disposition="renewed", override="not_a_real_code"))
    assert lead["current_disposition"] == "renewed"
    assert harness.quality()["unknown_disposition_events"] == 1


def test_a_call_with_only_an_unknown_code_has_no_disposition(harness):
    seed(harness)
    lead = run(harness, call_events("c1", "call_1", "2026-10-01", disposition="unknown"))
    assert lead["current_disposition"] == ""
    assert lead["bucket"] == "in_progress_dialed"


def test_a_disposition_event_with_no_code_in_its_payload_is_unknown(harness):
    seed(harness)
    lead = run(harness, call_events("c1", "call_1", "2026-10-01") + [
        event("evt_disp", "disposition.set", "2026-10-01T10:06:00+05:30", call_id="call_1",
              payload={}),
    ])
    assert lead["current_disposition"] == ""
    assert harness.quality()["unknown_disposition_events"] == 1


def test_current_disposition_comes_from_the_latest_call_that_has_one(harness):
    """A later call that produced no outcome must not blank the lead's
    disposition."""
    seed(harness)
    lead = run(
        harness,
        call_events("c1", "call_1", "2026-10-01", disposition="payment_link_sent")
        + call_events("c2", "call_2", "2026-10-05", connected=False),
    )
    assert lead["total_calls"] == "2"
    assert lead["last_call_at"] == "2026-10-05T10:00:00+05:30"
    assert lead["current_disposition"] == "payment_link_sent"


def test_last_call_at_is_the_latest_initiated_time_in_ist(harness):
    seed(harness)
    lead = run(
        harness,
        call_events("c1", "call_1", "2026-10-01", connected=False, hour="09")
        + call_events("c2", "call_2", "2026-10-03", connected=False, hour="18"),
    )
    assert lead["last_call_at"] == "2026-10-03T18:00:00+05:30"


def test_a_call_without_its_initiated_event_is_not_a_call(harness):
    seed(harness)
    lead = run(harness, [
        event("evt_conn", "call.connected", "2026-10-01T10:00:10+05:30", call_id="call_1"),
        event("evt_end", "call.ended", "2026-10-01T10:05:00+05:30", call_id="call_1",
              payload={"duration_sec": 290, "end_reason": "completed"}),
        event("evt_disp", "disposition.set", "2026-10-01T10:06:00+05:30", call_id="call_1",
              payload={"disposition_code": "renewed"}),
    ])
    assert lead["total_calls"] == "0"
    assert lead["bucket"] == "yet_to_reach_out"
    assert lead["last_call_at"] == ""
    assert lead["current_disposition"] == ""


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------

def test_bucket_yet_to_reach_out_when_there_are_no_calls(harness):
    seed(harness)
    harness.ingest("drop_1")
    lead = harness.lead_row("MOT/1")
    assert lead["bucket"] == "yet_to_reach_out"
    assert (lead["total_calls"], lead["connected_calls"], lead["last_call_at"]) == ("0", "0", "")


def test_bucket_completed_on_any_terminal_disposition(harness):
    seed(harness)
    lead = run(harness, call_events("c1", "call_1", "2026-10-01", disposition="not_interested"))
    assert lead["bucket"] == "completed"
    assert lead["ever_success"] == "false"  # terminal, but not a win


def test_completed_wins_over_telephony_failed_on_a_later_call(harness):
    """Rule 2 is checked before rule 3: a terminal outcome on call 1 outranks a
    carrier failure on call 2."""
    seed(harness)
    lead = run(
        harness,
        call_events("c1", "call_1", "2026-10-01", disposition="renewed")
        + call_events("c2", "call_2", "2026-10-05", telephony_failed=True),
    )
    assert lead["bucket"] == "completed"
    assert lead["ever_success"] == "true"


def test_bucket_telephony_failed_only_when_every_call_failed(harness):
    seed(harness)
    lead = run(
        harness,
        call_events("c1", "call_1", "2026-10-01", telephony_failed=True)
        + call_events("c2", "call_2", "2026-10-02", telephony_failed=True),
    )
    assert lead["bucket"] == "in_progress_telephony_failed"
    assert lead["connected_calls"] == "0"


def test_one_call_that_rang_puts_the_lead_back_in_dialed(harness):
    seed(harness)
    lead = run(
        harness,
        call_events("c1", "call_1", "2026-10-01", telephony_failed=True)
        + call_events("c2", "call_2", "2026-10-02", connected=False),
    )
    assert lead["bucket"] == "in_progress_dialed"


def test_bucket_dialed_for_a_connected_call_with_a_non_terminal_outcome(harness):
    seed(harness)
    lead = run(harness, call_events("c1", "call_1", "2026-10-01",
                                    disposition="callback_requested"))
    assert lead["bucket"] == "in_progress_dialed"
    assert lead["connected_calls"] == "1"


def test_a_connected_call_with_no_disposition_is_dialed_not_completed(harness):
    seed(harness)
    lead = run(harness, call_events("c1", "call_1", "2026-10-01"))
    assert lead["bucket"] == "in_progress_dialed"
    assert lead["current_disposition"] == ""


def test_an_override_can_move_a_lead_into_completed(harness):
    seed(harness)
    lead = run(harness, call_events("c1", "call_1", "2026-10-01",
                                    disposition="callback_requested", override="dnd"))
    assert lead["bucket"] == "completed"
    assert lead["current_disposition"] == "dnd"


def test_an_override_can_move_a_lead_back_out_of_completed(harness):
    """A supervisor correcting `not_interested` to `callback_requested` must
    reopen the lead, not leave it closed."""
    seed(harness)
    lead = run(harness, call_events("c1", "call_1", "2026-10-01",
                                    disposition="not_interested", override="callback_requested"))
    assert lead["bucket"] == "in_progress_dialed"
    assert lead["ever_success"] == "false"
