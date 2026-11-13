"""Shared constants and filesystem layout conventions."""
from __future__ import annotations

from datetime import timedelta, timezone
from pathlib import Path

#: All report dates are IST dates; naive event timestamps are interpreted as IST.
IST = timezone(timedelta(hours=5, minutes=30))

#: Repository root, resolved from this file so a clean clone works from any cwd.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Provided (unmodified) reference data.
SCHEMA_DIR = REPO_ROOT / "assignment-a-data" / "schemas"
DISPOSITIONS_CSV = SCHEMA_DIR / "dispositions.csv"

DEFAULT_DB_PATH = REPO_ROOT / "ledger.db"

#: Report file names, in the order of the output contract.
REPORT_FILES = (
    "batch_summary.csv",
    "daily_overview.csv",
    "lead_report.csv",
    "rejections.csv",
    "data_quality.json",
)
