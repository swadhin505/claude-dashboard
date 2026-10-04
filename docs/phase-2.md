# Phase 2 acceptance: Claude ingestion

Status: implemented and verified with synthetic end-to-end fixtures on Windows,
CPython 3.12.11. No personal transcript ingestion was needed for these checks.
The reference `claude-usage` repository is unchanged. No runtime dependency was added.

## Delivered

- Claude JSONL discovery with overlapping-root deduplication and source warnings.
- A content-free adapter for projects, sessions, user turns, requests, subagent
  attribution, minimal tool calls/results, and reported server-tool quantities.
- Session-scoped identity, missing-ID enrichment, revision-aware upserts, and
  retained observations. Final records outrank partial records; otherwise the
  largest exclusive token sum wins, then source line and stable path order.
- Main/subagent counts partition one canonical request set. Copied history uses
  explicit record-specific lineage, never identical text or shared cache prefixes.
- Binary byte checkpoints with file identity, generation, size, mtime, prefix/tail
  fingerprints, parser version and per-file turn context. Newline-incomplete tails
  wait for the next scan. Each file's normalized rows and checkpoint commit together.
- `scan`, `scan --full`, JSON output, sanitized path/line/error-code diagnostics,
  and a small ledger summary with unknown-counter coverage.
- OS-managed cross-process locking, busy/active-run reporting, crash release,
  and recovery of interrupted run metadata.
- Additive schema migration 002; the released migration 001 was not changed.

## Acceptance checks

The production scanner, not a second reference algorithm, is exercised against
all ten Phase 1 golden scenarios and the byte-level file-mutation recipe.
The subagent-overlap fixture's source-pointer expectation was corrected to match
the plan's tie-break rule: equal usage on parent line 2 outranks subagent line 1.
Both observations are retained; no token, request-count or attribution expectation changed.

- Repeated scans and full rescans preserve logical totals and observation identity.
- Appended revisions match a fresh full scan; the original request date is retained.
- Explicit final usage can correct a larger partial record.
- A price-relevant server-unit-only change also increments the request revision.
- Unknown counts remain unknown; invalid booleans/floats/negative/overflowing
  counters, inconsistent cache splits and reasoning greater than output are rejected.
- Missing request IDs can be enriched without duplicating a request; separate API
  retry IDs remain separate even when a message ID is reused.
- Related replay is counted once, including late evidence and child-before-parent
  discovery. Unrelated sessions reusing IDs remain separate.
- Truncation, replacement and source deletion retain durable usage and provenance.
- Parser-version changes reparse unchanged files and supersede older observations
  at successfully reinterpreted source positions; old audit evidence is retained.
- Malformed, invalid UTF-8, duplicate-key and non-finite JSON records do not block
  valid records; failed files do not block other files.
- Simulated transaction interruption and source mutation do not advance checkpoints.
- Tool results do not create extra turns; prompts, response text, tool arguments,
  tool output and raw exception messages do not enter the database.
- A real second process cannot scan while the lock is held; abrupt process exit
  releases the lock without deleting its file.
- Schema 1 databases migrate without losing existing rows; database integrity and
  foreign keys are checked in the end-to-end scenarios.

The verification suite currently has **110 passing tests**, with Ruff lint/format
checks. A 2,500-request/50-session synthetic ledger took approximately 1.80 seconds
to ingest and 0.045 seconds for an unchanged scan on this machine. These are local
measurements, not a production performance promise. Query-plan checks confirm the
time, session/message and session/request indexes are used. No summary-cache tables
or speculative indexes were added.

Locked offline dependency sync, source/wheel builds and an installed-wheel smoke
check also passed. From outside the source tree, the installed command migrated
a schema 1 verification database, imported one synthetic request with 450 tokens,
repeated the scan with zero new/revised rows, and passed `doctor` integrity/catalog
checks. The development `.local` database was migrated to schema 2 without importing
personal logs. Disposable package builds and verification environments were then
removed; the actual development environment and database were preserved.

## Deliberate boundaries and cautions

1. **No costs yet.** Token and billable-unit observations are ready for Phase 3.
   The full pinned LiteLLM catalog remains unchanged. Subscription quota percentages,
   API billing reconciliation and historical-price exactness are not claimed.
2. **Provider remains unknown without request-era evidence.** JSONL model names do
   not prove Anthropic versus a cloud provider/gateway, and today's auth config does
   not prove historical routing. No credentials or auth files are read. Phase 3 must
   implement an explicit, tested evidence policy before showing provider-based costs;
   it must not make every record first-party just to populate a cost card.
3. **Lineage is conservative.** Supported replay evidence is a record-specific
   `forkedFrom` object with session and message UUID plus stable request/message IDs.
   Session-only ancestry and same content are insufficient. Unsupported copied-history
   shapes may remain separately counted until a schema fixture proves a safe rule.
4. **Local logs are not a billing API.** Unknown metadata, unattributed turns, missing
   counters and absent provider information are preserved, not manufactured.
   No activity-time, tool-duration, title extraction or heuristic turn-gap feature
   was added. Tool success is only set when `is_error` is explicitly boolean.
5. **Bounded parsing.** Complete records over 16 MiB are skipped with `line_too_large`;
   partial last lines stay deferred. Supported transcripts are newline-delimited UTF-8.
   Changed files are rescanned; small fingerprints are not a cryptographic whole-file
   integrity guarantee against deliberate middle-of-file edits that also restore metadata.
6. **Durability is intentional.** File cleanup does not subtract known historical
   usage. `--full` is not a destructive database rebuild. Unidentifiable records use
   source position/generation with low confidence; copies without IDs cannot safely
   be deduplicated across replacement files.
7. Windows locking is exercised here. The POSIX `flock` implementation exists but
   still needs a Linux/macOS CI run before claiming verified cross-platform support.

## Start Phase 3 next

Implement provider/model evidence, exact catalog lookup and explicit aliases,
Decimal-based per-request costs, TTL/modifier/server-unit completeness, immutable
cost receipts and timezone-aware reports. Compare the hand-calculated cost fixtures
against the actual pricing implementation. Do not start dashboard design before
those money and coverage invariants pass.

For cleanup boundaries and commands, see the [README](../README.md).

## Evidence consulted

- [ccusage issue 888](https://github.com/ccusage/ccusage/issues/888): observed repeated
  usage snapshots motivate retaining and selecting revisions rather than first-write wins.
- [Claude Code issue 40363](https://github.com/anthropics/claude-code/issues/40363):
  observed per-record fork metadata and duplicate branch occurrences. This is a bug
  report, not a stable vendor schema specification.
- [Python Windows file locking](https://docs.python.org/3/library/msvcrt.html): byte-range,
  non-blocking lock semantics, including locking beyond the current end of a file.
- [Python SQLite transactions](https://docs.python.org/3/library/sqlite3.html): explicit
  transaction ownership under the connection settings used by this project.
