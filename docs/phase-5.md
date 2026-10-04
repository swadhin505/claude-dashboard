# Phase 5 - daily-use hardening

Core hardening is implemented. Optional OTel ingestion and status-line/live bridges
are **not implemented or enabled**. No Claude settings or reference-project files
are modified. The Phase 4 manual visual acceptance gate remains open.

## Follow-up: auto cost estimates selected by the user

After the Phase 5 checkpoint, the user requested ccusage-style cost visibility.
`serve` and `pricing apply` now default to `--cost-mode auto`: unknown routing uses
Anthropic API-equivalent rates and unknown cache-write duration uses the standard
5-minute rate. Both are labelled assumptions. Explicit 1-hour writes, exact model
matching and unsupported-provider/modifier safeguards are unchanged. `--cost-mode strict`
retains the former behavior. No schema or formula change is necessary: these are
existing policies, frozen into each receipt, not changes to the calculation itself.

Startup/refresh preserve existing receipts; changing retained selections still requires
explicit repricing. The user authorized that one-time repricing on the local ledger;
a private pre-change backup is kept under `.backups/`. Do not treat it as run debris.
Original receipt history and normalized evidence are retained.

Follow-up verification: **257 tests pass**, including default CLI/web estimation,
strict-mode selection, legacy cache-write estimation, explicit 1-hour preservation,
reported-cost scope checks, unsupported-rate safeguards, and historical receipt replay.
Ruff lint/format checks pass. The authorized local repricing selected 106 new estimated
receipts with zero unpriced requests in that snapshot; all 212 old/new receipts replay.
The pre-change backup is `.backups/before-auto-pricing-20261001/`. Restart any running
dashboard process to load the new auto default for future requests.

The sections below document the earlier Phase 5 diagnostic checkpoint. References to
strict defaults and data not yet repriced describe that checkpoint, not current defaults.
This is ccusage-style estimation, not full ccusage parity: the app retains pinned
LiteLLM prices and does not import ambiguous source costs or implement ccusage's other modes.

## Why the retained requests were unpriced

The read-only diagnostic snapshot contained 106 canonical requests. All three exact
model names existed in the bundled catalog; missing model prices were not the cause.
All requests had unknown provider evidence, which the strict default refuses to turn
into an Anthropic routing fact. They also reported `inference_geo="not_available"`.

The geography value exposed a second blocker when previewing the explicit Anthropic
estimate policy. Formula `claude-cost/2` now preserves that evidence but treats it as
unavailable information, with labelled standard-modifier assumptions. It does not
interpret the string as proven global routing. Explicit unsupported regions still
fail closed, and unknown providers still require the explicit estimate policy.

Formula v1 receipts remain replayable using their original rules. No automatic
repricing happens on a code/formula/catalog upgrade. The user's retained receipts
were inspected, not changed. Counts describe that diagnostic snapshot, not a permanent
promise about a live database.

Comparison: ccusage's documented auto mode prefers available source costs and otherwise
calculates token-based estimates; calculate mode uses rate catalogs. The imported
reference uses a hardcoded table with model-family fallbacks. Ours uses exact supported
catalog resolution and explicit assumptions, while preserving partial/unpriced coverage.
API-equivalent cost estimates measure usage value, **not a subscription invoice**.

To choose estimates for retained unknown-provider requests, run explicitly:

```powershell
uv run --locked claude-metrics pricing apply --data-dir .local --assume-provider anthropic --reprice
uv run --locked claude-metrics pricing verify --data-dir .local --json
```

At the original checkpoint, future dashboard runs needed `--assume-provider anthropic`.
After the auto-default follow-up above, that flag is no longer required.
Existing selections and known non-Anthropic evidence are not overridden by the startup
flag. Unknown cache TTL remains partial unless separately choosing `--assume-cache-5m`.

## Backup and restore

```powershell
uv run --locked claude-metrics backup --data-dir .local --output .backups/before-upgrade
```

The destination must not exist or lie inside configured Claude source roots. The
command holds the shared writer lock, checks the ledger, uses SQLite's backup API
to include committed WAL data, and publishes a single-file database, referenced and
active catalog snapshots, an active pointer, and a SHA-256 manifest. It never copies
the live database file alone. A failed export does not publish a partial destination.
The database, source coordinates, project/session identifiers and usage are private;
**do not share a backup as a support bundle**. Transcripts and application configuration
are not included. Configure timezone and source roots separately after restoration.

Restore into a **new** application-data directory; never overwrite the working ledger:

1. Stop application writers. Preserve the original data and an untouched backup copy.
2. Copy the entire backup directory into a new restore directory. Check each file against
   `manifest.json`'s `files_sha256` (PowerShell `Get-FileHash -Algorithm SHA256` suffices).
   Validate before running `init`, since initialization can change the database file.
3. Initialize the restored copy to enable WAL and apply supported migrations:

   ```powershell
   uv run --locked claude-metrics init --data-dir .restored --timezone Asia/Kolkata
   uv run --locked claude-metrics doctor --data-dir .restored --timezone Asia/Kolkata
   uv run --locked claude-metrics pricing verify --data-dir .restored --json
   uv run --locked claude-metrics serve --data-dir .restored --no-scan --timezone Asia/Kolkata
   ```

4. Inspect totals/receipts before enabling scans against the original source roots.
   A rescan of unchanged files must not add requests. Keep the original ledger until
   satisfied. Roll back a failed upgrade using the pre-upgrade app version and a separate
   pre-upgrade backup, not by editing migrations or downgrading the schema in place.

No automatic restore/delete command is provided. Never manually delete active WAL/SHM
or lock files. Hard process termination rolls back the active SQLite transaction;
previously committed files remain durable, and the next scan marks an interrupted run
failed before continuing. This is per-file scan atomicity, not whole-scan atomicity.

## Content-free support and local logs

```powershell
uv run --locked claude-metrics support-bundle --data-dir .local --output .support/check-1
```

The new directory contains only `support.json`: application/runtime/parser/formula
versions, catalog checksum, database health flags, table counts and scan-state counts.
It excludes paths, source IDs, model names, token/cost totals, receipts, diagnostics,
raw content and operational logs. It still reveals coarse activity counts; review
the file before sharing. Exports do not upload anything.

`data-dir/logs/operations.jsonl` records only fixed event names, UTC timestamps and
allowlisted nonnegative counts/durations. At 1 MiB it rotates to two older files,
retaining roughly 3 MiB plus a record. Raw exceptions, paths, model/session IDs and
content cannot enter this log. Logging failure does not undo committed accounting.

## Explicit catalog updates

Normal scans, reports, pricing and startup never fetch rates. Review an immutable
LiteLLM commit and calculate the SHA-256 of its original JSON bytes before updating:

```powershell
uv run --locked claude-metrics pricing update --data-dir .local --commit <full-40-character-lowercase-commit> --sha256 <expected-64-character-lowercase-sha256>
uv run --locked claude-metrics pricing info --data-dir .local --json
```

Replace the placeholders; they are not literal shell arguments. A checksum supplied
from the same untrusted source is not independent authenticity verification.

The updater only downloads the fixed GitHub raw LiteLLM catalog path over HTTPS.
It rejects redirects, encoded/truncated/oversize responses, invalid JSON, duplicate
keys, invalid metadata and catalogs without a supported Anthropic per-token rate card.
It caps payloads at 20 MiB, uses a 15-second socket timeout and checks an overall
60-second deadline between reads (an in-progress socket read can overrun that deadline).
Environment proxies are not used; networks requiring a proxy may reject this download.

Validated original JSON bytes and metadata are archived under
`data-dir/pricing/litellm-<commit>/`. Existing versions are never overwritten. The
bundled MIT license is retained; a change to upstream licensing requires review,
not an assumption by the updater. Download failure leaves the active catalog alone.
The active pointer is replaced only after archives are written and validated.
Malformed pointers or missing archives fail closed, not back to hidden fallback rates.

Restart an already running dashboard after an update: its catalog is pinned for its
process lifetime. Existing selected receipts remain untouched. Only explicit
`pricing apply --reprice` selects new receipts; old receipts retain their catalog/formula
and can be fully replayed with archived bytes. No automatic historical price claim is made.

Publication is process-crash safe through staging and rename/replace. It is not a
guarantee against hardware failure or power loss on every filesystem. Interrupted
updates may leave an unused archive or staging directory; do not remove referenced
catalogs. Backups are the recovery boundary. User data is never automatic cleanup material.

## Verification and scope

- Subprocess hard exits test lock release, rollback of uncommitted checkpoints/rows,
  stale-run recovery, and rollback of partially inserted pricing receipts.
- A deliberately failing migration proves schema/data rollback and preservation of
  receipt history. The v1-to-v2 formula transition also preserves and replays receipts.
- Live-WAL backup/restore checks include checksum verification, receipt replay,
  idempotent rescans and identical totals; competing writers and partial exports fail safely.
- Seeded property-style tests cover duplicate permutations, malformed JSONL, invalid
  counters, Unicode/special-character paths, and 500 timezone/date boundary cases.
  These are deterministic randomized checks, not an exhaustive proof or coverage-guided fuzzer.
- Catalog transport/failure tests mock network responses. Catalog archives, checksum
  rejection, immutable-version collisions, failed pointer publication and no automatic
  repricing are exercised end to end with synthetic ledgers.
- Support/log tests check field allowlists, private sentinel exclusion, rotation and
  tolerance of logging failures. HTTP tests retain Phase 4 accounting/security coverage.
- Adapter contract `usage-adapter/1` is frozen in `adapters/base.py` and
  [the data contract](data-contract.md). Only Claude is implemented. No new dependency,
  speculative plugin framework, second adapter, OTel collector or status-line mutation.

Final verification on Windows / Python 3.12.11:

- **246 tests passed**; Ruff lint and format checks passed. No dependency added in Phase 5.
- A 2,500-request / 50-session fixture: first scan **2.042 seconds**, unchanged rescan
  **0.049 seconds** (zero parsed records); combined overview, paginated session/detail
  queries and rendered Overview HTML **0.654 seconds**. Local measurements, not an SLA;
  no new optimization or speculative index was justified by these results.
- Source distribution and wheel built. A fresh wheel-only environment, outside the
  source package, passed synthetic ingestion, pricing, private backup, support export,
  restored initialization and receipt replay. Its HTTP server returned 200 for all
  four pages, packaged CSS/JS and overview/health APIs; fixture totals remained
  **480 tokens / 1,690,000 nanodollars**. This is HTTP acceptance, not visual QA.
- Read-only full replay of the user's **106 retained receipts** passed without changing
  their pricing selections. The reference repository and bundled catalog bytes remain
  unchanged. Network update failure paths were mocked; no real catalog replacement
  was performed on the user's data.
- Initialization now observes the same writer lock as scan, pricing, catalog activation
  and backup; a competing initialization fails without modifying receipts.

The [Phase 4 manual browser checklist](phase-4.md#remaining-manual-visual-gate) remains
required: no browser automation surface was available during this work.

## Primary references

- [ccusage cost modes](https://ccusage.com/guide/cost-modes): source-cost versus calculation policy.
- [Anthropic usage-report geography](https://platform.claude.com/docs/en/api/beta/organization/usage_report/retrieve_messages)
  and [data residency](https://platform.claude.com/docs/en/manage-claude/data-residency):
  unavailable geography is not evidence of a specific routing choice.
- [Python SQLite backup API](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)
  and [SQLite WAL](https://www.sqlite.org/wal.html): consistent backup versus copying a live DB alone.
- [Python replace semantics](https://docs.python.org/3/library/os.html#os.replace): same-filesystem publication.
- [LiteLLM cost maps](https://docs.litellm.ai/docs/proxy/custom_model_cost_map): catalog-based rates.
