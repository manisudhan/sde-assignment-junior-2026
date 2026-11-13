# AI Usage Disclosure

> **Note to the candidate:** this is an accurate record of the session that
> produced the code, but it has to be *your* disclosure. Read the code, then
> edit the sections below so they describe what you actually did and decided.
> You will be asked to walk through this code and change it live.

## Tools used

Claude (Claude Code, Opus) — one session, driving the editor and the terminal.

## What I used them for

Most of the implementation. Specifically:

- Reading the assignment README, glossary, workflow schemas, disposition
  catalogue and the data generator, and extracting the precise rules from them.
- Writing all the modules under `solution/ledger/`: the normalisers, the SQLite
  schema and views, lead and event ingestion, the re-link pass, the report
  writer and the CLI.
- Writing the 150 tests under `solution/tests/`.
- Drafting `SUBMISSION.md` and this file.
- Running the pipeline, the invariant cross-checks and a fresh-seed run.

## What I wrote or decided myself

The four decisions that shape the solution were mine, chosen from options the AI
laid out with trade-offs:

- **SQLite over Postgres** — a reviewer has to run this from a clean clone, and
  a container is a setup dependency that buys nothing the assignment is graded on.
- **HMAC blind index + AES-GCM for PII**, over hashing-and-discarding or
  deterministic encryption. A voice-agent platform genuinely needs the name and
  the number back, so destroying them dodges the question rather than answering it.
- **Aggregation in SQL views** rather than Python, so each rule exists in exactly
  one place.
- **Scope**: core first, and of the stretch goals only the incremental re-link,
  because it falls out of the idempotency design anyway. No large-scale run, no
  half-finished extras.

_[Add anything else here that was yours — edge cases you spotted, code you wrote
or rewrote, tests you added.]_

## Where the AI was wrong or unhelpful

Three real ones, in increasing order of how much they mattered:

1. **Dead, nonsensical code.** The first draft of `normalize.py` contained a
   `to_utc_key` helper with an `astimezone(... if False else ...)` expression —
   syntactically valid, semantically meaningless, and never called. Noticed on
   reading the file back before running anything; replaced with a three-line
   `utc_key`.
2. **Wrong test expectations, not wrong code.** Several event tests asserted
   five events where the fixture builder emits four. The tests failed, and the
   temptation was to "fix" the pipeline. Checking what `call_events` actually
   emits showed the fixture was right and the assertions were wrong. A test
   failure is not evidence about which side is wrong.
3. **A PII design that only worked by accident.** The first version stored the
   customer name in a single `name_enc` column. That works only because both
   shipped schemas happen to have exactly one PII field besides the phone — add
   an `email` and it silently has nowhere to go, or worse, lands in the
   plaintext `attributes` blob. Changed to one encrypted JSON blob covering
   every `pii: true` field, which is what the rule in the spec actually says.

The AI also initially wanted to count orphans at ingest time and reconcile them
later, which would have produced wrong `data_quality.json` numbers for events
that arrive before their lead. Deriving orphan status from the absence of a link
row instead is what makes the "final state" requirement true for free — that
reframing came out of pushing back on the first design, not out of the first design.

## How I verified AI-generated code

Reading it was the first pass; running it was the second. Beyond the test suite:

- **Invariant cross-checks** against the seed-42 output, which is what I had
  instead of a golden file: `lead_rows_total` recounted straight from the raw
  CSVs; `batch_summary` totals vs `lead_report` rows vs `COUNT(leads)`; bucket
  columns summing to `total_leads`; call totals agreeing across two different
  reports; `rejections.csv` rows matching both quality counters.
- **A plaintext-leak check** — grepping the raw `.db` file for a real name and a
  real phone number taken from the source CSV, confirming neither appears while
  the last 4 digits do, and that the phone decrypts and re-derives its own blind
  index.
- **Append-only enforcement** — attempting `UPDATE` and `DELETE` on the ledger
  and confirming the database refuses both.
- **A fresh seed** (`--seed 7`, which the code had never seen) end to end, with
  the same invariants re-checked.
- **Two spec examples reproduced verbatim**, which is the strongest external
  signal available without a reference implementation: `0.5625` as the first
  `daily_overview` connectivity rate and `2026-10-14T17:55:24+05:30` as the
  first lead's `last_call_at`.
