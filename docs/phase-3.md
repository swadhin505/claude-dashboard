# Phase 3 acceptance: reproducible pricing and reports

Status: implemented; **163 tests pass** on Windows/CPython 3.12.11. Ruff lint and
format checks pass. No runtime dependency was added, no personal transcripts were
imported, and the sibling reference repository and pinned LiteLLM bytes are unchanged.

## Delivered

- Pure Decimal calculator with strict provider/model lookup and one explicit
  `anthropic/` prefix normalization rule. No fuzzy model matching or copied price table.
- Separate uncached input, cache reads, five-minute writes, one-hour writes and
  output; reasoning is a subset, not added twice. Unknown TTL remains partial by
  default, with an opt-in, labelled five-minute estimate.
- Catalog-backed whole-request context tiers, batch alternate fields, fast/US
  multipliers, and per-query web-search charges. Unknown or unsupported rules stay
  visible as missing coverage. Multipliers affect token charges, not search fees.
- Immutable receipt payloads with normalized inputs, exact decimal components,
  rates/rules, formula/catalog/policy versions, source evidence and coverage. Only
  current/non-current selection can change. Full replay against the matching catalog
  and independent frozen-rate recomputation are both available.
- Explicit `pricing apply`, `pricing apply --reprice` and read-only `pricing verify`.
  Pricing and ingestion share the same cross-process writer lock.
- Read-only overview, daily/model/project/source grouping, paginated sessions and
  request detail, minimal tool summaries and data-health reports. Date filters select
  request events, not whole session lifetimes. CLI reads use a consistent transaction.
- Additive schema migration 003. Late lineage reconciliation retains priced request
  identities and frozen receipts while excluding inherited usage from current totals.
- Parser `claude-jsonl/2` rejects malformed pricing modifiers/server-unit containers
  instead of silently interpreting them as standard pricing or absent charges.

## Verified gates

- Hand arithmetic: all exclusive token buckets total **1,620,000 nanodollars**;
  the subagent fixture totals **135,000**; two catalog-priced web searches add
  **20,000,000**. Mixed-model session cost equals the two separately priced requests.
- Unknown model/provider/region/speed/tier/geography never silently becomes free
  or standard-priced. Missing alternate one-hour batch rates remain partial.
- Context tests cover exactly 200,000 and 200,001 input tokens, inclusive cache
  threshold basis, whole-request rate selection and models without that premium.
- Fast/geography stacking, TTL assumptions, unknown billable units, counter absence,
  known zero, reasoning inclusion, money overflow and half-even nanodollar rounding.
- Reported/calculated provenance and configurable discrepancy tolerance are tested
  at the normalized input boundary; unverified session-level reported costs are rejected
  as effective request amounts. Actual telemetry ingestion is still deferred.
- Receipt idempotency, explicit policy changes, append-only usage revisions, transaction
  rollback and late replay after pricing. Database triggers reject amount edits/deletion.
- Frozen-rate and catalog-backed verification reproduce every tested receipt.
- Grouped costs/tokens/requests conserve the same selected request set. Partial totals
  expose missing tokens/units, and pending pricing never displays stale receipts.
- Local midnight plus 23/25-hour New York DST days; pagination, empty reports,
  exact filters, query privacy and report read-only behavior.
- A 2,500-request/50-session synthetic run measured approximately **0.59 s** for pricing
  and **0.17 s** for overview on this machine. These are local measurements, not a
  production performance guarantee; no materialized aggregate tables were added.

The locked environment sync and source/wheel builds passed. A separate wheel-only
environment with the pinned runtime dependencies passed `init`, synthetic `scan`,
`pricing apply`, a local-date overview query, `pricing verify` and `doctor` from
outside the source directory: schema 3, one request, 450 tokens, and 1,620,000
nanodollars, with successful receipt replay and catalog/database integrity checks.
After initial dependency fetching, the build and command checks ran offline.
The development database was migrated without scanning personal history.

Temporary npm/ccusage and wheel-check environments, caches and build artifacts were
removed after verification. `.venv`, its `.tools/python` runtime and the durable
`.local/usage.sqlite3` database are intentionally retained.

## ccusage comparison

On 2026-10-01, npm reported **ccusage 20.0.26**. That pinned release was temporarily
installed inside `.tools` with install scripts disabled. The native Windows CLI ran
offline, in calculate mode, against separate copies of all ten synthetic scenarios.
An explicit Claude source root and explicit empty config prevented personal-log/config
discovery. Each case was then scanned and priced by our actual implementation.

Our comparison deliberately selected `assume_provider=anthropic` to compare
API-equivalent values; this is an estimate, not new provider evidence. Unknown TTL
remained unpriced. Tiny binary-float rendering differences are not accounting differences.

| Scenario | Our known USD | ccusage USD | Result |
|---|---:|---:|---|
| Normal | 0.001620 | 0.001620 | 450 tokens agree |
| Streaming revision | 0.001620 | 0.001620 | Final 450 tokens agree |
| Mixed models | 0.001690 | 0.001690 | 480 tokens agree |
| Main/subagent overlap | 0.001755 | 0.001755 | 565 tokens agree |
| Confirmed replay | 0.001620 | 0.001620 | 450 tokens agree |
| Local midnight | 0.000054 | 0.000054 | 6 total tokens agree |
| Legacy unknown TTL | 0.000069 + missing coverage | 0.000099 | 15 tokens agree; ccusage's amount includes the 8 writes at the five-minute rate |
| Unrelated reused IDs | 0.001686 | 0.001620 | Ours retains 460 tokens across two unrelated sessions; ccusage returned 450 |
| Provider/modes fixture | 0.000386 + missing coverage | 0.000380 | 60 tokens agree; the 0.000006 difference matches the observed US-token premium |
| Server units | 0.020120 + missing coverage | 0.000120 | 24 tokens agree; ours also includes two observed searches and flags the unknown unit |

The modes case contains an unknown model: both identify missing model pricing; our
aggregate is explicitly partial rather than treating its zero contribution as a complete
total. These outcomes explain differences on these fixtures only; they do not establish
general superiority over another tool or prove every live transcript variant.

Re-run with an explicitly installed executable:

```powershell
.venv/Scripts/python.exe scripts/compare_ccusage.py --binary C:/path/to/ccusage.exe
```

The script creates and removes temporary fixture copies; it does not install tools,
make network requests, scan personal logs or change application data.

## Boundaries before Phase 4

1. **Provider evidence:** no secret/auth files are inspected, and today's login is not
   retroactive proof. Strict default pricing leaves unknown routing unpriced. Optional
   Anthropic API-equivalent estimation is user-selected and labelled. Known cloud/gateway
   providers remain unsupported/unpriced; this phase does not invent their rate rules.
2. **Rate coverage:** unsupported tier combinations, missing catalog fields and server
   units other than supported web searches stay partial/unpriced. A documented vendor
   price is not silently patched into the pinned JSON. No catalog updater is implemented.
3. **Estimates and precision:** historical schedules are unavailable. Missing standard
   modifier evidence is labelled. Exact USD component text is retained; storage rounds
   once to nanodollars and labels sub-nanodollar rounding. Reported values are not invoices.
4. **Workflow:** `scan` and `pricing apply` are explicit separate steps. New revisions
   invalidate current costs. Phase 4 must compose these services and show pending/busy
   states; browser code must not recalculate money or silently change pricing policy.
5. Windows is verified; Linux/macOS locking still needs CI. The dashboard, HTTP endpoints,
   live telemetry, subscription quota estimates and automatic repricing are not included.

Next: Phase 4's small local dashboard using these shared report services. Keep the
complete/partial/unpriced and estimated labels visible beside every monetary total.

## Sources consulted

- [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing): token,
  cache, mode/geography, batch and server-tool billing semantics. Published pages can
  change; production rate values still come from the pinned snapshot, not this page.
- [Pinned LiteLLM JSON](https://github.com/BerriAI/litellm/blob/8a1f3568ba2963cc6db140c9ec0000394d45721e/model_prices_and_context_window.json):
  exact model entries, rate fields, modifier values and search-query rate bases.
- [ccusage CLI options](https://ccusage.com/guide/cli-options),
  [source-root environment](https://ccusage.com/guide/environment-variables) and
  [JSON coverage behavior](https://ccusage.com/guide/json-output): comparison setup and interpretation.
