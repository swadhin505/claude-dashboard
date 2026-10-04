# Phase 4 — local dashboard

Status: implemented; **207 automated tests pass** on Windows / Python 3.12.11.
Accounting, HTTP, security, lint and package checks pass. **Visual browser acceptance
is still pending**: the browser tool returned no available browser or app surface.
This is a runnable MVP candidate, not a claim that the visual release gate was checked.

This note records the Phase 4 checkpoint. See [Phase 5](phase-5.md) for subsequent
hardening, formula v2, active catalog selection and the current verification results.

## Delivered

- Overview with exact API-equivalent values, exclusive token buckets, request/session
  counts, daily token/cost charts, model/project and main/subagent attribution.
- Sessions and paginated request detail with turn IDs, timestamps, model/provider,
  token types, calculated/reported/effective costs, complete frozen pricing receipts,
  source file/line/byte evidence, and minimal tool success summaries.
- Global Data health with scan history, source availability/checkpoints, current and
  historical receipt counts, parser/formula/catalog versions/checksums, unpriced model
  coverage, estimation reasons, missing units and source diagnostics.
- Paired inclusive local dates, IANA timezone, exact project ID/model filters in the
  URL. Navigation retains filters; changing filters resets the cursor. Health is
  explicitly global. Project/model suggestions are limited to 200; exact IDs still work.
- Explicit empty, unpriced, partial, estimated, pending-pricing, unavailable-source,
  failed-refresh and stale-snapshot states. Stale means no completed scan or a completed
  scan older than 24 hours; it is a freshness hint, not a file-change detector.
- `claude-metrics serve`: initialize, scan, price, then listen on `127.0.0.1:8765`.
  `--port` and `--no-scan` are supported; no remote bind or background watcher.

Only a shallow `web/` package was added. SQLite remains schema 3, the formula remains
`claude-cost/1`, and parser semantics remain `claude-jsonl/2`. No ORM, frontend build,
CDN, chart package, new provider, live telemetry or copied reference-project source.
Simple locally rendered SVG bars replace the planned optional chart library: they
need no dependency and have exact accessible tables immediately below them.

## Refresh and pricing policy

The web refresh holds the **same OS writer lock as both CLI commands** across scan
and pricing. File checkpoints remain individually transactional; pricing remains one
transaction. This is not a claim that the whole scan is one database transaction.
Read-only report transactions can continue against a consistent SQLite snapshot.
Busy writers return HTTP 409 instead of starting a competing job. A failed pricing
pass leaves new revisions visibly pending rather than showing their stale amounts.

Refresh never requests `reprice=True`, fetches prices, or guesses provider evidence.
Current receipts are preserved. Before scanning, it remembers selected request policies
under the lock, so a revised request retains its selected assumptions even when the
selected receipt is older than the most recently created receipt. A single historical
policy can also be recovered after an external CLI scan. If that CLI scan invalidated
selection and several historical policies exist, refresh **does not guess**: the request
stays `pricing_pending` until explicit `pricing apply` establishes the desired policy.

`serve --assume-provider anthropic` and `--assume-cache-5m` affect newly priced requests,
not existing selections. They are explicit labelled estimates, not normalized evidence.
Without these flags, unknown routing and unknown cache TTL retain strict behavior.
Changing existing selections requires the existing explicit `pricing apply --reprice`.

Scanner results now also expose `files_unchanged` (no new bytes to parse) and
`files_processed` (new/appended/reparsed or unfinished-tail processing). No new
database migration was needed: these additional detailed counters are available in
the latest dashboard refresh result for this server lifetime. Durable scan counters
remain those from Phase 2. “Unchanged” does not mean the file was never inspected.
Diagnostics are retained source-pointer/error-code records, not raw malformed lines.

## HTTP and display contracts

| Endpoint | Response / pagination |
|---|---|
| `GET /api/overview` | Same full selected totals as `reports.overview`; grouped arrays paged by offset |
| `GET /api/sessions` | Same session aggregates as `reports.sessions`; cursor is last `session_pk` |
| `GET /api/sessions/{session_pk}` | Session totals plus request page; cursor is last `request_pk` |
| `GET /api/data-health` | Global health; source and incomplete-model arrays paged by offset |
| `POST /api/scan` | Refresh result, busy (409), or sanitized failure (503); no body needed |

Read queries accept `from`, `to`, `tz`, `project`, `model`, `limit` and `cursor`.
Health ignores report scope but validates the query and uses timezone for display.
Default limit is 30, maximum 200. Overview/health arrays share the supplied offset and
return `group_counts` and `next_cursor`; totals always describe the full selection.
Recent diagnostics are an explicitly bounded latest-20 sample. Receipt evidence and
tool summaries remain attached to their selected session/request; they are not raw logs.
Session URLs deliberately use the internal key, not an ambiguous source session string.
The request timeline is stable **ledger order**, with event timestamps shown; it is not
an inferred wall-time/active-time view or an ordering based on arbitrary idle gaps.

Pydantic validates query/response boundaries. Unknown/repeated query keys and invalid
dates, timezones, sizes or keys get 422; unknown sessions get 404. Read connections
are read-only and each multi-query response runs in one transaction. The web layer
calls Phase 3 reports rather than reimplementing aggregation or pricing.

Money remains integer USD nanodollars, formatted on the server with integer division
and exact decimal digits. The JSON API retains integer precision on the wire; external
JavaScript consumers must use a lossless parser for integers above `2^53-1`. This app
does not parse report totals in JavaScript: rendered HTML and SVG come from Python.
The only JavaScript handles refresh transport/loading/error text and reloads the page.

Every affected monetary value includes completeness and estimation badges. Partial
values say “known portion,” unpriced values say “Unpriced,” and unknown token counters
are not presented as a complete zero. Rate coverage and accepted reported-cost scope
remain separate. Complete means all **captured** components are covered, not an invoice
or proof that Claude's local files captured every API call.

## Local security and privacy

- Fixed IPv4 loopback binding; proxy-header trust and access logging disabled.
- Exact `localhost` / `127.0.0.1` Host allowlist with valid optional port; no wildcards.
- POST requires exact same Origin, including port, and a random per-process token
  obtained from the same-origin page. Cross-site fetches/origins are rejected. No CORS.
- CSP allows only local scripts, styles and connections; inline script, external assets,
  frames and base injection are prohibited. Responses are `no-store`, `nosniff`,
  no-referrer and same-origin resource policy. Jinja autoescaping remains enabled.
- FastAPI tracing, metrics, logs, operation spans and environment exporter configuration
  are explicitly disabled. No telemetry SDK/exporter, remote fonts or price fetches.
- Swagger/Redoc pages are disabled because their defaults use remote assets;
  `/openapi.json` remains a local machine-readable schema.

This is not a multi-user security boundary: other local processes/users with access to
the loopback service or database can read it. Do not expose it through a reverse proxy,
port forward or hosted deployment. Paths/project names remain sensitive local metadata.

## Verification

- All previous Phase 1–3 tests plus 44 new web/presentation checks pass: **207 total**.
- All ten sanitized truth scenarios run through the actual HTTP boundary and pages;
  costs, tokens, grouped totals and session/detail responses match shared reports.
- Unknown-provider nonzero usage is visibly unpriced; mixed models, cache TTL,
  non-token units, streaming revisions, overlaps, replay and date boundaries retain
  their established behavior. No personal transcripts were used.
- Valid/invalid date filters, pagination, exact project/model selection, missing roots,
  stale data, unavailable database, sanitized refresh failure, metadata XSS escaping,
  Host/DNS-rebinding rejection, Origin/token rejection and writer-busy behavior checked.
- Policy recovery checks cover reselecting an older receipt and ambiguous external
  scan invalidation. Receipt replay still passes after usage revisions.
- A 2,500-request / 50-session synthetic ledger: HTTP overview, two session pages,
  two request pages and rendered Overview HTML took about **0.62 seconds combined**
  on this machine, after ingestion/pricing. This is not an unlimited-history SLA.
- Source distribution and wheel build successfully. A separate wheel-only environment
  serves the packaged pages/assets and API outside the source directory; the mixed-model
  fixture gives 480 tokens and 1,690,000 nanodollars. Initial dependency fetching needs
  network access; installed dashboard operation is offline.

## Remaining manual visual gate

Run the start command in the README, then check these in a browser before calling the
MVP visually accepted:

1. Overview, Sessions, detail and Data health at desktop and narrow/mobile widths.
2. Date/project/model filtering and Next page retain the chosen timezone and scope.
3. Open a receipt; confirm text/table overflow is contained and source paths are readable.
4. Click Refresh data: loading/disabled state, success reload, and readable error/busy state.
5. Check keyboard focus and charts/tables with both priced and unpriced data.

No additional features or Phase 5 work is needed for this check. Temporary build/test
environments and synthetic preview data are disposable; preserve `.venv`, `.tools/python`
and the user's `.local/usage.sqlite3`. The reference repository and pinned catalog
remain unchanged.

## Primary references checked

- [FastAPI templates](https://fastapi.tiangolo.com/advanced/templates/): Jinja integration
  and locally mounted static assets.
- [Starlette middleware](https://starlette.dev/middleware/): Host-header protection;
  our boundary also validates ports and exact Origin plus a request token.
- [Uvicorn settings](https://www.uvicorn.org/settings/): explicit loopback host and
  disabling forwarded-header trust.
- [FastAPI OpenTelemetry](https://fastapi.tiangolo.com/advanced/opentelemetry/): default
  instrumentation/environment setup, explicitly disabled here for local-only privacy.
- [Starlette test client](https://www.starlette.io/testclient/): current supported
  `httpx2` test transport. Exact installed dependencies are recorded in `uv.lock`.
