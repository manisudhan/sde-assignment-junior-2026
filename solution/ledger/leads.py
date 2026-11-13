"""Lead ingestion: header mapping, validation, rejection and duplicate merging.

The shape of this module follows the spec's order of operations exactly, because
that order is load-bearing: fields are walked *in schema order* so that the
first failure is the row's single rejection reason, and rows are applied *in
file order* so that the first valid row for an identity is the one that fixes
the lead's batch.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from solution.ledger.crypto import PiiCipher
from solution.ledger.normalize import (
    normalize_date,
    normalize_decimal,
    normalize_enum,
    normalize_phone,
    normalize_string,
    phone_last4,
)
from solution.ledger.workflow import Field, WorkflowSchema


@dataclass
class LeadFileCounters:
    lead_rows_total: int = 0
    lead_rows_rejected: int = 0
    duplicate_lead_rows_merged: int = 0
    field_warnings: int = 0


@dataclass
class ParsedRow:
    """Outcome of validating one data row against the workflow schema."""

    values: dict[str, str | None] = dc_field(default_factory=dict)
    reason: str | None = None
    warnings: int = 0

    @property
    def rejected(self) -> bool:
        return self.reason is not None


def _parse_value(field: Field, raw: str) -> str | None:
    if field.type == "phone":
        return normalize_phone(raw)
    if field.type == "date":
        return normalize_date(raw)
    if field.type == "decimal":
        return normalize_decimal(raw)
    if field.type == "enum":
        return normalize_enum(raw, list(field.values))
    return normalize_string(raw)


def is_blank_row(cells: list[str]) -> bool:
    """A row whose every cell is empty or whitespace. Skipped and not counted."""
    return all(not cell.strip() for cell in cells)


def parse_row(
    schema: WorkflowSchema, mapping: dict[int, Field], cells: list[str]
) -> ParsedRow:
    """Validate one row, walking fields in schema order and stopping at the
    first failure so the row carries exactly one rejection reason."""
    raw_by_field: dict[str, str] = {}
    for index, field in mapping.items():
        raw_by_field[field.name] = cells[index] if index < len(cells) else ""

    result = ParsedRow()
    for field in schema.fields:
        raw = raw_by_field.get(field.name, "").strip()

        if not raw:  # absent column, empty cell or whitespace: all "missing"
            if field.required:
                result.reason = f"missing_required:{field.name}"
                return result
            result.values[field.name] = None
            continue

        parsed = _parse_value(field, raw)
        if parsed is None:
            if field.type == "phone":
                result.reason = "invalid_phone"
                return result
            if field.required:
                # Unreachable with the shipped schemas (the only required
                # non-string field is the phone). A present-but-unparseable
                # required value is treated as missing - see SUBMISSION.md.
                result.reason = f"missing_required:{field.name}"
                return result
            # Optional field present but invalid: store null, warn, carry on.
            result.values[field.name] = None
            result.warnings += 1
            continue

        result.values[field.name] = parsed
    return result


class LeadWriter:
    """Applies validated rows to ``leads``, buffering writes per ingest run.

    Holds the merge state in memory so that "overwrite only the non-null fields"
    is expressed once, in Python, instead of as a pile of per-field SQL
    ``COALESCE``es that would have to change every time a workflow gains a field.
    Existing leads are preloaded per workflow, so an identity that first
    appeared in an earlier drop merges exactly as if both drops were ingested at
    once.
    """

    def __init__(self, conn: sqlite3.Connection, cipher: PiiCipher) -> None:
        self._conn = conn
        self._cipher = cipher
        self._loaded: set[str] = set()
        self._state: dict[tuple[str, str], dict] = {}
        self._touched: list[tuple[str, str]] = []
        row = conn.execute("SELECT COALESCE(MAX(lead_id), 0) AS m FROM leads").fetchone()
        self._next_lead_id = int(row["m"]) + 1

    def _ensure_loaded(self, workflow_id: str) -> None:
        if workflow_id in self._loaded:
            return
        for row in self._conn.execute(
            "SELECT lead_id, phone_bidx, phone_last4, batch_id, external_ref, attributes "
            "FROM leads WHERE workflow_id = ?",
            (workflow_id,),
        ):
            self._state[(workflow_id, row["phone_bidx"])] = {
                "lead_id": row["lead_id"],
                "workflow_id": workflow_id,
                "phone_bidx": row["phone_bidx"],
                "phone_last4": row["phone_last4"],
                "batch_id": row["batch_id"],
                "external_ref": row["external_ref"],
                "attributes": json.loads(row["attributes"]),
                "phone_enc": None,
                "pii": None,       # decrypted lazily, only if a duplicate merges
                "pii_dirty": False,
                "is_new": False,
                "dirty": False,
            }
        self._loaded.add(workflow_id)

    def _load_pii(self, state: dict) -> dict:
        """Decrypt a lead's PII blob on demand (only duplicate rows need it)."""
        if state["pii"] is None:
            row = self._conn.execute(
                "SELECT pii_enc FROM leads WHERE lead_id = ?", (state["lead_id"],)
            ).fetchone()
            blob = row["pii_enc"] if row else None
            plaintext = self._cipher.decrypt(blob)
            state["pii"] = json.loads(plaintext) if plaintext else {}
        return state["pii"]

    def apply(
        self,
        schema: WorkflowSchema,
        parsed: ParsedRow,
        batch_id: str,
        file_id: int,
    ) -> bool:
        """Create or merge the lead for one valid row.

        Returns ``True`` when the row merged into an existing identity, i.e. when
        it counts as a merged duplicate.
        """
        workflow_id = schema.workflow_id
        self._ensure_loaded(workflow_id)

        canonical_phone = parsed.values[schema.phone_field.name]
        assert canonical_phone is not None  # a valid row always has a valid phone
        key = (workflow_id, self._cipher.blind_index(canonical_phone))

        reference_name = schema.reference_field.name
        pii_values, attribute_values = {}, {}
        for field in schema.fields:
            if field.type == "phone" or field.name == reference_name:
                continue  # identity and the report-facing reference have own columns
            target = pii_values if field.pii else attribute_values
            target[field.name] = parsed.values.get(field.name)
        reference = parsed.values.get(reference_name)

        existing = self._state.get(key)
        if existing is None:
            self._state[key] = {
                "lead_id": self._next_lead_id,
                "workflow_id": workflow_id,
                "phone_bidx": key[1],
                "phone_last4": phone_last4(canonical_phone),
                "phone_enc": self._cipher.encrypt(canonical_phone),
                "batch_id": batch_id,  # permanent: set by the first valid row
                "external_ref": reference,
                "pii": dict(pii_values),
                "pii_dirty": True,
                "attributes": dict(attribute_values),
                "file_id": file_id,
                "is_new": True,
                "dirty": True,
            }
            self._next_lead_id += 1
            self._touched.append(key)
            return False

        # Merged duplicate: last write wins, but only where the value is non-null.
        if not existing["dirty"]:
            self._touched.append(key)
        existing["dirty"] = True
        existing["file_id"] = file_id
        if reference is not None:
            existing["external_ref"] = reference
        if any(value is not None for value in pii_values.values()):
            stored_pii = self._load_pii(existing)
            for name, value in pii_values.items():
                if value is not None:
                    stored_pii[name] = value
            existing["pii_dirty"] = True
        for name, value in attribute_values.items():
            if value is not None:
                existing["attributes"][name] = value
        return True

    def flush(self, schemas: dict[str, WorkflowSchema]) -> None:
        """Write buffered leads, applying schema defaults after merging."""
        inserts, updates = [], []
        for key in self._touched:
            state = self._state[key]
            if not state["dirty"]:
                continue
            schema = schemas[state["workflow_id"]]
            attributes = state["attributes"]
            # Defaults apply after merging, to fields that are still null.
            for field in schema.fields:
                if field.default is None:
                    continue
                container = state["pii"] if field.pii else attributes
                if container is not None and container.get(field.name) is None:
                    container[field.name] = field.default
                    if field.pii:
                        state["pii_dirty"] = True
            payload = json.dumps(attributes, sort_keys=True, ensure_ascii=False)
            pii_enc = (
                self._cipher.encrypt(json.dumps(state["pii"], sort_keys=True, ensure_ascii=False))
                if state["pii_dirty"]
                else None
            )
            if state["is_new"]:
                inserts.append(
                    (
                        state["lead_id"], state["workflow_id"], state["phone_bidx"],
                        state["phone_enc"], state["phone_last4"], state["batch_id"],
                        state["external_ref"], pii_enc, payload,
                        state["file_id"], state["file_id"],
                    )
                )
            else:
                updates.append(
                    (
                        state["external_ref"], pii_enc, payload,
                        state["file_id"], state["lead_id"],
                    )
                )
            state["is_new"] = False
            state["dirty"] = False
            state["pii_dirty"] = False

        if inserts:
            self._conn.executemany(
                "INSERT INTO leads(lead_id, workflow_id, phone_bidx, phone_enc, phone_last4, "
                "batch_id, external_ref, pii_enc, attributes, created_file_id, updated_file_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                inserts,
            )
        if updates:
            # A NULL pii_enc here means "unchanged", not "erase it".
            self._conn.executemany(
                "UPDATE leads SET external_ref = ?, "
                "pii_enc = COALESCE(?, pii_enc), attributes = ?, updated_file_id = ? "
                "WHERE lead_id = ?",
                updates,
            )
        self._touched.clear()


def ingest_lead_file(
    conn: sqlite3.Connection,
    writer: LeadWriter,
    schema: WorkflowSchema,
    path: Path,
    file_id: int,
) -> LeadFileCounters:
    """Ingest one lead CSV. The file stem is the batch id."""
    counters = LeadFileCounters()
    batch_id = path.stem
    rejections: list[tuple[int, int, str, str, str]] = []

    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        try:
            headers = next(reader)
        except StopIteration:
            return counters
        mapping = schema.header_map(headers)

        row_number = 0
        for cells in reader:
            if is_blank_row(cells):
                continue  # blank rows are skipped and not counted
            row_number += 1
            counters.lead_rows_total += 1

            parsed = parse_row(schema, mapping, cells)
            if parsed.rejected:
                counters.lead_rows_rejected += 1
                rejections.append(
                    (file_id, row_number, schema.workflow_id, batch_id, parsed.reason)
                )
                continue

            counters.field_warnings += parsed.warnings
            if writer.apply(schema, parsed, batch_id, file_id):
                counters.duplicate_lead_rows_merged += 1

    if rejections:
        conn.executemany(
            "INSERT OR IGNORE INTO rejection(file_id, row_number, workflow_id, batch_id, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            rejections,
        )
    return counters
