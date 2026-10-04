# Phase 1 acceptance

Status: complete. Verified on Windows with isolated CPython 3.12.11.

## Delivered

- Independent `claude-dashboard` package, `src/claude_metrics`, console/module entrypoints.
- Locked dependencies and a local virtual environment; no reference-project imports.
- Explicit configuration precedence, Claude source-root selection, and IANA timezone handling.
- `init`, `doctor`, and offline `pricing info` commands.
- Content-free normalized contracts plus the single `AgentAdapter` protocol.
- SQLite WAL/foreign-key/STRICT setup and one transactional, checksummed migration.
- Twelve core tables including observations and versioned cost receipts; no live-snapshot table yet.
- Full upstream LiteLLM snapshot at commit `8a1f3568ba2963cc6db140c9ec0000394d45721e`,
  SHA-256 `29b1bd7844adb6ed90a4bddac2e8121ecd112af85eccdb9b86a80d0a327052c5`,
  retrieval metadata, and upstream license.
- Content-free Claude-shaped fixtures and ten independent golden scenarios,
  illustrative cost expectations, and byte-level file mutation recipes.
- [Data contract](data-contract.md) explaining identity, token inclusion, unknown
  TTL, privacy, timestamps, exact money units, and future revision handling.

## Verified

| Check | Result |
|---|---|
| Locked dependency sync, offline after installation | Passed |
| Unit, schema, integration, and fixture tests | 66 passed |
| Ruff lint and formatting | Passed |
| Source archive and wheel build | Passed |
| Fresh wheel installation into a separate environment, offline | Passed |
| Installed wheel `init` and `doctor` from outside the project directory | Passed |
| Packaged SQL migrations, JSON catalog, and checksum | Passed |
| Windows IANA timezone, UTC midnight, 23/25-hour DST days | Passed |
| Transaction rollback, idempotent migration, changed/newer migration rejection | Passed |
| Unknown versus zero tokens; cache TTL/reasoning arithmetic | Passed |
| Sibling `claude-usage` git status before/after | Clean, unchanged |

The fixture checks validate hand-authored expectations and structural input cases.
They do **not** claim that ingestion/deduplication or model pricing works yet.
Those production implementations and their end-to-end comparisons are the next
phase gates. No tests were marked skipped or xfailed to claim later work as passing.

## Intentional refinements to the root plan

- Project folder is `claude-dashboard`; Python package/command retain the planned names.
- `init` makes database creation explicit; `doctor` stays diagnostic.
- Runtime dependencies currently cover paths/timezones. Web dependencies arrive in Phase 4.
- Legacy combined cache writes have an unknown-TTL counter so token totals stay
  complete without falsely treating the observed TTL as five minutes.
- `units.py` centralizes precise money/time conversions used by both future adapters and reports.
- Provider and replay-lineage context in fixtures is explicit test metadata;
  Phase 2 must establish which real Claude sources can supply that evidence.

## Begin Phase 2 here

1. Implement `adapters/claude.py` against the protocol and fixture cases.
2. Add validated root discovery, binary JSONL reading, and safe parser diagnostics.
3. Implement observations, identity enrichment, revision selection, and source evidence.
4. Add transactional checkpoints, parser-version/full rescans, and cross-process scan locking.
5. Wire `scan` only once it can compare real output with the golden expectations.

After those steps, prove idempotency, incremental/full equivalence, durable
retention, and main/subagent conservation before beginning request pricing.
