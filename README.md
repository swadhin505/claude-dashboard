# Claude Dashboard

A local dashboard with offline ingestion, reproducible pricing, and recovery tools.
The package is `claude_metrics`; the command is
`claude-metrics`. The sibling `claude-usage` repository is reference material only.

See [Project structure](docs/architecture.md) for module responsibilities and the
boundary between source code, generated files, and private runtime data.
Source/pricing extension points and where to change shared behavior are documented
in [Modular ingestion and pricing](docs/modularity.md).

## Start on Windows

Python 3.12+ and uv are required. From this folder:

```powershell
uv sync --locked
uv run --locked claude-metrics serve
```

Edit [settings.toml](settings.toml) for source folders, storage and timezone. Commands
automatically load it when run from this folder. It already points to `.local`, the
standard Claude projects folders and `Asia/Kolkata`. Restart the dashboard after edits.

Open **http://127.0.0.1:8765**. The command initializes the ledger, scans configured
Claude sources, prices new revisions, and serves Overview, Sessions/detail, and
Data health. Stop with Ctrl+C. `--port` changes the local port; LAN/public binding
is deliberately unsupported. `--no-scan` opens retained history without a startup
scan. The Refresh data button runs another scan and pricing pass; there is no watcher.

Auto pricing is now the default: new requests receive labelled **Anthropic
API-equivalent estimates** when routing is unknown; cache writes with unknown duration
use the standard 5-minute rate with an assumption label. Explicit 1-hour writes retain
their own rate. Use `--cost-mode strict` for the previous conservative behavior.
Startup does **not** change already-selected receipts; use the explicit repricing
command below for those. Revised requests preserve their selected policy where known.

Additional CLI commands:

```powershell
uv run --locked claude-metrics init --data-dir .local --timezone Asia/Kolkata
uv run --locked claude-metrics doctor --data-dir .local --timezone Asia/Kolkata
uv run --locked claude-metrics scan --data-dir .local --timezone Asia/Kolkata
uv run --locked claude-metrics pricing info --data-dir .local
uv run --locked claude-metrics pricing apply --data-dir .local
uv run --locked claude-metrics report overview --data-dir .local --timezone Asia/Kolkata --json
uv run --locked claude-metrics pricing verify --data-dir .local --json
$env:PYTHONDONTWRITEBYTECODE = "1"
uv run --locked pytest -p no:cacheprovider
```

The first sync downloads dependencies. Installed application commands work
offline except the explicitly requested `pricing update` command. `.local` is a development database inside this project.
Without a configured data directory (CLI, environment or TOML), the platform
application-data directory is used. Tests use temporary
directories; they never read personal transcripts or the reference database.

`doctor` reports available/missing source roots but does not read transcripts or
initialize the database. `init` creates/migrates only this application's ledger.
Missing Claude roots are warnings, so a fresh machine can still pass setup.
`scan` reads Claude logs but never modifies them. It creates/migrates this app's
ledger and returns counts, coverage, and sanitized diagnostics. Repeat it to ingest
new records; use `scan --full` to reparse available files without deleting retained
history. An unfinished last line waits for a newline on the next scan.

The dashboard is local-only, with packaged templates/styles/scripts and no CDN.
All token/cost logic stays in Python; JavaScript handles refresh status and optional
expand/collapse controls, not accounting or transcript parsing.
The refreshed UI uses compact navigation, clearer cards/tables, and expandable
filters/evidence. Headline costs are rounded to cents; expand **Exact value & coverage**
for full precision. Positive sub-cent amounts show **<$0.01**, not zero. See the
[UI refresh and visual checklist](docs/ui-refresh.md).
Current token counts are not a bill, subscription utilization, or a claim of complete
API coverage. Provider/auth evidence is not guessed from model names or today's login.
No personal transcript was imported as part of the automated acceptance tests.

### Session conversations

Choose **Sessions → Open conversation** to read user prompts, assistant replies,
recorded thinking and expandable tool inputs/results. **Usage & receipts** keeps
the existing accounting view alongside it. Main and subagent source logs can be
selected separately. Conversation content is read locally on demand from the
original JSONL, never copied into the usage database. Missing originals leave
usage history intact but cannot be reconstructed from the ledger.

See [Conversation browsing](docs/conversations.md) for privacy, source-order
semantics, pagination, read limits and the separate module/API boundaries.

## Pricing and reports

`pricing apply` creates immutable, reproducible receipts using the selected LiteLLM
snapshot (bundled by default). It does not fetch new prices. Unknown models and known
unsupported providers remain `UNPRICED`; unsupported components make costs partial. Historical/current-catalog
estimates are always labelled. A report's `known_cost_nanos` is the known portion;
`total_cost_nanos` is null unless coverage is complete. One USD is 1,000,000,000 nanodollars.

Claude JSONL usually does not prove historical provider routing. Auto mode estimates
using Anthropic rates without changing that evidence. To select auto estimates for
already-retained requests, use:

```powershell
uv run --locked claude-metrics pricing apply --data-dir .local --cost-mode auto --reprice
```

This does not change source evidence or claim subscription charges. Known non-Anthropic
providers are not overridden. Add `--from YYYY-MM-DD --to YYYY-MM-DD --timezone Asia/Kolkata`
to restrict pricing to a chosen local date interval. Auto mode labels both provider and
unknown-TTL assumptions. Strict mode leaves unknown routing/TTL unpriced or partial;
`--cost-mode strict --assume-provider anthropic` enables only the provider estimate,
and `--assume-cache-5m` can separately enable the TTL estimate. `--reprice` is required
to change already-current receipts.
Old receipts are retained, including after later usage corrections or lineage merges.

This follows ccusage's practical token-estimation fallback, not every ccusage feature.
Validated request-scoped reported costs still take precedence in the calculator;
the current Claude adapter does not import ambiguous `costUSD` fields or cumulative
session costs as request charges. Our rates remain pinned/offline, not automatically
refreshed or a complete historical schedule. See [ccusage cost modes](https://ccusage.com/guide/cost-modes).

An observed geography of `not_available` is preserved as evidence, but formula v2
treats it as unavailable information, not an unsupported region. Standard-modifier
pricing is explicitly estimated; this does not prove global routing. Strict unknown
provider handling is unchanged. Existing formula v1 receipts still replay unchanged.

Read-only commands:

```powershell
uv run --locked claude-metrics report overview --data-dir .local --json
uv run --locked claude-metrics report sessions --data-dir .local --limit 50 --json
uv run --locked claude-metrics report session --data-dir .local --session-pk 1 --json
uv run --locked claude-metrics report health --data-dir .local --json
```

Overview/session reports support paired `--from`/`--to`, IANA `--timezone`, exact
`--model` and `--project` ID filters. Session/request lists return `next_cursor`;
pass it as `--after`. A scan can invalidate receipts; run `pricing apply` after
scanning. Until then, affected report rows show `pricing_pending`, not stale money.
For a consistent multi-query report, callers use one read transaction (the CLI does this).

## Recovery and maintenance

```powershell
uv run --locked claude-metrics backup --data-dir .local --output .backups/before-upgrade
uv run --locked claude-metrics support-bundle --data-dir .local --output .support/check-1
```

Both commands require a **new** destination directory. Backups include committed WAL
data, the private ledger, and the catalogs needed to replay its receipts. Keep them
private. Support reports contain only allowlisted versions, health flags and counts;
they exclude paths, identifiers, model names, token/cost totals, receipts and raw content.

Catalog updates require a reviewed full upstream commit and expected SHA-256; they
retain older snapshots and never reprice automatically. Restart the dashboard after
updating. See [Phase 5 operations and restore instructions](docs/phase-5.md) before
updating prices or recovering a database.

## Configuration

The single editable application-settings file is [settings.toml](settings.toml).
All CLI commands use the same loader, including `serve`, `scan`, `report`, pricing
and maintenance. `serve` passes the resolved settings to dashboard startup, refresh
and report routes; those routes do not load a separate configuration.

Precedence: command options > `CLAUDE_METRICS_*` variables > selected TOML > built-in
defaults. By default, only `settings.toml` in the **current working directory** is
loaded, if present. No parent-directory or installed-package search is performed.
An explicit `--config` selects a different file **instead of** the local file; the
two files are not merged. A missing explicit file or invalid selected file is an
error, never a silent fallback.

From this folder:

```powershell
uv run --locked claude-metrics doctor
uv run --locked claude-metrics serve
```

When invoking the installed command from elsewhere, point to the same file:

```powershell
claude-metrics serve --config C:/path/to/claude-dashboard/settings.toml
```

Supported settings:

```toml
[app]
data_dir = ".local"
source_dirs = ["~/.claude/projects", "~/.config/claude/projects"]
timezone = "Asia/Kolkata"
```

Paths in TOML are relative to that file; `~` expands to your home folder.
CLI/environment paths are relative to the working directory. `--source-dir` is
repeatable and names a **projects root**, not one project's folder. An explicit
source list replaces discovery; add custom/backup roots to that list as needed.
Environment options: `CLAUDE_METRICS_DATA_DIR`, `CLAUDE_METRICS_SOURCE_DIRS`
(semicolon-separated on Windows), and `CLAUDE_METRICS_TIMEZONE`.
Remove `source_dirs` from the TOML to use automatic source discovery: with no CLI or
`CLAUDE_METRICS_SOURCE_DIRS` override, `CLAUDE_CONFIG_DIR/projects` takes precedence
over the default `~/.claude/projects` and XDG Claude projects roots. Remove `timezone`
to detect the system timezone. An absent optional source root remains a warning.

Restart a running dashboard to apply edits. Changing `data_dir` selects another
ledger; it does not move the existing database. Changing sources does not delete
already-retained usage. Pricing mode (`--cost-mode`) and port (`--port`) remain CLI
options, not TOML keys. This file does not replace the developer integration wiring
in `runtime.py`, and changing settings does not implicitly reprice old receipts.

Local timezone is detected via `tzlocal`; `tzdata` supplies IANA rules on Windows.
Use an explicit timezone if automatic detection fails. No credentials, prompts,
responses, or tool arguments are stored.

## Working layout

```text
settings.toml      editable app paths/timezone, auto-loaded from the working directory
data/pricing/       pinned upstream JSON, metadata, license
src/claude_metrics/
  cli.py           stable entry point, output formatting and exit codes
  commands/        argument definitions and command orchestration
    parser.py      CLI flags, defaults and help text
    handlers.py    setup, scan, pricing, reports and loopback serve operations
  config.py        configuration precedence and source roots
  runtime.py       one composition point for source, pricing engine and catalog loader
  application.py   shared database initialization and scan/pricing refresh
  domain.py        normalized content-free contracts
  units.py         exact money and UTC boundary conversions
  storage.py       database connections and migration runner
  maintenance.py   consistent private backups and content-free support exports
  operations.py    bounded allowlisted local logs and export publication
  adapters/        adapter protocol and Claude-only normalization
  ingestion/       scanner/checkpoints, request projection, OS writer lock
  reports.py       shared date-filtered overview/session/health queries
  migrations/      numbered SQL migrations
  pricing/         engine contract, shared policy, catalogs, calculator and receipts
  web/             HTTP boundary, response schemas, presentation and refresh composition
    app.py         lifecycle, routes and template context
    security.py    local host/origin/token checks and response headers
    queries.py     query validation and grouped-list pagination
    schemas.py     HTTP request/response contracts
    service.py     compatibility import for application.refresh
    presentation.py exact/compact formatting and chart geometry
    templates/     Overview, Sessions/detail, Data health and shared layouts
    static/        local stylesheet and small refresh helper (no frontend build)
tests/             contract, schema, fixture, and CLI tests
docs/              data contract and phase acceptance notes
scripts/           optional offline comparison against an explicit ccusage binary
```

Keep folders focused on existing responsibilities. See
the [architecture guide](docs/architecture.md), [data contract](docs/data-contract.md)
and historical [Phase 5 acceptance](docs/phase-5.md).

Automated HTTP, accounting, security and package checks pass. A visual browser review
is still pending because no browser automation surface was connected during this build;
the current manual checklist is in the [UI refresh note](docs/ui-refresh.md).

## Keep the checkout clean

Only source, tests, documentation, the locked dependency manifest, and the pinned
catalog are project assets. Build output, test/lint caches, bytecode, and temporary
wheel-check environments are disposable and ignored. Run Ruff with `--no-cache`
and tests with the flags above to avoid unnecessary cache files.

Keep `.venv` and `.tools/python` when using the locally installed Python: the virtual
environment depends on that runtime. `.local/usage.sqlite3` is durable application
data, **not** cleanup material. Never manually remove active SQLite WAL/SHM files.
User-created backups and `.local/pricing/` catalog archives are durable data too.
`.local/logs/` holds bounded operational diagnostics, not conversation content.
The tiny `ingestion.lock` file is intentional; deleting a lock file while a process
holds it can undermine single-writer protection. The operating system releases the
actual lock on exit/crash. Do not clean or modify the sibling reference repository.
