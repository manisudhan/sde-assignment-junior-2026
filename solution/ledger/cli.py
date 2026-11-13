"""Command-line entry point.

    python -m solution.ledger ingest data/drop_1
    python -m solution.ledger ingest data/drop_2
    python -m solution.ledger report --out reports/

Both subcommands are safe to re-run. ``ingest`` skips files it has already seen;
``report`` only reads.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from solution.ledger.config import DEFAULT_DB_PATH, DISPOSITIONS_CSV, SCHEMA_DIR
from solution.ledger.crypto import PiiCipher
from solution.ledger.db import connect
from solution.ledger.pipeline import ingest_drop
from solution.ledger.report import write_reports
from solution.ledger.workflow import load_catalogue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m solution.ledger",
        description="Interaction ledger and reporting pipeline.",
    )
    parser.add_argument(
        "--db", default=str(DEFAULT_DB_PATH), help="SQLite database path (default: ./ledger.db)"
    )
    parser.add_argument("--schemas", default=str(SCHEMA_DIR), help="directory of workflow schemas")
    parser.add_argument("--dispositions", default=str(DISPOSITIONS_CSV), help="disposition catalogue CSV")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="ingest one or more drop directories")
    ingest.add_argument("drops", nargs="+", help="drop directories, e.g. data/drop_1")

    report = sub.add_parser("report", help="write the five report files")
    report.add_argument("--out", required=True, help="output directory")

    sub.add_parser("stats", help="print a short database summary")
    return parser


def _stats(conn) -> list[str]:
    scalar = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return [
        f"  files ingested  : {scalar('SELECT COUNT(*) FROM ingested_file')}",
        f"  leads           : {scalar('SELECT COUNT(*) FROM leads')}",
        f"  rejected rows   : {scalar('SELECT COUNT(*) FROM rejection')}",
        f"  ledger events   : {scalar('SELECT COUNT(*) FROM interaction_event')}",
        f"  linked events   : {scalar('SELECT COUNT(*) FROM event_link')}",
        f"  calls           : {scalar('SELECT COUNT(*) FROM v_call')}",
    ]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    catalogue = load_catalogue(Path(args.schemas), Path(args.dispositions))
    conn = connect(args.db)
    try:
        if args.command == "ingest":
            cipher = PiiCipher()
            for drop in args.drops:
                started = time.perf_counter()
                summary = ingest_drop(conn, cipher, catalogue, Path(drop))
                for line in summary.as_lines():
                    print(line)
                for workflow_id in summary.unknown_workflows:
                    print(f"  [warn] no schema for workflow {workflow_id!r}; its lead files were skipped")
                print(f"  elapsed         : {time.perf_counter() - started:.2f}s")
        elif args.command == "report":
            started = time.perf_counter()
            written = write_reports(conn, Path(args.out))
            for name, count in written.items():
                label = "keys" if name.endswith(".json") else "rows"
                print(f"  {name:<20} {count} {label}")
            print(f"  elapsed         : {time.perf_counter() - started:.2f}s")
        else:
            print("\n".join(_stats(conn)))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
