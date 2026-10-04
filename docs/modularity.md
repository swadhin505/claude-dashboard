# Modular ingestion and pricing

This refactor adds explicit integration boundaries, not a plugin framework. Production
still supports Claude only. There is no new dependency, database migration, price
download, background watcher or UI change.

## Where to make a change

| Concern | Owner | Used by |
|---|---|---|
| Selected source, pricing engine and active catalog loader | `runtime.py:default_runtime` | Scanning, pricing, startup, CLI, web and support metadata |
| Editable app folders and timezone | root `settings.toml`, resolved by `config.py` | All CLI commands and the settings passed to the dashboard |
| Source discovery and availability | `config.py` | CLI and web; explicit folders still override defaults |
| JSONL source contract and parser factory | `adapters/base.py` | Resumable ingestion |
| Claude record interpretation | `adapters/claude.py` | The selected Claude adapter |
| Exclusive token bucket names | `domain.py:TOKEN_COLUMNS` | Ingestion, calculation and reports |
| Strict policy, auto defaults and CLI policy resolution | `pricing/policy.py` | CLI, web, calculator, receipts and replay |
| Calculation and historical formula replay | `pricing/calculator.py` through `pricing/base.py` | Receipt creation and verification |
| Database initialization and scan/pricing refresh | `application.py` | CLI initialization, web startup and refresh |
| Aggregation | `reports.py` | CLI reports, JSON endpoints and pages |

Changing the default implementation in `default_runtime()` takes effect after process
restart. It does **not** reprice retained receipts or rewrite source evidence.
For ordinary path/timezone changes, edit root `settings.toml`, not `runtime.py`.
The shared loader reads `settings.toml` in the working directory automatically;
`--config` selects a replacement file. CLI flags and dedicated environment variables
still take precedence. Restart the dashboard after edits. Direct Python callers can
use `load_settings()` too; an explicitly constructed `Settings` stays explicit and
does not read files behind the caller's back. Pricing mode and port remain CLI options.
Schema changes still require migrations and compatible readers; changing a field
name in one constant is not a safe schema upgrade.

## Data flow and responsibilities

```text
CLI / web
    │
    ├── config.py: paths, timezone, source status
    └── runtime.py: select compatible components
             │
       application.py: initialization / lock-preserving refresh
             │
       JSONLSource → scanner → normalized events → ledger
                                                  │
                      Catalog + PricingEngine → receipts
                                                  │
                                              reports → UI / CLI
```

The scanner owns bounded JSONL framing, file fingerprints, byte offsets, transactions
and rollback. It no longer constructs a concrete Claude parser or embeds a Claude
source-file SQL namespace. A `JSONLSource` provides the agent type, parser version
and a factory for a fresh stateful adapter per file. Discovery has a separate instance.
Persisted context remains content-free string identifiers; rejected records restore
the previous context. Discovery/record namespace mismatches are rejected.

The existing `AgentAdapter` event contract remains `usage-adapter/1`. Its companion
`JSONLAdapter` describes the context and discovery warnings required by the JSONL
reader. This is not an HTTP, CSV or arbitrary-format reader: those would need their
own ingestion transport while honoring the same normalized contracts.

Receipt persistence depends on a `PricingEngine` protocol: agent identity, current
and supported formula versions, input normalization, calculation and frozen-rate
recomputation. The existing calculator module implements it directly; there is no
wrapper class around every function. Pricing selects only requests in that engine's
agent namespace. Source availability updates are also namespace-scoped.

`Runtime` checks that source and pricing agent identities agree and the current
formula can be replayed. It is an immutable bundle of references, not a mutable global
registry. Tests and integrations can pass it explicitly to `create_app`, `initialize`,
`refresh`, `apply_pricing` and `verify_receipts`. `scan` accepts `runtime.source`.

The catalog loader still returns the verified `Catalog` contract. Catalog storage,
checksums and archives remain in `pricing/catalog.py` and `pricing/store.py`.
Historical verification intentionally uses each receipt's recorded catalog/version,
not today's selected rates. With no explicit catalog it starts from the bundled
catalog; supply `data_dir` to resolve archived versions, or an explicit `catalog`.

## Adding an integration safely

1. Implement a content-free adapter and add synthetic fixtures. Preserve stable
   identities, timestamp units, unknown counters and replay evidence.
2. Supply a distinct parser version and a compatible pricing engine/catalog loader.
   For another Claude JSONL format, reuse the existing Claude engine. For a new agent,
   use its own namespace and validate its provider/billing semantics first.
3. Exercise it by passing an explicit `Runtime`; select it in `default_runtime()` only
   when it is ready. No scanner, page or command-dispatch rewrite is needed for a
   compatible JSONL integration.
4. Keep historical formulas replayable. A changed calculation requires a new formula
   version, not silent mutation of the old one. Existing policies/receipts remain frozen.

One runtime selects **one integration**; it can read multiple configured folders.
This is not yet a user-facing multi-provider router. Mixed-provider operation would
also need deliberate source selection, policy evolution, per-formula audit dispatch,
and reporting/UI decisions. The shared policy and normalized token/catalog shapes
currently represent Claude semantics; do not shoehorn another provider into them.
Unknown formulas fail verification rather than being replayed with the wrong rules.

## Compatibility and verification

Verified October 4, 2026: **322 tests pass** (304 before this refactor), Ruff lint and
format checks pass, and the rebuilt wheel passes outside-checkout startup, fixture
totals, pages/assets, host-security and receipt-replay checks. Temporary package
builds and synthetic ledgers were removed.

- Existing commands, URLs, JSON report shapes and pricing defaults are preserved.
- Schema 3, parser `claude-jsonl/2` and formula `claude-cost/2` are unchanged; v1 replay
  remains supported. No personal data was scanned, repriced or migrated for this work.
- `calculator.PricingPolicy`, `calculator.AUTO_POLICY`, `calculator.canonical`,
  `ledger.TOKEN_COLUMNS` and `web.service.refresh` remain compatible imports.
- Calculation, report and Claude parsing function syntax trees match their originals.
- Tests cover a substituted adapter/engine/catalog loader reaching CLI and web,
  context resumption, namespace separation, pricing scope, missing-source isolation,
  rollback on pricing failure, shared defaults and forbidden core-layer dependencies.
- Changing `DEFAULT_COST_MODE` reaches CLI parsing, web startup and direct pricing
  through the same policy module; the production default remains `auto`.
- Existing golden, security, replay, crash recovery and performance tests remain in place.

## Design references

The design uses explicit composition and small dependency contracts rather than a
container. This follows the separation of component configuration from use described
by [Martin Fowler's dependency-injection article](https://www.martinfowler.com/articles/injection.html)
and the function-oriented approach in [Dependency Composition](https://martinfowler.com/articles/dependency-composition.html).

[Python protocols](https://typing.python.org/en/latest/reference/protocols.html)
let modules/classes satisfy a small interface without inheriting a framework base
class. They document the boundary; fixture and integration tests verify behavior.

We retain SQLite's transactional boundaries and the existing single-writer lock,
consistent with [SQLite's transaction model](https://www.sqlite.org/lang_transaction.html).
One file's ingestion and a pricing batch retain their existing commit/rollback scope;
the full refresh is not falsely described as one atomic database transaction.
