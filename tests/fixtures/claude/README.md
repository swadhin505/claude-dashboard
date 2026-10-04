# Claude structural fixtures

All records are synthetic and content-free. They retain Claude-shaped identifiers,
usage fields, timestamps, sidechain flags, and empty message content. They contain
no exported user transcript, credentials, prompt text, response text, or tool output.
Model strings exercise lookup behavior; their presence is not a price guarantee.

The raw shape follows the reference scanner/tests and the streamed example in
[ccusage issue 888](https://github.com/ccusage/ccusage/issues/888). Cache TTL and
server-tool fields follow [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing).
Provider evidence in `tests/golden/claude-cases.json` remains explicit future-pricing
test context; it does not invent a corresponding Claude JSONL field. In Phase 2,
the replay fixture now uses record-specific `forkedFrom.sessionId` and `messageUuid`,
as observed in [Claude Code issue 40363](https://github.com/anthropics/claude-code/issues/40363).
That issue is empirical schema evidence, not a guaranteed public transcript API.

Each golden case lists its own input files: these files are **not** one dataset
to scan together. Winner line numbers and totals were specified independently
of any production parser. Phase 2 compares the real scanner's counts and tokens
to those values. Price-coverage expectations remain for Phase 3.

- `normal`, `streaming`: one request, including early and late usage snapshots.
- `missing-request-id`: message-only identity and unknown cache-write TTL.
- `mixed-models`: two models in one session.
- `parent`, `subagents`: a duplicated subagent request with distinct main usage.
- `resumed`, `unrelated`: proven lineage replay versus unrelated reused IDs.
- `modifiers`: standard/fast/US inference, unknown provider, unknown model.
- `server-tools`: reported web searches and an intentionally unknown billed unit.
- `midnight`: records immediately before/at midnight in Asia/Kolkata.
- `malformed`: one valid record and two invalid/truncated JSON records.
- `file-mutations.json`: exact append, partial-line completion, truncate, and
  replacement operations; tests materialize these into temporary byte streams.

`cost-examples.json` uses fixed illustrative rates solely as hand-calculated test
expectations. They are never imported into application pricing.
