# Data contract, version 3

This document describes the ingestion, pricing and reporting boundary implemented
in Phase 3 and reused by the Phase 4 dashboard. Phase 5 retains schema 3 and parser
`claude-jsonl/2`, introduces formula `claude-cost/2` with v1 replay, and freezes adapter
contract `usage-adapter/1`. See [HTTP semantics](phase-4.md) and [operations](phase-5.md).

## Adapter boundary, usage-adapter/1

`AgentAdapter.discover(Settings)` yields `SourceFile(path, agent_type)` for local
read-only input. `parse(SourceRecord)` yields the typed normalized events in
`domain.py`; it must not write storage, price requests, or emit arbitrary raw fields.
The scanner supplies source coordinates, handles file generations/checkpoints and
validates JSON framing. The shared ledger owns identity reconciliation, cross-file
deduplication and current revision selection. Pricing and reports consume that ledger.
Missing evidence stays unknown; agent/model names cannot manufacture provider evidence.
All implementations must obey the identity, privacy, numeric and timestamp invariants
below. Claude is the only implemented adapter; this contract adds no registry, dynamic
plugin system, second provider, or speculative agent-specific schema.

The JSONL reader now consumes a `JSONLSource` descriptor and the companion
`JSONLAdapter` lifecycle protocol in `adapters/base.py`; the normalized event contract
is unchanged. It creates separate discovery/per-file instances, checkpoints only
content-free context, and checks source/event agent namespaces. See the
[modular integration guide](modularity.md) for source and pricing extension boundaries.

## Identity and evidence

`agent_type` is a string shared by the core; only Claude will have an adapter
initially. Database surrogate keys (`session_pk`, `request_pk`, etc.) are distinct
from source-provided identifiers (`session_id`, `request_id`, `message_id`).
An external ID alone is never a global database key. Sessions are unique on
`(agent_type, session_id)`; requests on `(session_pk, dedup_key)`.

`SourceRecord` holds transient raw JSON. An adapter emits only the content-free
`Project`, `Session`, `Turn`, `Request`, `ToolCall`, and `BillableUnit` contracts.
It must not persist `SourceRecord.payload`. `SourceRef` uses a 1-based line,
0-based **byte** offset, file generation, and parser version. Observation JSON
must be serialized from the normalized allowlist, never copied from raw JSON.

The current request is a projection of retained observations. Full rescans must
not add a new observation for the same file generation, offset, and parser version.
Copied/resumed source occurrences need explicit lineage plus matching request
identifiers before merging; shared prompt/cache content alone proves nothing.
Two genuinely separate API requests can reuse the same cached prefix and both
must count. Replays retain source references but charge only the original owner.
Turn IDs belong to user interactions; a tool-result `user` record does not by
itself initiate a new user turn.

The ledger reconciles identity enrichment: an early record lacking a
request ID and a later record with the same message ID plus a request ID are
candidates for the same request, subject to ambiguity checks. Changing the
dedup key must not duplicate the ledger entry. Stable first-observed event time
must not move between reporting days just because a later stream revision wins.

If lineage merges a request that already has receipts, its old request identity is
retained with `superseded_by`. Canonical queries must select `superseded_by IS NULL`.
Receipt payloads and original request/revision references never move; the old receipt
is only marked non-current. Its frozen evidence/input snapshot remains reproducible
even when observation ownership is reconciled with an earlier proven session.

## Tokens and completeness

Counters are non-negative signed-64-bit integers or `None`. Unknown is never
implicitly converted to zero. Booleans and floats are rejected as counters.

```text
input_total = input_uncached + cache_read + cache_write_5m
            + cache_write_1h + cache_write_unknown
all_tokens = input_total + output
```

`reasoning_tokens` is an optional subset of output and cannot be added twice.
Tool-definition overhead already inside native usage cannot be added again.
`cache_write_unknown_tokens` preserves legacy combined writes where TTL is
unavailable. This refines the plan's five priced buckets without mislabelling
observed tokens. For explicit TTL splits, set unknown writes to zero only after
validating that the split agrees with a reported combined total. A mismatching
split is a data-quality error, not permission to add combined and split values.

Missing any exclusive bucket makes `TokenUsage.all_tokens` unknown. A known
legacy write count can have a known token total but uncertain cost. Pricing
completeness (`COMPLETE`, `PARTIAL`, `UNPRICED`) is separate from estimation
reasons (historical catalog, assumed TTL, etc.). `COMPLETE` means all observed
components are priced; it is not a guarantee that local logs capture every call.

## Money and catalog

Money is integer USD nanodollars. Source integer micro-USD converts by multiplying
by 1,000; decimal USD is parsed as `Decimal`, never a float. Preserve the original
reported text/unit. `usd_to_nanos` rejects negative, non-finite, overflowing, or
sub-nanodollar reported values rather than silently rounding. The calculator uses
80-digit Decimal intermediates, preserves exact component/total USD as decimal text,
and rounds once at the request boundary to integer nanodollars using half-even.
Any sub-nanodollar rounding is explicitly labelled. No per-bucket rounding occurs.

Token rate units and billable tool-unit rates are different: a per-1,000-search
price cannot use a per-million-token denominator. Provider, speed, service tier,
region/geography, and context rules are resolved before applying rates.
Unknown rates and unsupported modifiers remain unpriced or partial. Unknown provider
evidence is retained; the product's auto policy may estimate Anthropic-equivalent value.

Receipts retain request revision, winning observation, catalog version, formula
version, resolved price key, rates, rules, estimates, and reported/calculated/effective
cost. Superseding changes the current pointer/state, not historical amounts.
Formula `claude-cost/1` and `claude-cost/2` receipts include a deterministic pricing-policy key,
frozen normalized inputs, catalog checksum and exact coverage/warning data. SQLite
triggers prevent editing/deleting historical payloads. Different pricing policies
produce separate immutable receipts; selecting another current policy requires
explicit `pricing apply --reprice`. Unchanged requests are not silently repriced.
Only a complete, scope-compatible **per-request** reported value may displace a
calculated request value; session/status-line cumulative costs are comparison
evidence, never added to each request.

The full unmodified LiteLLM JSON is bundled with its SHA-256, commit, retrieval
time, and upstream license. Explicit updates archive old snapshots and atomically
select a validated new one; no receipt is silently repriced. `pricing info` verifies
the selected bytes offline;
`pricing apply` resolves/calculates and `pricing verify` recomputes frozen receipts.
Historical effective dates are unknown in this snapshot; no historical exactness is claimed.

Only first-party Anthropic rate rules are implemented initially. Product entry points
(`serve`, `create_app`, `pricing apply`, `apply_pricing`) default to `AUTO_POLICY`:
estimate unknown routing with Anthropic rates and unknown cache TTL with the 5-minute
rate. Known other providers stay unpriced. Both assumptions are labelled; neither
changes normalized evidence. `--cost-mode strict` selects the original policy, where
unknown routing/TTL stay unpriced or partial. The existing assumption flags permit
individual opt-ins from strict mode. `PricingPolicy()` and the pure calculator's
default remain strict for explicit callers and historical replay; frozen policies
fully encode the actual choices, independent of today's product default.
Missing speed/tier/geography default-rule
assumptions are also labelled. Explicit unsupported modifier values cannot fall
back to standard rates. Formula v2 treats the literal `inference_geo="not_available"`
as unavailable evidence and records `ESTIMATED_UNAVAILABLE_INFERENCE_GEO` plus the
standard-modifier assumption; the original string remains in frozen inputs. This is
not evidence of global routing. Formula v1's former unpriced behavior remains available
for exact old receipt replay. JSONL does not currently supply verified request-scoped
reported costs; reported-cost precedence is tested at the normalized contract boundary.

Reports join only current receipts matching the current request revision. Missing
receipts are `pricing_pending`; stale receipt amounts are not included. Aggregates
expose complete/partial/unpriced counts, known cost, token/unknown-counter coverage,
unpriced units and estimates. `priced_tokens`/`unpriced_tokens` describe calculated
rate coverage; `reported_complete_requests` separately describes accepted reported
scope. This distinction allows a complete reported amount even when token-rate
calculation is unavailable. Empty reports explicitly have zero requests.

## Time

Persist UTC epoch **microseconds**, not local date text or floating-point seconds.
Require timezone-aware incoming timestamps. Original timestamp text can be
retained as normalized evidence where needed.

Report inputs are inclusive calendar dates in an explicit IANA timezone. Convert
the first local midnight and the midnight after the last day to UTC, then query
`start <= occurred_at_us < end`. DST days may be 23 or 25 hours. This is why
adding a fixed 86,400 seconds to a UTC start is wrong.

## Storage and migrations

SQLite uses foreign keys, STRICT tables, WAL, FULL synchronous mode, and a bounded
busy timeout. The application owns connection lifetime; no shared global connection.
Migration files run under one `BEGIN IMMEDIATE` transaction with a stored checksum.
Changed applied migrations or a newer unknown schema are rejected. A failed
migration rolls back both schema and migration history. Never modify a released
migration; add the next numbered file.

`doctor` opens an existing database read-only and checks schema/checksums, WAL,
quick integrity, and foreign keys. It never reparses or migrates automatically.

## Evidence sources

- [Python timezone rules and Windows tzdata](https://docs.python.org/3/library/zoneinfo.html)
- [Python SQLite transaction behavior](https://docs.python.org/3/library/sqlite3.html)
- [Claude Code API-request telemetry](https://code.claude.com/docs/en/monitoring-usage)
- [Anthropic pricing and cache/server-tool units](https://platform.claude.com/docs/en/about-claude/pricing)
- [Pinned LiteLLM catalog](https://github.com/BerriAI/litellm/blob/8a1f3568ba2963cc6db140c9ec0000394d45721e/model_prices_and_context_window.json)
- [ccusage streamed usage example](https://github.com/ccusage/ccusage/issues/888)
