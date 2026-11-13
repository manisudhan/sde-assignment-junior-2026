"""Workflow lead schemas and the disposition catalogue.

The pipeline is schema-driven: nothing here or downstream knows that
``motor_renewal`` has a ``premium_amount``. Adding a workflow means dropping a
JSON file in ``assignment-a-data/schemas`` and adding rows to
``dispositions.csv`` - no code change.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from solution.ledger.config import DISPOSITIONS_CSV, SCHEMA_DIR
from solution.ledger.normalize import normalize_header

VALID_TYPES = {"string", "phone", "date", "decimal", "enum"}


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    required: bool = False
    pii: bool = False
    reference: bool = False
    values: tuple[str, ...] = ()
    default: str | None = None
    aliases: tuple[str, ...] = ()

    @property
    def match_keys(self) -> set[str]:
        """Normalised header forms that map a CSV column to this field."""
        return {normalize_header(self.name), *(normalize_header(a) for a in self.aliases)}


@dataclass(frozen=True)
class WorkflowSchema:
    workflow_id: str
    description: str
    fields: tuple[Field, ...]

    @property
    def reference_field(self) -> Field:
        for f in self.fields:
            if f.reference:
                return f
        raise ValueError(f"{self.workflow_id}: no field marked reference=true")

    @property
    def phone_field(self) -> Field:
        for f in self.fields:
            if f.type == "phone":
                return f
        raise ValueError(f"{self.workflow_id}: no phone field")

    def header_map(self, headers: list[str]) -> dict[int, Field]:
        """Map column index -> field. Unmatched columns are ignored.

        If two columns map to the same field (a renamed column kept alongside
        its alias), the first in file order wins; the spec does not decide this.
        """
        mapping: dict[int, Field] = {}
        claimed: set[str] = set()
        for index, raw in enumerate(headers):
            key = normalize_header(raw)
            for f in self.fields:
                if key in f.match_keys and f.name not in claimed:
                    mapping[index] = f
                    claimed.add(f.name)
                    break
        return mapping


@dataclass(frozen=True)
class Disposition:
    workflow_id: str
    code: str
    label: str
    is_terminal: bool
    is_success: bool


@dataclass
class Catalogue:
    """Everything the pipeline knows about workflows, loaded once per run."""

    schemas: dict[str, WorkflowSchema] = dc_field(default_factory=dict)
    dispositions: dict[tuple[str, str], Disposition] = dc_field(default_factory=dict)

    def schema(self, workflow_id: str) -> WorkflowSchema | None:
        return self.schemas.get(workflow_id)


def _load_schema(path: Path) -> WorkflowSchema:
    raw = json.loads(path.read_text(encoding="utf-8"))
    fields = []
    for spec in raw["fields"]:
        if spec["type"] not in VALID_TYPES:
            raise ValueError(f"{path.name}: unsupported field type {spec['type']!r}")
        fields.append(
            Field(
                name=spec["name"],
                type=spec["type"],
                required=bool(spec.get("required", False)),
                pii=bool(spec.get("pii", False)),
                reference=bool(spec.get("reference", False)),
                values=tuple(spec.get("values", ())),
                default=spec.get("default"),
                aliases=tuple(spec.get("aliases", ())),
            )
        )
    return WorkflowSchema(
        workflow_id=raw["workflow_id"],
        description=raw.get("description", ""),
        fields=tuple(fields),
    )


def load_catalogue(
    schema_dir: Path = SCHEMA_DIR, dispositions_csv: Path = DISPOSITIONS_CSV
) -> Catalogue:
    catalogue = Catalogue()
    for path in sorted(schema_dir.glob("*.json")):
        schema = _load_schema(path)
        catalogue.schemas[schema.workflow_id] = schema

    with open(dispositions_csv, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            disposition = Disposition(
                workflow_id=row["workflow_id"].strip(),
                code=row["code"].strip(),
                label=row["label"].strip(),
                is_terminal=row["is_terminal"].strip().lower() == "true",
                is_success=row["is_success"].strip().lower() == "true",
            )
            catalogue.dispositions[(disposition.workflow_id, disposition.code)] = disposition
    return catalogue
