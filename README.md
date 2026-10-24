# Claude Dashboard

**A clearer view of your Claude Code usage.**

Explore tokens, estimated costs, and conversations in a local dashboard built from your Claude Code logs. Your data stays on your machine, with no API key or frontend build required.

## What you can explore

- **Usage at a glance** — daily activity, input/output and cache tokens, plus breakdowns by model and project.
- **Session details** — filter usage by date, model, or project and inspect individual requests and pricing receipts.
- **Conversations** — browse prompts, replies, recorded thinking, and expandable tool calls, with separate main and subagent logs.
- **Data health** — check source availability, ingestion diagnostics, and pricing coverage.
- **Reproducible estimates** — calculate costs from a bundled price catalog and retain earlier receipts when repricing.

## Quick start

You'll need **Python 3.12+**, **uv**, and local Claude Code logs to see your usage.

From the project directory:

```sh
uv sync --locked
uv run --locked claude-metrics serve
```

Open **http://127.0.0.1:8765**. Startup scans your configured logs and prices new usage. Click **Refresh data** to pick up later activity; there is no background watcher. Stop the server with `Ctrl+C`.

The first sync downloads dependencies. After setup, the dashboard works offline. Only the explicit `pricing update` command fetches new prices.

> No usage showing? Run `uv run --locked claude-metrics doctor` to check your source folders, then review the configuration below.

## Make it yours

Edit [settings.toml](settings.toml), which is loaded automatically when you run commands from the project directory:

```toml
[app]
data_dir = ".local"
source_dirs = ["~/.claude/projects", "~/.config/claude/projects"]
timezone = "Asia/Kolkata"
```

- Set `source_dirs` to your Claude **projects roots**, rather than individual project folders. Missing roots produce warnings.
- Change `timezone` to your IANA timezone, or remove it to use your system timezone.
- Paths are relative to the settings file; `~` expands to your home folder. Restart the dashboard after edits.

Settings priority: **CLI flags > `CLAUDE_METRICS_*` environment variables > TOML > defaults**. From another directory, pass `--config /path/to/settings.toml`.

Use `serve --port 9000` for another port, or `serve --no-scan` to open retained history without a startup scan.

## Understanding costs

Costs are **API-equivalent estimates**, not your subscription bill or remaining plan allowance. They reflect the logs available to this app and a pinned LiteLLM price catalog, which is not automatically updated.

The default `auto` mode uses labelled Anthropic-rate estimates when provider routing is unknown, and a 5-minute cache-write rate when the duration is unknown. Use `serve --cost-mode strict` to leave those cases unpriced or partial. Unknown models and unsupported providers remain unpriced.

Existing receipts are preserved. To explicitly recalculate retained usage with auto estimates:

```sh
uv run --locked claude-metrics pricing apply --cost-mode auto --reprice
```

## Useful commands

Run these from the project directory. Each command also supports `--help`.

| Task | Command |
| --- | --- |
| Check setup | `uv run --locked claude-metrics doctor` |
| Scan local logs | `uv run --locked claude-metrics scan` |
| Price newly scanned usage | `uv run --locked claude-metrics pricing apply` |
| Export an overview as JSON | `uv run --locked claude-metrics report overview --json` |
| Verify stored pricing receipts | `uv run --locked claude-metrics pricing verify` |
| Back up the ledger and catalogs | `uv run --locked claude-metrics backup --output .backups/manual-1` |

When using the CLI, run `pricing apply` after `scan` to update costs. Backups require a new destination directory each time.

## Your data stays local

The server binds only to `127.0.0.1` and serves its own styles and scripts. Scanning never modifies your Claude logs.

Usage metadata lives in a local SQLite ledger. Conversation content is read from the original logs on demand and is not copied into that database. If the originals are missing, retained usage remains available, but conversations cannot be reconstructed.

Treat `.local/` and your backups as private, durable data. Changing `data_dir` selects a different ledger; it does not move the existing one.

## Development and further reading

Built with **Python, FastAPI, Jinja2, and SQLite**. Application code lives in `src/claude_metrics/`, with tests in `tests/`.

```sh
uv run --locked pytest -p no:cacheprovider
uv run --locked ruff check --no-cache .
```

- [Architecture](docs/architecture.md) — project layout and module responsibilities.
- [Conversation browsing](docs/conversations.md) — supported content, privacy, and read limits.
- [Data contract](docs/data-contract.md) — usage records and accounting semantics.
- [Extending ingestion and pricing](docs/modularity.md) — adapters and shared behavior.
- [Operations and recovery](docs/phase-5.md) — backups, restore steps, and catalog updates, with historical implementation notes.
