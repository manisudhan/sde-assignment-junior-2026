"""SQLite schema, connection setup and the derived-fact views.

Design notes
------------
*Append-only ledger.* ``interaction_event`` is the single source of truth and is
insert-only; ``BEFORE UPDATE``/``BEFORE DELETE`` triggers abort any attempt to
change history. An override is a new row, never an edit of the row it corrects.

*Links are derived, not content.* The lead an event belongs to is a conclusion
we draw, and it can change when a lead arrives in a later drop. Keeping it in a
separate ``event_link`` table is what lets the ledger itself stay strictly
immutable: re-linking inserts links, it never rewrites events. An event with no
link row is an orphan - that is the definition, not a flag someone has to
remember to keep up to date.

*Typed lead attributes in JSON.* Lead columns differ per workflow, so the
workflow-specific fields live in a JSON object while the columns the platform
and the reports actually need (identity, batch, reference, masking) are real
columns with real constraints. One wide table per workflow would mean a
migration per client; EAV would mean a join per field for no benefit, since no
report reads those fields.

*Reports are SQL.* Every derived fact - a call, its effective disposition, a
bucket - is a view over the ledger, so each rule exists in exactly one place and
the aggregation never loads the dataset into memory.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = "1"

DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Disposition catalogue, loaded from dispositions.csv. Kept in the database so
-- "is this code known / terminal / a success" is a join, not a Python lookup.
CREATE TABLE IF NOT EXISTS disposition (
    workflow_id TEXT    NOT NULL,
    code        TEXT    NOT NULL,
    label       TEXT    NOT NULL,
    is_terminal INTEGER NOT NULL CHECK (is_terminal IN (0, 1)),
    is_success  INTEGER NOT NULL CHECK (is_success  IN (0, 1)),
    PRIMARY KEY (workflow_id, code)
) WITHOUT ROWID;

-- One row per source file ever ingested: the idempotency guard and the home of
-- the per-file counters that data_quality.json sums. Counters live here rather
-- than in a running total so re-ingesting a drop cannot double-count.
CREATE TABLE IF NOT EXISTS ingested_file (
    file_id        INTEGER PRIMARY KEY,
    drop_id        TEXT    NOT NULL,
    rel_path       TEXT    NOT NULL,
    kind           TEXT    NOT NULL CHECK (kind IN ('leads', 'events')),
    content_sha256 TEXT    NOT NULL,
    ingested_at    TEXT    NOT NULL,
    lead_rows_total            INTEGER NOT NULL DEFAULT 0,
    lead_rows_rejected         INTEGER NOT NULL DEFAULT 0,
    duplicate_lead_rows_merged INTEGER NOT NULL DEFAULT 0,
    field_warnings             INTEGER NOT NULL DEFAULT 0,
    events_total               INTEGER NOT NULL DEFAULT 0,
    malformed_events           INTEGER NOT NULL DEFAULT 0,
    duplicate_events_dropped   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (drop_id, rel_path)
);

-- A lead is identified by (workflow_id, phone). The phone is never stored in
-- plaintext: phone_bidx is a keyed HMAC used as the join key, phone_enc is
-- AES-GCM ciphertext of the number we would actually dial, and phone_last4
-- exists only so reports can print +91******1234.
--
-- Every other field marked pii:true goes into pii_enc, one AES-GCM blob holding
-- a JSON object, so a workflow can add a PII field without a migration and
-- without any chance of it landing in a plaintext column by accident.
-- attributes holds the non-PII typed fields.
CREATE TABLE IF NOT EXISTS leads (
    lead_id         INTEGER PRIMARY KEY,
    workflow_id     TEXT NOT NULL,
    phone_bidx      TEXT NOT NULL,
    phone_enc       BLOB NOT NULL,
    phone_last4     TEXT NOT NULL CHECK (length(phone_last4) = 4),
    batch_id        TEXT NOT NULL,
    external_ref    TEXT,
    pii_enc         BLOB,
    attributes      TEXT NOT NULL DEFAULT '{}',
    created_file_id INTEGER NOT NULL REFERENCES ingested_file(file_id),
    updated_file_id INTEGER NOT NULL REFERENCES ingested_file(file_id),
    UNIQUE (workflow_id, phone_bidx)
);
CREATE INDEX IF NOT EXISTS idx_leads_batch ON leads(workflow_id, batch_id);

-- Rejected rows are not leads. No PII here by construction: the table has
-- nowhere to put a name or a number.
CREATE TABLE IF NOT EXISTS rejection (
    file_id     INTEGER NOT NULL REFERENCES ingested_file(file_id),
    row_number  INTEGER NOT NULL,
    workflow_id TEXT    NOT NULL,
    batch_id    TEXT    NOT NULL,
    reason      TEXT    NOT NULL,
    PRIMARY KEY (file_id, row_number)
) WITHOUT ROWID;

-- THE LEDGER. Append-only; see the triggers below.
CREATE TABLE IF NOT EXISTS interaction_event (
    event_id          TEXT PRIMARY KEY,
    event_type        TEXT NOT NULL,
    workflow_id       TEXT NOT NULL,
    call_id           TEXT,
    occurred_at_utc   TEXT,
    occurred_at_ist   TEXT,
    occurred_date_ist TEXT,
    phone_bidx        TEXT,
    phone_enc         BLOB,
    phone_last4       TEXT,
    disposition_code  TEXT,
    payload           TEXT NOT NULL DEFAULT '{}',
    source_file_id    INTEGER NOT NULL REFERENCES ingested_file(file_id)
);
CREATE INDEX IF NOT EXISTS idx_event_phone ON interaction_event(workflow_id, phone_bidx);
CREATE INDEX IF NOT EXISTS idx_event_call  ON interaction_event(call_id);
CREATE INDEX IF NOT EXISTS idx_event_type  ON interaction_event(event_type);

CREATE TRIGGER IF NOT EXISTS interaction_event_no_update
BEFORE UPDATE ON interaction_event
BEGIN
    SELECT RAISE(ABORT, 'interaction_event is append-only: events are never updated');
END;

CREATE TRIGGER IF NOT EXISTS interaction_event_no_delete
BEFORE DELETE ON interaction_event
BEGIN
    SELECT RAISE(ABORT, 'interaction_event is append-only: events are never deleted');
END;

-- Derived: which lead an event belongs to. Absence of a row means orphan.
CREATE TABLE IF NOT EXISTS event_link (
    event_id  TEXT    PRIMARY KEY REFERENCES interaction_event(event_id),
    lead_id   INTEGER NOT NULL REFERENCES leads(lead_id),
    link_via  TEXT    NOT NULL CHECK (link_via IN ('phone', 'call'))
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_event_link_lead ON event_link(lead_id);
"""

VIEWS = """
DROP VIEW IF EXISTS v_lead_report;
DROP VIEW IF EXISTS v_lead_current_disposition;
DROP VIEW IF EXISTS v_lead_last_call;
DROP VIEW IF EXISTS v_lead_call_agg;
DROP VIEW IF EXISTS v_call_fact;
DROP VIEW IF EXISTS v_call_disposition;
DROP VIEW IF EXISTS v_call;
DROP VIEW IF EXISTS v_linked_event;

-- Every event we were able to attribute to a lead. Orphans are excluded here,
-- once, so no downstream view has to remember to exclude them.
CREATE VIEW v_linked_event AS
SELECT e.event_id, e.event_type, e.workflow_id, e.call_id,
       e.occurred_at_utc, e.occurred_at_ist, e.occurred_date_ist,
       e.disposition_code, k.lead_id
FROM interaction_event e
JOIN event_link k ON k.event_id = e.event_id;

-- A call exists only if its call.initiated event is present and linked. Should
-- a call ever carry two, the earliest wins, with a deterministic tie-break.
CREATE VIEW v_call AS
SELECT call_id, workflow_id, lead_id,
       occurred_at_utc   AS initiated_at_utc,
       occurred_at_ist   AS initiated_at_ist,
       occurred_date_ist AS call_date_ist
FROM (
    SELECT e.*, ROW_NUMBER() OVER (
               PARTITION BY e.call_id
               ORDER BY e.occurred_at_utc, e.event_id
           ) AS rn
    FROM v_linked_event e
    WHERE e.event_type = 'call.initiated'
      AND e.call_id IS NOT NULL
      AND e.occurred_at_utc IS NOT NULL
)
WHERE rn = 1;

-- Effective disposition: the latest override by occurred_at, else the latest
-- set. The join to `disposition` is what implements "unknown codes are
-- ignored" - an unknown code is simply not a candidate, so the rule falls out
-- of the data model rather than needing a special case.
CREATE VIEW v_call_disposition AS
SELECT call_id, disposition_code
FROM (
    SELECT e.call_id, e.disposition_code,
           ROW_NUMBER() OVER (
               PARTITION BY e.call_id
               ORDER BY CASE WHEN e.event_type = 'disposition.override' THEN 1 ELSE 0 END DESC,
                        e.occurred_at_utc DESC,
                        e.event_id DESC
           ) AS rn
    FROM v_linked_event e
    JOIN disposition d
      ON d.workflow_id = e.workflow_id AND d.code = e.disposition_code
    WHERE e.event_type IN ('disposition.set', 'disposition.override')
      AND e.call_id IS NOT NULL
)
WHERE rn = 1;

CREATE VIEW v_call_fact AS
SELECT c.call_id,
       c.workflow_id,
       c.lead_id,
       c.initiated_at_utc,
       c.initiated_at_ist,
       c.call_date_ist,
       COALESCE(f.connected, 0)        AS connected,
       COALESCE(f.telephony_failed, 0) AS telephony_failed,
       d.disposition_code              AS effective_disposition,
       COALESCE(cat.is_terminal, 0)    AS is_terminal,
       COALESCE(cat.is_success, 0)     AS is_success
FROM v_call c
LEFT JOIN (
    SELECT call_id,
           MAX(event_type = 'call.connected')        AS connected,
           MAX(event_type = 'call.telephony_failed') AS telephony_failed
    FROM v_linked_event
    WHERE call_id IS NOT NULL
    GROUP BY call_id
) f ON f.call_id = c.call_id
LEFT JOIN v_call_disposition d ON d.call_id = c.call_id
LEFT JOIN disposition cat
       ON cat.workflow_id = c.workflow_id AND cat.code = d.disposition_code;

CREATE VIEW v_lead_call_agg AS
SELECT lead_id,
       COUNT(*)              AS total_calls,
       SUM(connected)        AS connected_calls,
       MAX(is_success)       AS ever_success,
       MAX(is_terminal)      AS any_terminal,
       MIN(telephony_failed) AS all_telephony_failed
FROM v_call_fact
GROUP BY lead_id;

CREATE VIEW v_lead_last_call AS
SELECT lead_id, initiated_at_ist AS last_call_at
FROM (
    SELECT lead_id, initiated_at_ist,
           ROW_NUMBER() OVER (
               PARTITION BY lead_id ORDER BY initiated_at_utc DESC, call_id DESC
           ) AS rn
    FROM v_call_fact
)
WHERE rn = 1;

-- The most recent call *that has* an effective disposition, not the most
-- recent call overall: a later call with no outcome must not blank this out.
CREATE VIEW v_lead_current_disposition AS
SELECT lead_id, effective_disposition
FROM (
    SELECT lead_id, effective_disposition,
           ROW_NUMBER() OVER (
               PARTITION BY lead_id ORDER BY initiated_at_utc DESC, call_id DESC
           ) AS rn
    FROM v_call_fact
    WHERE effective_disposition IS NOT NULL
)
WHERE rn = 1;

CREATE VIEW v_lead_report AS
SELECT l.lead_id,
       l.workflow_id,
       COALESCE(l.external_ref, '')           AS external_ref,
       l.batch_id,
       '+91******' || l.phone_last4           AS phone_masked,
       CASE
           WHEN COALESCE(a.total_calls, 0) = 0  THEN 'yet_to_reach_out'
           WHEN COALESCE(a.any_terminal, 0) = 1 THEN 'completed'
           WHEN a.all_telephony_failed = 1      THEN 'in_progress_telephony_failed'
           ELSE 'in_progress_dialed'
       END                                    AS bucket,
       COALESCE(a.total_calls, 0)             AS total_calls,
       COALESCE(a.connected_calls, 0)         AS connected_calls,
       COALESCE(lc.last_call_at, '')          AS last_call_at,
       COALESCE(cd.effective_disposition, '') AS current_disposition,
       CASE WHEN COALESCE(a.ever_success, 0) = 1 THEN 'true' ELSE 'false' END AS ever_success
FROM leads l
LEFT JOIN v_lead_call_agg            a  ON a.lead_id  = l.lead_id
LEFT JOIN v_lead_last_call           lc ON lc.lead_id = l.lead_id
LEFT JOIN v_lead_current_disposition cd ON cd.lead_id = l.lead_id;
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open the ledger database, creating the schema if it is not there yet."""
    path = Path(db_path)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -64000")  # 64 MB page cache
    conn.executescript(DDL)
    conn.executescript(VIEWS)
    set_meta(conn, "schema_version", SCHEMA_VERSION)
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def assert_key_matches(conn: sqlite3.Connection, fingerprint: str) -> None:
    """Refuse to touch a database written with a different PII key.

    The blind index is keyed, so the wrong key would silently fail to match any
    existing lead and quietly create a second copy of every one of them.
    """
    stored = get_meta(conn, "pii_key_fingerprint")
    if stored is None:
        set_meta(conn, "pii_key_fingerprint", fingerprint)
        return
    if stored != fingerprint:
        raise SystemExit(
            "This database was built with a different LEDGER_PII_KEY (stored "
            f"fingerprint {stored}, current {fingerprint}). Ingesting with the "
            "wrong key would duplicate every lead. Set the original key, or "
            "start a fresh database."
        )


def sync_dispositions(conn: sqlite3.Connection, catalogue) -> None:
    """Mirror the disposition catalogue into the database (idempotent)."""
    conn.executemany(
        "INSERT INTO disposition(workflow_id, code, label, is_terminal, is_success) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(workflow_id, code) DO UPDATE SET "
        "label = excluded.label, is_terminal = excluded.is_terminal, "
        "is_success = excluded.is_success",
        [
            (d.workflow_id, d.code, d.label, int(d.is_terminal), int(d.is_success))
            for d in catalogue.dispositions.values()
        ],
    )
