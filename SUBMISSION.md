# Submission — Assignment A

| | |
|---|---|
| **Name** | Manisudhan M |
| **Assignment** | **A (Data)** — Interaction Ledger & Reporting Pipeline |
| **Time spent** | ~8 hours |
| **Stack** | Python 3.11+, SQLite (stdlib), `cryptography` for PII. Nothing else. |
| **Tests** | 150, `pytest`, ~14s, no fixtures on disk |

> **In one paragraph:** leads and events land in SQLite. The event table is
> strictly append-only, enforced by triggers; which lead an event belongs to
> lives in a *separate* `event_link` table, because that is a conclusion that
> changes when a late drop delivers the lead. That one split is what makes
> orphan handling, idempotency and incremental re-linking the same mechanism
> instead of three. PII is a keyed HMAC blind index (so events still match leads
> by phone) plus AES-GCM for anything we must read back. All five reports are
> SQL views, so each rule is defined exactly once.

**A note on repo visibility:** this README asks for a *private* repo, while the
submission form asks for a *publicly accessible* link. I followed the form, since
that is the live submission channel. Nothing sensitive is committed either way —
`.gitignore` covers generated data, reports and the database, and the only key in
the tree is the self-documented development fallback in `crypto.py`.

---

## 1. How to run

From a clean clone:

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
```

Then the pipeline:

```bash
python -m solution.ledger ingest data/drop_1
python -m solution.ledger ingest data/drop_2
python -m solution.ledger report --out reports/
```

And the tests:

```bash
pytest
```

Both subcommands are safe to re-run.

| Option | Effect |
|---|---|
| `ingest data/drop_1 data/drop_2` | several drops in one call |
| `--db PATH` | database location (default `./ledger.db`) |
| `stats` | one-screen summary of what is in the database |
| `LEDGER_PII_KEY` | encryption key. Unset, the pipeline **warns** and uses a committed dev key so a clean clone runs |

---

## 2. Design

```
   leads/*.csv ──► validate · reject · merge ──────────────►  leads
                                                                 │
 events/*.jsonl ──► parse · dedupe ──► interaction_event ◄───────┤
                                       (APPEND-ONLY)             │
                                              └──── relink ──────┤
                                                                 ▼
                                                           event_link
                                                        (derived, insert-only)
                                                                 │
                                   v_call ─► v_call_fact ─► v_lead_report
                                                                 │
                                                      five report files
```

### 2.1 Tables

| Table | Holds | Notes |
|---|---|---|
| `leads` | one row per `(workflow_id, phone)` | `UNIQUE(workflow_id, phone_bidx)` is the identity |
| `interaction_event` | the ledger | **append-only**, enforced by triggers |
| `event_link` | event → lead | derived; absence of a row *is* "orphan" |
| `rejection` | rejected rows | PII-free by construction — no column can hold a name |
| `ingested_file` | one row per file ever ingested | idempotency guard + the per-file quality counters |
| `disposition` | the catalogue | in SQL so "is this terminal?" is a join |

### 2.2 Storage: SQLite

A reviewer runs this from a clean clone; a single file with a stdlib driver means
no DSN, no container, nothing to start. The schema is ordinary SQL with real
constraints, so moving to the provided Postgres is a driver swap plus
`INTEGER PRIMARY KEY` → `BIGSERIAL`. Nothing in the design leans on SQLite.

### 2.3 The ledger is append-only — enforced, not promised

`interaction_event` has `BEFORE UPDATE` and `BEFORE DELETE` triggers that
`RAISE(ABORT)`. An override is a new row; the event it corrects is never touched.
This is the database's job rather than the team's, because *"we never update that
table"* is a rule that survives exactly as long as the next person who has a
deadline.

### 2.4 Links live outside the ledger — the load-bearing decision

Which lead an event belongs to is a **conclusion**, and it changes: an event can
arrive a whole drop before the lead it belongs to. Put that in a column on the
event and every late-arriving lead means an `UPDATE` on the immutable table. So
it lives in `event_link`, and the re-link pass only ever *inserts*. Three things
then fall out for free:

- **An orphan is an event with no link row.** Not a flag someone has to remember
  to clear — which is why `orphan_events` reflects the final state automatically
  rather than via a fix-up pass.
- **Re-linking is incremental *and* idempotent**, because they are the same
  operation: insert links for events that don't have one. After drop 2 it picks
  up exactly the events whose lead has since arrived — 910 of them on seed 42 —
  and does nothing else.
- **A crash is self-healing.** Re-running skips the files and re-links anyway.

Two passes, in order: by `(workflow_id, phone_bidx)`, then by `call_id` for
events carrying no usable phone — which is how a `disposition.override` finds the
lead of its call. The second pass **refuses to link when a `call_id` maps to more
than one lead**: a missing number in a client report is recoverable, a wrong one
is not.

### 2.5 PII: two primitives, because there are two problems

| | Primitive | Why this one |
|---|---|---|
| **Match** events to leads | `phone_bidx = HMAC-SHA256(key, "+91XXXXXXXXXX")` | Deterministic, so it works as a `UNIQUE` key *and* a join key while storing no plaintext. An **unkeyed** hash would be no protection: ~4×10⁹ Indian mobiles is seconds of brute force |
| **Read back** the number and name | AES-256-GCM | Randomised, so two leads named the same produce different ciphertext and the column leaks nothing — which is also exactly why it can't be searched, and why the blind index must exist alongside it |

Both keys are HKDF-derived from one master key, so one env var rotates everything
and the two uses never share key material. `phone_last4` is the only phone
fragment in the clear and exists solely to render `+91******1234`. Every field
marked `pii: true` other than the phone goes into **one encrypted JSON blob**
(`pii_enc`), so a workflow can add a PII field without a migration and with no
chance of it landing in a plaintext column by accident.

### 2.6 Idempotency is per source file

Keyed on `(drop_id, relative path)`, guarded by a SHA-256 of the contents. A file
already in `ingested_file` is skipped outright. The quality counters live on that
same row, so `data_quality.json` is a `SUM` over files actually ingested — not a
running total someone increments, which is the version that double-counts.

### 2.7 Reports are SQL

Every derived fact is a view: `v_call` (a call exists only if its `call.initiated`
is present *and* linked), `v_call_disposition` (latest override, else latest set),
`v_call_fact`, then `v_lead_report` for buckets. Each rule is defined once,
aggregation streams instead of loading the dataset into memory, and Python only
formats and sorts. *"Unknown codes are ignored"* is a `JOIN disposition` — an
unknown code simply isn't a candidate, so the rule falls out of the data model
instead of being a special case to remember in three places.

**Lead attributes are typed JSON.** Lead columns differ per workflow, so
workflow-specific fields sit in an `attributes` object while everything the
platform and reports need — identity, batch, reference, masking — is a real
column with a real constraint. A wide table per workflow means a migration per
client; EAV means a join per field for no benefit, since no report reads those
fields. The pipeline is schema-driven throughout: adding a workflow is a JSON
file plus catalogue rows, not a code change.

---

## 3. Decisions & assumptions

Where the spec didn't decide, I did. Grouped by area.

### Counting

| Decision | Why | Alternative considered |
|---|---|---|
| `orphan_events` and `unknown_disposition_events` are **queried from the final ledger state** | "Is this an orphan?" has no stable answer when the line is read — the lead may arrive next drop | Counting at ingest and reconciling; a second source of truth that drifts |
| An **unlinked** event with an unknown code is an orphan *only* | Spec checks orphan (step 3) before unknown disposition (step 4), so an orphan never reaches step 4 | Counting both; the two fields would overlap |
| `success_leads` counts leads whose call **on that date** succeeded | *"at least one call on that date **whose** effective disposition…"* — "whose" binds to the call | Leads ever successful who were called that day |

### Dispositions

| Decision | Why | Alternative considered |
|---|---|---|
| An **unknown code is ignored entirely**, so the effective disposition falls back to the next valid candidate | *"Count it and ignore it"* = treat it as never having arrived. Letting it blank a known outcome would be *acting* on it | Unknown latest override ⇒ no disposition |
| An override **outranks a `set` by kind, not by clock** — it wins even when earlier | The rule is "latest override, **else** latest set". A human correction is authoritative regardless of when the agent's label was written | Strict chronological order across both types |
| `current_disposition` = latest call **that has one** | Otherwise a later no-answer call blanks a disposition the client can see in the same row | Latest call overall |
| Ties broken by `event_id` / `call_id` descending | `occurred_at` alone is not a total order; any deterministic tiebreak beats one that changes between runs | Leaving it to SQLite's row order |

### Parsing

| Decision | Why | Alternative considered |
|---|---|---|
| `connectivity_rate` = `f"{connected/total:.4f}"` | Matches the worked example (`0.5625`) and the obvious reading. `ROUND_HALF_UP` differs only where the true ratio is exactly …5 at the 5th decimal (e.g. 1/32), and float formatting is the likelier reference behaviour | `Decimal.quantize(ROUND_HALF_UP)` — more correct for money, riskier against a diff |
| Money is `Decimal`, `ROUND_HALF_UP`, stored as a **string** | Floats are the wrong type for currency, and JSON/SQLite would quietly make one. No report exposes it, so this is about not corrupting data we were handed | Storing a float |
| Indian grouping (`1,23,456`) accepted as well as `1,234` | Commas are "thousands separators" per the spec, and this is INR data | 3-digit grouping only |
| 13+ digits is invalid even starting `0`,`91` | The rules are "12 digits starting 91" and "11 digits starting 0", applied literally | Stripping prefixes repeatedly until something valid appears |
| Required field present but unparseable → `missing_required:<field>` | Unreachable with the shipped schemas, but the pipeline needs an answer rather than a crash if a schema gains one | Inventing an `invalid_<field>` reason not in the contract |
| Two columns mapping to one field → **first in file order** wins | Stable under column reordering, no tiebreak rule to explain | Last wins; erroring out |
| Defaults applied at write time, after merging | Equivalent to read time (a later non-null overwrites a default; a later null never overwrites), and keeps the stored row self-describing | Applying defaults in the views |

### Safety rails

| Decision | Why | Alternative considered |
|---|---|---|
| A **changed file** under an already-ingested path **stops the run** | Re-reading double-counts; skipping hides a correction. Both are wrong often enough to matter, and nobody watches a cron job closely enough to notice | Silently skipping, or silently re-ingesting |
| **Wrong `LEDGER_PII_KEY` is refused** via a fingerprint stored in the DB | The blind index is keyed, so a wrong key matches no existing lead and silently creates a second copy of every one. Worst available failure, and invisible in the output | Letting it run |
| Lead files ingested **before** event files in a drop | Most events then link on the first pass. Correctness doesn't depend on it — tests cover events-before-leads explicitly | Requiring the order |

---

## 4. Failure modes

### Handled

| Scenario | Behaviour |
|---|---|
| Re-running `ingest` on an ingested drop | Every file skipped; ledger, counters and all five reports byte-identical |
| Drop-at-a-time vs all-at-once | Identical reports — tested as a property against a second database |
| Events arriving **before** their lead | Adopted by the re-link pass; orphan counts reflect final state (910 adopted on seed 42) |
| Overrides arriving before the call they correct | Same mechanism, via `call_id` |
| Crash mid-run | Each file commits alone; re-linking is a pure function of the ledger, so the next `ingest` repairs it. Tested by deleting `event_link` and re-running |
| Dirty input | Malformed JSON, non-object lines, missing/empty `event_id`, unparseable timestamps, invalid phones, impossible dates, junk money, unknown enums, blank rows, ragged rows, renamed/reordered/extra columns, BOM-prefixed CSVs, duplicate deliveries within and across drops |
| Tampering with history | Refused by the database |
| Wrong encryption key | Refused with a fingerprint mismatch |
| A workflow with no schema | Lead files skipped with a warning, never guessed at |

### Not handled, knowingly

- **No concurrency.** One writer at a time. WAL mode plus per-file transactions mean a second writer blocks rather than corrupts, but nothing coordinates two ingests of the same drop. Postgres + an advisory lock per drop is the fix.
- **No deletes or corrections.** A re-delivered file with changed contents stops the run and needs a human. Done properly, a retraction is a compensating ledger entry, not a `DELETE`.
- **Key rotation re-encrypts nothing.** Changing the key invalidates every blind index. The fingerprint check turns silent duplication into a loud failure, which is as far as I took it; real rotation needs a dual-read window and a re-index job.
- **`phone_last4` is not encrypted.** The output contract requires it, and masking at report time would mean decrypting every lead on every run. A deliberate, documented 4-digit leak.
- **Reports fully recompute.** A streaming read of the whole ledger, not materialised aggregates. Fine at this size.
- **`attributes` values aren't constrained by the database.** Written by validated code, but a hand-written `UPDATE` could put anything there. Postgres `jsonb` + a `CHECK` would close it.

---

## 5. Testing

`pytest` — **150 tests, ~14s**, no on-disk fixtures and no generated data needed.
They drive the real `ingest_drop` and `write_reports`, so they exercise the
idempotency guard and the re-link pass rather than a mock of them.

| File | Covers |
|---|---|
| `test_normalize.py` | All six phone formats and how each fails; day-first dates incl. `31/02` and a real leap day; every currency prefix and Indian grouping; enum casing; header normalisation; all three timestamp forms landing on one instant. **Both IST/UTC date-boundary directions** — an off-by-one day would silently move numbers between rows of `daily_overview.csv` |
| `test_leads.py` | Aliased/reordered/extra columns; blank rows skipped and uncounted; row numbers over non-blank rows only; first-failure-in-schema-order (a row both nameless *and* with a bad phone is a **name** rejection); warnings not counted on rejected rows; identity across phone formats and workflows; batch permanence incl. a **rejected** first row not claiming the batch; merge overwriting only non-null fields, encrypted PII included |
| `test_events.py` | Malformed variants; dedupe within a file and across drops; first delivery wins; orphans from unknown numbers, wrong workflows, invalid phones; override linking via `call_id`; **an event, and an override, arriving before what it belongs to**; unknown codes counted but ignored, incl. a code valid in the *other* workflow |
| `test_dispositions_buckets.py` | Override beating a later `set`; latest of several overrides; unknown override falling back to the valid `set`; a later outcome-less call not blanking `current_disposition`; a call with no `initiated` event not being a call; all four buckets and their precedence; an override moving a lead **into and back out of** `completed` |
| `test_pipeline.py` | Re-ingest changes nothing; counters don't double-count; changed-file refusal; crash recovery; drop-at-a-time == all-at-once as a property; append-only enforcement; wrong-key refusal; empty drops; unknown workflows |
| `test_reports.py` | Exact headers, sort orders, 4-decimal rates, `true`/`false`, `+91******NNNN` masking, empty-not-null fields, no PII in `lead_report.csv` or `rejections.csv`, `data_quality.json` key order, header-only output on an empty DB, stability across two writes |

**Not tested:** concurrent ingests (unsupported), Postgres (unused), `--scale large`
(see §7). There is no golden-file test, because I have no reference output — the
cross-checks in §6 are what I used instead.

---

## 6. Results

Seed 42, `--scale small`, Python 3.12 on a laptop:

| Step | Wall clock |
|---|---|
| `ingest data/drop_1` | 1.6 s |
| `ingest data/drop_2` | 1.5 s |
| `report --out reports/` | 1.2 s |
| re-ingest both (all files skipped) | 0.2 s |

8 files · 1,892 leads · 8,500 ledger events · 8,038 linked · 2,536 calls · 5.1 MB database.

**Cross-checks**, standing in for a golden file:

- `lead_rows_total` recounted straight from the raw CSVs matches (2,057) **and** equals `leads + rejected + merged duplicates` exactly.
- `batch_summary` lead totals = `lead_report` rows = `COUNT(leads)`; every row's four buckets sum to its `total_leads`.
- `daily_overview` call totals = `lead_report` call totals = `COUNT(v_call)`; same for connected calls.
- `rejections.csv` rows = `lead_rows_rejected` = the sum of `rejections_by_reason`.
- The `.db` file contains **no** plaintext name, no 10-digit number and no `+91…` string — only the last 4 digits the masking contract requires. The phone decrypts and re-derives its own blind index.
- Re-ran everything on **seed 7**, which the code had never seen: clean, same invariants hold (2,065 = 1,886 + 114 + 65).

Two things were reassuring: the spec's own worked examples turn up **verbatim** in
the seed-42 output — `0.5625` as the first `daily_overview` connectivity rate, and
`2026-10-14T17:55:24+05:30` as the first lead's `last_call_at`.

### What surprised me in the data

~5% of lead rows are rejected, and a good number are rows whose **phone is
perfectly valid** — rejected for a missing name or policy number. Those leads
still get called, so their events are orphans forever. That, not "numbers we
never uploaded", is most of the 462 final orphans. Operationally it's the
interesting number in `data_quality.json`: **calls we made and cannot bill or
report on.**

---

## 7. If I had another day

1. **Golden-output tests.** Freeze seed-42 reports as fixtures and diff on every change. The §6 checks catch *inconsistency* but would not catch a rule that is uniformly wrong.
2. **Run `--scale large` and fix what it finds.** The design streams and batches, but 140k leads / 600k events is untested and `LeadWriter` preloading a workflow's leads into a dict is the obvious first thing to break. Measure before optimising.
3. **Postgres properly** — real `DATE`/`NUMERIC`/`JSONB`, a `CHECK` on `event_type`, and an advisory lock per drop so two ingests can't interleave.
4. **Production shape for 100k leads/day.** A drop lands in object storage and emits an event; one task per file keyed on the same `(drop, path)` identity so retries are free; re-link once per drop. Reports stay a full recompute until that stops fitting the window, then go incremental per `(workflow, date)` with the ledger as the rebuild source. Backfills are just "ingest an older drop" — the re-link pass already makes late data correct.
5. **Operational visibility.** `data_quality.json` is a report, not an alert. Rejection and orphan rate per batch, with a threshold, would have surfaced those valid-phone-no-name rows on day one.
6. **Decrypt-on-demand CLI** (`lead show <ref>`) with an audit log, so support can read a name without anyone reaching for `sqlite3`.
