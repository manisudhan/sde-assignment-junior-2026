# Interaction Ledger & Reporting Pipeline — Assignment A

```
solution/
└── ledger/
    ├── cli.py         argparse entry point (ingest / report / stats)
    ├── config.py      IST, repo-relative paths, report file names
    ├── crypto.py      PII: HMAC blind index + AES-256-GCM, both from one HKDF master key
    ├── workflow.py    lead schemas and the disposition catalogue
    ├── normalize.py   phone / date / decimal / enum / header / timestamp rules
    ├── db.py          SQLite schema, append-only triggers, the derived-fact views
    ├── leads.py       lead ingestion: validate, reject, merge
    ├── events.py      event ingestion into the ledger: malformed, dedupe
    ├── linking.py     the re-link pass (by phone, then by call)
    ├── pipeline.py    drop orchestration and per-file idempotency
    └── report.py      the five report files, aggregated in SQL
```

## Run it

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

python -m solution.ledger ingest data/drop_1
python -m solution.ledger ingest data/drop_2
python -m solution.ledger report --out reports/
pytest
```

Both subcommands are safe to re-run; `ingest` accepts several drops at once
(`ingest data/drop_1 data/drop_2`). `--db` chooses the database (default
`./ledger.db`), and `stats` prints a one-screen summary of what is in it.

## PII

Set `LEDGER_PII_KEY` to a real key for anything but synthetic data. Unset, the
pipeline warns and falls back to a committed development key, because the blind
index has to be stable across runs — a per-run random key would make every
re-ingest create a second copy of every lead. The key's fingerprint is stored in
the database and checked on every ingest, so opening a ledger with the wrong key
fails loudly instead of silently duplicating.

See `SUBMISSION.md` for the data model, the decisions behind it, and what is not
handled.
