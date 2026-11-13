"""Test harness: build tiny hand-written drops and run the real pipeline on them.

The tests deliberately go through ``ingest_drop`` and ``write_reports`` rather
than poking at internals, so they exercise the same code path a reviewer runs
from the command line - including the per-file idempotency guard and the
re-link pass.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from solution.ledger.config import DISPOSITIONS_CSV, SCHEMA_DIR  # noqa: E402
from solution.ledger.crypto import PiiCipher  # noqa: E402
from solution.ledger.db import connect  # noqa: E402
from solution.ledger.pipeline import ingest_drop  # noqa: E402
from solution.ledger.report import data_quality, write_reports  # noqa: E402
from solution.ledger.workflow import load_catalogue  # noqa: E402

#: Fixed so the blind index is stable across tests and runs.
TEST_KEY = b"pytest-fixed-key"

MOTOR_HEADER = [
    "customer_name", "phone", "policy_number", "policy_expiry",
    "premium_amount", "preferred_language",
]


@dataclass
class Harness:
    root: Path
    db_path: Path

    def __post_init__(self) -> None:
        self.cipher = PiiCipher(TEST_KEY)
        self.catalogue = load_catalogue(SCHEMA_DIR, DISPOSITIONS_CSV)
        self.conn = connect(self.db_path)

    # -- building input -----------------------------------------------------
    def drop(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_leads(
        self, drop: str, workflow: str, batch: str, header: list[str], rows: list[list[str]]
    ) -> Path:
        path = self.drop(drop) / "leads" / workflow / f"{batch}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)
        return path

    def write_events(self, drop: str, filename: str, events: list) -> Path:
        path = self.drop(drop) / "events" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [e if isinstance(e, str) else json.dumps(e) for e in events]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    # -- running ------------------------------------------------------------
    def ingest(self, drop: str):
        return ingest_drop(self.conn, self.cipher, self.catalogue, self.drop(drop))

    def reports(self, out: str = "reports") -> dict:
        directory = self.root / out
        write_reports(self.conn, directory)
        parsed: dict = {}
        for name in ("batch_summary", "daily_overview", "lead_report", "rejections"):
            with open(directory / f"{name}.csv", newline="", encoding="utf-8") as handle:
                parsed[name] = list(csv.DictReader(handle))
        parsed["data_quality"] = json.loads((directory / "data_quality.json").read_text())
        parsed["_dir"] = directory
        return parsed

    def quality(self) -> dict:
        return data_quality(self.conn)

    def lead_row(self, external_ref: str) -> dict:
        rows = self.reports()["lead_report"]
        matches = [r for r in rows if r["external_ref"] == external_ref]
        assert len(matches) == 1, f"expected one row for {external_ref}, got {len(matches)}"
        return matches[0]

    def query(self, sql: str, params: tuple = ()):  # convenience for assertions
        return self.conn.execute(sql, params).fetchall()


@pytest.fixture
def harness(tmp_path, monkeypatch) -> Harness:
    monkeypatch.setenv("LEDGER_PII_KEY", TEST_KEY.decode())
    instance = Harness(root=tmp_path, db_path=tmp_path / "ledger.db")
    yield instance
    instance.conn.close()


# ---------------------------------------------------------------------------
# Event builders - a realistic call is several events, so the tests say what
# they mean instead of repeating ten-key dicts.
# ---------------------------------------------------------------------------

def event(
    event_id: str,
    event_type: str,
    occurred_at: str,
    *,
    workflow: str = "motor_renewal",
    call_id: str | None = "call_1",
    phone: str | None = "9876543210",
    payload: dict | None = None,
) -> dict:
    body = {
        "event_id": event_id,
        "event_type": event_type,
        "workflow_id": workflow,
        "call_id": call_id,
        "occurred_at": occurred_at,
        "payload": payload or {},
    }
    if phone is not None:
        body["phone"] = phone
    return body


def call_events(
    prefix: str,
    call_id: str,
    day: str,
    *,
    phone: str = "9876543210",
    workflow: str = "motor_renewal",
    connected: bool = True,
    telephony_failed: bool = False,
    disposition: str | None = None,
    override: str | None = None,
    hour: str = "10",
) -> list[dict]:
    """One whole call, in the shape the telephony layer emits."""
    base = f"{day}T{hour}:00:00+05:30"
    events = [
        event(f"{prefix}_init", "call.initiated", base, workflow=workflow,
              call_id=call_id, phone=phone)
    ]
    if telephony_failed:
        events.append(
            event(f"{prefix}_fail", "call.telephony_failed", f"{day}T{hour}:00:05+05:30",
                  workflow=workflow, call_id=call_id, phone=phone,
                  payload={"reason": "carrier_rejected"})
        )
        return events
    if connected:
        events.append(
            event(f"{prefix}_conn", "call.connected", f"{day}T{hour}:00:10+05:30",
                  workflow=workflow, call_id=call_id, phone=phone)
        )
    events.append(
        event(f"{prefix}_end", "call.ended", f"{day}T{hour}:05:00+05:30",
              workflow=workflow, call_id=call_id, phone=phone,
              payload={"duration_sec": 290, "end_reason": "completed"})
    )
    if disposition is not None:
        events.append(
            event(f"{prefix}_disp", "disposition.set", f"{day}T{hour}:06:00+05:30",
                  workflow=workflow, call_id=call_id, phone=phone,
                  payload={"disposition_code": disposition})
        )
    if override is not None:
        # Overrides carry no phone at all: they link through their call_id.
        events.append(
            event(f"{prefix}_ovr", "disposition.override", f"{day}T{hour}:07:00+05:30",
                  workflow=workflow, call_id=call_id, phone=None,
                  payload={"disposition_code": override, "overridden_by": "supervisor_1",
                           "reason": "qa_review"})
        )
    return events
