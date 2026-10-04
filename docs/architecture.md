# Project structure

This is one local Python application, not a collection of services. Keep modules
grouped by their existing responsibility; add folders only when a real boundary
needs them. The sibling `claude-usage` repository is reference material, not part
of this application.

For the current source/pricing extension points and single composition point, see
[Modular ingestion and pricing](modularity.md). The earlier structural-cleanup
checkpoint below remains a historical record.

## Layout

```text
claude-dashboard/
  pyproject.toml              package, dependencies, CLI entry point, test/lint settings
  uv.lock                     reproducible dependency lock
  README.md                   start commands and operational guidance
  settings.toml               editable source folders, data directory and timezone
  data/pricing/               pinned LiteLLM catalog, metadata and upstream license
  src/claude_metrics/
    __main__.py               python -m claude_metrics
    cli.py                    stable main(), output formatting and exit codes
    commands/
      parser.py               argument definitions and defaults; no execution
      handlers.py             command orchestration using the existing services
    config.py                 shared TOML discovery, precedence and source resolution
    runtime.py                select the source, pricing engine and catalog loader
    application.py            shared initialization and scan/pricing refresh
    domain.py                 content-free normalized records and invariants
    units.py                  exact money and time conversions
    storage.py                SQLite connections, schema checks and migrations
    reports.py                shared aggregates, filters and report queries
    maintenance.py            backups and sanitized support exports
    operations.py             bounded operational logs and safe publication helpers
    adapters/                 source discovery and Claude event normalization
    ingestion/                checkpoints, deduplication/projection and writer locking
    pricing/                  contracts/policy, catalogs, calculation and receipts
    migrations/               immutable, numbered SQL migrations
    web/
      app.py                  app lifecycle, routes and template context assembly
      security.py             local host, origin, scan-token and response-header checks
      queries.py              HTTP query validation and grouped-list pagination
      schemas.py              HTTP request/response contracts
      service.py              compatibility import for application.refresh
      presentation.py         formatting and chart geometry, not accounting
      templates/              Jinja pages and shared layout/macros
      static/                 local stylesheet and refresh-only JavaScript
  tests/
    unit/                     individual contracts, helpers and argument parsing
    integration/              storage, ingestion, pricing, CLI, HTTP and recovery
    golden/                   expected results for the sanitized truth scenarios
    fixtures/claude/          synthetic inputs, never personal transcripts
  scripts/                    optional ccusage comparison tool
  docs/                       current contracts, architecture and historical phase notes
```

`__init__.py` files mark packages; they do not initialize databases or perform scans.
The console entry point remains `claude_metrics.cli:main`. Both
`python -m claude_metrics` and `python -m claude_metrics.cli` remain supported.

## Ownership and boundaries

- **Application settings:** edit root `settings.toml`. `config.py` auto-loads it from
  the current working directory for all CLI commands; an explicit `--config`
  replaces it. CLI flags and dedicated environment variables override its values.
  The dashboard receives the same resolved settings. Restart after edits; no
  personal config is searched for in parent folders or the installed package.
- **CLI:** `cli.py` handles output/errors; `commands/parser.py` builds arguments;
  `commands/handlers.py` delegates to application services. CLI flags, defaults,
  help text and exit behavior stay at this boundary.
- **Web:** `web/app.py` assembles the app and routes. Security and query validation
  have named modules so they are easy to audit. Pages and JSON endpoints consume
  the same reports; do not recalculate costs in templates or JavaScript.
- **Composition:** `runtime.py` selects compatible source/pricing/catalog components;
  `application.py` owns shared startup/refresh. Core token definitions live in
  `domain.py`, and policy defaults/serialization live in `pricing/policy.py`.
- **Accounting:** normalization, ingestion, pricing and reports remain separate.
  Money stays in integer nanodollars; unknown usage must not become a false zero.
  Pricing receipts retain frozen inputs, policy, rates and calculation evidence.
- **Persistence:** use existing connection, migration and writer-lock helpers.
  Preserve the single-writer lock across dashboard scan/pricing and consistent
  read transactions across multi-query reports.
- **Future providers:** add an adapter when actually needed, backed by fixtures.
  Do not spread provider-specific parsing into CLI, templates or shared reports.

## What belongs in the checkout

Source, tests, docs, fixtures, the dependency lock and the pinned catalog are project
assets. Keep the original requirements and phase notes: they document decisions,
not disposable build output. Current documentation starts with the README, this
guide, [the data contract](data-contract.md), and [the UI note](ui-refresh.md).
The phase notes record historical checkpoints and may describe older behavior.

Private runtime state is deliberately separate and ignored:

- `.local/`: the development ledger, catalog archives, operational logs and lock.
- `.backups/`, `.support/`, `.restored/`: user-created operational artifacts.
- `.venv/`, `.tools/`: the installed development environment/runtime; keep these.

Python bytecode, test/lint caches and temporary build output are disposable. Never
treat `usage.sqlite3`, SQLite WAL/SHM files, source logs, user comparison outputs or
backups as cleanup material. Do not run cleanup against the workspace root.

## Structural cleanup verification

The October 2, 2026 cleanup separated CLI parsing/execution and web security/query
helpers. It added no dependencies, schema changes, UI changes or accounting changes.

- Baseline: 279 tests passed. After extraction and compatibility checks: **304 pass**.
- Ruff lint and formatting checks pass.
- Syntax-tree checks matched extracted argument definitions, pricing-policy
  selection, diagnostics, output formatting, HTTP validation, pagination and security
  logic to their originals.
- A wheel was built and exercised outside the checkout with synthetic usage:
  CLI, pages, packaged assets/catalog, host protection and expected totals passed.
- No personal source scan, repricing or data migration was run during the cleanup.

To verify future changes from this folder:

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
uv run --locked pytest -p no:cacheprovider
uv run --locked ruff check --no-cache src tests scripts
uv run --locked ruff format --check --no-cache src tests scripts
```
