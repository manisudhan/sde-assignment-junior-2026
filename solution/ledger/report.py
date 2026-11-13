"""The five report files.

All aggregation happens in SQL, over the views in ``db.py``. Python's only job
here is formatting and ordering - no business rule is re-implemented in this
module, which is the point: there is exactly one definition of "connected call"
or "terminal disposition" in this codebase, and it is a view.

Orphan and unknown-disposition counts are queried from the final state of the
ledger rather than accumulated during ingest, so they are automatically correct
after a late drop delivers the leads that adopt earlier orphans.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

BATCH_SUMMARY_SQL = """
SELECT workflow_id,
       batch_id,
       COUNT(*)                                            AS total_leads,
       SUM(bucket = 'yet_to_reach_out')                    AS yet_to_reach_out,
       SUM(bucket = 'in_progress_telephony_failed')        AS in_progress_telephony_failed,
       SUM(bucket = 'in_progress_dialed')                  AS in_progress_dialed,
       SUM(bucket = 'completed')                           AS completed
FROM v_lead_report
GROUP BY workflow_id, batch_id
ORDER BY workflow_id, batch_id
"""

# success_leads counts distinct leads whose call *on that date* ended in a
# success disposition - not leads that ever succeeded.
DAILY_OVERVIEW_SQL = """
SELECT workflow_id,
       call_date_ist                                           AS date,
       COUNT(*)                                                AS total_calls,
       SUM(connected)                                          AS connected_calls,
       COUNT(DISTINCT CASE WHEN is_success = 1 THEN lead_id END) AS success_leads
FROM v_call_fact
GROUP BY workflow_id, call_date_ist
ORDER BY workflow_id, call_date_ist
"""

LEAD_REPORT_SQL = """
SELECT workflow_id, external_ref, batch_id, phone_masked, bucket,
       total_calls, connected_calls, last_call_at, current_disposition, ever_success
FROM v_lead_report
ORDER BY workflow_id, external_ref, lead_id
"""

REJECTIONS_SQL = """
SELECT workflow_id, batch_id, row_number, reason
FROM rejection
ORDER BY workflow_id, batch_id, row_number
"""

COUNTER_SQL = """
SELECT COALESCE(SUM(lead_rows_total), 0)            AS lead_rows_total,
       COALESCE(SUM(lead_rows_rejected), 0)         AS lead_rows_rejected,
       COALESCE(SUM(duplicate_lead_rows_merged), 0) AS duplicate_lead_rows_merged,
       COALESCE(SUM(field_warnings), 0)             AS field_warnings,
       COALESCE(SUM(events_total), 0)               AS events_total,
       COALESCE(SUM(malformed_events), 0)           AS malformed_events,
       COALESCE(SUM(duplicate_events_dropped), 0)   AS duplicate_events_dropped
FROM ingested_file
"""

ORPHAN_SQL = """
SELECT COUNT(*) AS n
FROM interaction_event e
LEFT JOIN event_link k ON k.event_id = e.event_id
WHERE k.event_id IS NULL
"""

# A disposition event we *did* attribute to a lead but whose code is not in the
# catalogue for its workflow. An unlinked one is an orphan instead: the spec
# checks orphan before unknown disposition, so the two never double-count.
UNKNOWN_DISPOSITION_SQL = """
SELECT COUNT(*) AS n
FROM v_linked_event e
LEFT JOIN disposition d
       ON d.workflow_id = e.workflow_id AND d.code = e.disposition_code
WHERE e.event_type IN ('disposition.set', 'disposition.override')
  AND d.code IS NULL
"""

REJECTION_REASONS_SQL = "SELECT reason, COUNT(*) AS n FROM rejection GROUP BY reason ORDER BY reason"


def _write_csv(path: Path, header: list[str], rows) -> int:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        count = 0
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def connectivity_rate(connected_calls: int, total_calls: int) -> str:
    """``connected / total`` to exactly four decimals."""
    if total_calls == 0:  # a date only appears when it has at least one call
        return "0.0000"
    return f"{connected_calls / total_calls:.4f}"


def write_reports(conn: sqlite3.Connection, out_dir: Path) -> dict[str, int]:
    """Write all five report files into ``out_dir``. Returns row counts."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, int] = {}

    written["batch_summary.csv"] = _write_csv(
        out_dir / "batch_summary.csv",
        [
            "workflow_id", "batch_id", "total_leads", "yet_to_reach_out",
            "in_progress_telephony_failed", "in_progress_dialed", "completed",
        ],
        (tuple(row) for row in conn.execute(BATCH_SUMMARY_SQL)),
    )

    written["daily_overview.csv"] = _write_csv(
        out_dir / "daily_overview.csv",
        ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate", "success_leads"],
        (
            (
                row["workflow_id"], row["date"], row["total_calls"], row["connected_calls"],
                connectivity_rate(row["connected_calls"], row["total_calls"]),
                row["success_leads"],
            )
            for row in conn.execute(DAILY_OVERVIEW_SQL)
        ),
    )

    written["lead_report.csv"] = _write_csv(
        out_dir / "lead_report.csv",
        [
            "workflow_id", "external_ref", "batch_id", "phone_masked", "bucket",
            "total_calls", "connected_calls", "last_call_at", "current_disposition",
            "ever_success",
        ],
        (tuple(row) for row in conn.execute(LEAD_REPORT_SQL)),
    )

    written["rejections.csv"] = _write_csv(
        out_dir / "rejections.csv",
        ["workflow_id", "batch_id", "row_number", "reason"],
        (tuple(row) for row in conn.execute(REJECTIONS_SQL)),
    )

    quality = data_quality(conn)
    (out_dir / "data_quality.json").write_text(
        json.dumps(quality, indent=2) + "\n", encoding="utf-8"
    )
    written["data_quality.json"] = len(quality)
    return written


def data_quality(conn: sqlite3.Connection) -> dict:
    """Assemble ``data_quality.json`` in the order given by the output contract."""
    counters = conn.execute(COUNTER_SQL).fetchone()
    reasons = {row["reason"]: row["n"] for row in conn.execute(REJECTION_REASONS_SQL)}
    return {
        "lead_rows_total": counters["lead_rows_total"],
        "lead_rows_rejected": counters["lead_rows_rejected"],
        "rejections_by_reason": reasons,
        "duplicate_lead_rows_merged": counters["duplicate_lead_rows_merged"],
        "field_warnings": counters["field_warnings"],
        "events_total": counters["events_total"],
        "malformed_events": counters["malformed_events"],
        "duplicate_events_dropped": counters["duplicate_events_dropped"],
        "orphan_events": conn.execute(ORPHAN_SQL).fetchone()["n"],
        "unknown_disposition_events": conn.execute(UNKNOWN_DISPOSITION_SQL).fetchone()["n"],
    }
