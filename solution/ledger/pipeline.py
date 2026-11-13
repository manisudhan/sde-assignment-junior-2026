"""Drop ingestion: ordering, idempotency and orchestration.

A *drop* is one day's delivery directory. Ingesting it is:

    lead files (in deterministic order)  ->  event files  ->  re-link

Leads go first so that events in the same drop usually link on the first pass;
correctness does not depend on it, because the re-link pass handles any order.

Idempotency is per *file*, keyed on ``(drop_id, relative path)`` and guarded by a
content hash. A file already recorded in ``ingested_file`` is skipped outright,
so re-running ``ingest`` on a drop changes nothing - not the ledger, and not the
counters, since the counters live on that same row. If a path comes back with
different bytes the run stops rather than guessing, because the two plausible
answers (re-read it, or keep the old numbers) are both wrong often enough to
matter and nobody is watching a cron job closely enough to catch it.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from pathlib import Path

from solution.ledger.crypto import PiiCipher
from solution.ledger.db import assert_key_matches, sync_dispositions
from solution.ledger.events import ingest_event_file
from solution.ledger.leads import LeadWriter, ingest_lead_file
from solution.ledger.linking import relink
from solution.ledger.workflow import Catalogue

_HASH_CHUNK = 1 << 20


@dataclass
class IngestSummary:
    drop_id: str
    lead_files: int = 0
    event_files: int = 0
    skipped_files: int = 0
    lead_rows_total: int = 0
    lead_rows_rejected: int = 0
    duplicate_lead_rows_merged: int = 0
    field_warnings: int = 0
    events_total: int = 0
    malformed_events: int = 0
    duplicate_events_dropped: int = 0
    links: dict[str, int] = dc_field(default_factory=dict)
    unknown_workflows: list[str] = dc_field(default_factory=list)

    def as_lines(self) -> list[str]:
        return [
            f"drop {self.drop_id}:",
            f"  files           : {self.lead_files} lead, {self.event_files} event"
            f" ({self.skipped_files} already ingested, skipped)",
            f"  lead rows       : {self.lead_rows_total} read, "
            f"{self.lead_rows_rejected} rejected, "
            f"{self.duplicate_lead_rows_merged} merged duplicates, "
            f"{self.field_warnings} field warnings",
            f"  events          : {self.events_total} lines, "
            f"{self.malformed_events} malformed, "
            f"{self.duplicate_events_dropped} duplicates dropped",
            f"  links created   : {self.links.get('linked_by_phone', 0)} by phone, "
            f"{self.links.get('linked_by_call', 0)} by call",
        ]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _register_file(
    conn: sqlite3.Connection, drop_id: str, rel_path: str, kind: str, path: Path
) -> int | None:
    """Claim a file for ingestion. ``None`` means it was ingested before."""
    digest = file_sha256(path)
    row = conn.execute(
        "SELECT file_id, content_sha256 FROM ingested_file WHERE drop_id = ? AND rel_path = ?",
        (drop_id, rel_path),
    ).fetchone()
    if row is not None:
        if row["content_sha256"] != digest:
            raise SystemExit(
                f"{drop_id}/{rel_path} was already ingested with different content "
                f"(stored {row['content_sha256'][:12]}..., now {digest[:12]}...). "
                "Re-delivering a changed file is a data-correction event, not a "
                "re-run; ingest it under a new path or rebuild the database."
            )
        return None
    cursor = conn.execute(
        "INSERT INTO ingested_file(drop_id, rel_path, kind, content_sha256, ingested_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (drop_id, rel_path, kind, digest, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    return int(cursor.lastrowid)


def _lead_files(drop_dir: Path) -> list[tuple[str, Path]]:
    """``(workflow_id, path)`` for every lead CSV, in deterministic order."""
    leads_root = drop_dir / "leads"
    if not leads_root.is_dir():
        return []
    found = []
    for workflow_dir in sorted(p for p in leads_root.iterdir() if p.is_dir()):
        for path in sorted(workflow_dir.glob("*.csv")):
            found.append((workflow_dir.name, path))
    return found


def _event_files(drop_dir: Path) -> list[Path]:
    events_root = drop_dir / "events"
    if not events_root.is_dir():
        return []
    return sorted(events_root.glob("*.jsonl"))


def ingest_drop(
    conn: sqlite3.Connection,
    cipher: PiiCipher,
    catalogue: Catalogue,
    drop_dir: Path,
) -> IngestSummary:
    """Ingest one drop directory. Safe to call twice with the same drop."""
    drop_dir = Path(drop_dir).resolve()
    if not drop_dir.is_dir():
        raise SystemExit(f"not a directory: {drop_dir}")

    assert_key_matches(conn, cipher.fingerprint)
    sync_dispositions(conn, catalogue)

    summary = IngestSummary(drop_id=drop_dir.name)
    writer = LeadWriter(conn, cipher)

    for workflow_id, path in _lead_files(drop_dir):
        schema = catalogue.schema(workflow_id)
        if schema is None:
            # A workflow we have no schema for: skip the whole file loudly
            # rather than inventing a mapping for it.
            if workflow_id not in summary.unknown_workflows:
                summary.unknown_workflows.append(workflow_id)
            continue
        rel_path = path.relative_to(drop_dir).as_posix()
        conn.execute("BEGIN IMMEDIATE")
        try:
            file_id = _register_file(conn, summary.drop_id, rel_path, "leads", path)
            if file_id is None:
                conn.execute("ROLLBACK")
                summary.skipped_files += 1
                continue
            counters = ingest_lead_file(conn, writer, schema, path, file_id)
            writer.flush(catalogue.schemas)
            conn.execute(
                "UPDATE ingested_file SET lead_rows_total = ?, lead_rows_rejected = ?, "
                "duplicate_lead_rows_merged = ?, field_warnings = ? WHERE file_id = ?",
                (
                    counters.lead_rows_total, counters.lead_rows_rejected,
                    counters.duplicate_lead_rows_merged, counters.field_warnings, file_id,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        summary.lead_files += 1
        summary.lead_rows_total += counters.lead_rows_total
        summary.lead_rows_rejected += counters.lead_rows_rejected
        summary.duplicate_lead_rows_merged += counters.duplicate_lead_rows_merged
        summary.field_warnings += counters.field_warnings

    for path in _event_files(drop_dir):
        rel_path = path.relative_to(drop_dir).as_posix()
        conn.execute("BEGIN IMMEDIATE")
        try:
            file_id = _register_file(conn, summary.drop_id, rel_path, "events", path)
            if file_id is None:
                conn.execute("ROLLBACK")
                summary.skipped_files += 1
                continue
            counters = ingest_event_file(conn, cipher, path, file_id)
            conn.execute(
                "UPDATE ingested_file SET events_total = ?, malformed_events = ?, "
                "duplicate_events_dropped = ? WHERE file_id = ?",
                (
                    counters.events_total, counters.malformed_events,
                    counters.duplicate_events_dropped, file_id,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        summary.event_files += 1
        summary.events_total += counters.events_total
        summary.malformed_events += counters.malformed_events
        summary.duplicate_events_dropped += counters.duplicate_events_dropped

    conn.execute("BEGIN IMMEDIATE")
    try:
        summary.links = relink(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return summary
