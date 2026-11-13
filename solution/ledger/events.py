"""Event ingestion into the append-only interaction ledger.

Every non-empty line is read exactly once and classified in the spec's order:
malformed, then duplicate, then (at report time, from the final state) orphan and
unknown disposition.

Orphan and unknown-disposition are deliberately *not* decided here. Events can
arrive before the lead they belong to, so "is this an orphan?" has no stable
answer at the moment a line is read - it only has one once everything is in.
Both are therefore properties of the ledger, derived at report time, which is
also what makes "orphan counts reflect the final state" true by construction
rather than by remembering to go back and fix up a counter.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from solution.ledger.crypto import PiiCipher
from solution.ledger.normalize import (
    ist_date,
    ist_iso,
    normalize_phone,
    parse_timestamp,
    phone_last4,
    utc_key,
)

#: Rows per executemany batch. Large enough to amortise the round trip, small
#: enough that a 600k-event file never materialises in memory all at once.
BATCH_SIZE = 5_000

DISPOSITION_EVENT_TYPES = ("disposition.set", "disposition.override")


@dataclass
class EventFileCounters:
    events_total: int = 0
    malformed_events: int = 0
    duplicate_events_dropped: int = 0


def build_event_row(raw: dict, cipher: PiiCipher, file_id: int) -> tuple:
    """Normalise one parsed JSON event into a ledger row."""
    event_type = raw.get("event_type")
    workflow_id = raw.get("workflow_id")
    call_id = raw.get("call_id")
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    moment = parse_timestamp(raw.get("occurred_at"))
    occurred_at_utc = utc_key(moment) if moment else None
    occurred_at_ist = ist_iso(moment) if moment else None
    occurred_date_ist = ist_date(moment) if moment else None

    # disposition.override carries no phone at all; some events carry one that
    # does not normalise. Either way phone_bidx is NULL and the event has to be
    # linked through its call instead.
    phone_bidx = phone_enc = last4 = None
    canonical = normalize_phone(raw.get("phone")) if isinstance(raw.get("phone"), str) else None
    if canonical:
        phone_bidx = cipher.blind_index(canonical)
        phone_enc = cipher.encrypt(canonical)
        last4 = phone_last4(canonical)

    disposition_code = None
    if event_type in DISPOSITION_EVENT_TYPES:
        code = payload.get("disposition_code")
        disposition_code = code if isinstance(code, str) and code.strip() else None

    return (
        str(raw["event_id"]),
        str(event_type) if event_type is not None else "",
        str(workflow_id) if workflow_id is not None else "",
        str(call_id) if call_id is not None else None,
        occurred_at_utc,
        occurred_at_ist,
        occurred_date_ist,
        phone_bidx,
        phone_enc,
        last4,
        disposition_code,
        json.dumps(payload, sort_keys=True, ensure_ascii=False),
        file_id,
    )


_INSERT_EVENT = (
    "INSERT OR IGNORE INTO interaction_event("
    "event_id, event_type, workflow_id, call_id, occurred_at_utc, occurred_at_ist, "
    "occurred_date_ist, phone_bidx, phone_enc, phone_last4, disposition_code, payload, "
    "source_file_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def ingest_event_file(
    conn: sqlite3.Connection, cipher: PiiCipher, path: Path, file_id: int
) -> EventFileCounters:
    """Ingest one JSON Lines file into the ledger."""
    counters = EventFileCounters()
    pending: list[tuple] = []

    def flush() -> None:
        if not pending:
            return
        before = conn.total_changes
        conn.executemany(_INSERT_EVENT, pending)
        inserted = conn.total_changes - before
        # INSERT OR IGNORE silently drops anything whose event_id we already
        # hold - from an earlier drop, an earlier file, or earlier in this very
        # file. Whatever it ignored was a duplicate delivery, so the arithmetic
        # is the duplicate count, with no need to hold every event_id in memory.
        counters.duplicate_events_dropped += len(pending) - inserted
        pending.clear()

    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue  # blank padding lines are not events
            counters.events_total += 1
            try:
                raw = json.loads(line)
            except ValueError:
                counters.malformed_events += 1
                continue
            if not isinstance(raw, dict) or not raw.get("event_id"):
                counters.malformed_events += 1
                continue
            pending.append(build_event_row(raw, cipher, file_id))
            if len(pending) >= BATCH_SIZE:
                flush()
    flush()
    return counters
