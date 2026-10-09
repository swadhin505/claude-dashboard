# Browse a Claude Code conversation

Open **Sessions → Open conversation**, or choose **Conversation** beside a
session's **Usage & receipts** view. Prompts and replies are readable in source
order. Expand a tool to see its recorded input and matched outputs. Recorded
thinking, injected context and message identifiers are collapsed separately.
The page works without JavaScript; the optional buttons expand/collapse all tools
on the current page.

When several source files are linked to a session, **Choose a log** switches
between them. Main files are listed first; files in `subagents/` or named
`agent-*.jsonl` are labelled subagent logs. Sources are not merged, and selecting
a source resets pagination. A page contains up to 25 displayed messages. Use
**Latest page** to reach the end of the bounded read.

## Privacy and accounting

The original JSONL is read on demand. No transcript content is written to SQLite,
cached on disk, included in support exports, or sent to an external service. The
browser necessarily receives the displayed content, which may contain secrets.
All responses retain the existing localhost/origin checks, no-store headers and
restrictive content security policy. Text is escaped and displayed literally;
commands, HTML, Markdown links and code in transcripts are never executed.

The existing usage ingestion, pricing, receipt history, report contracts and
database schema are unchanged. Conversation reads are independent of report
filters and can include new messages written since the last usage scan. Message
counts in this view are not billable-request counts. Use **Usage & receipts** for
accounting evidence, not transcript content.

A missing or inaccessible original log produces an unavailable state. Restoring
the original under a configured source folder makes browsing possible again;
retained usage is still available even without it. There is no transcript backup
in the ledger. Logs without a retained session/source association are not a new
discovery mechanism: the normal scan still supplies the session list.

## Interpretation and explicit limits

- Only Claude user, assistant and system records for the selected `sessionId`
  are displayed. Nested progress duplicates are ignored. Explicit injected
  context and compaction summaries are labelled, not mistaken for user prompts.
- Consecutive assistant snapshots with compatible message/request/stream IDs
  and parent linkage coalesce. Cumulative text grows in place; distinct additive
  blocks remain in order. A completed reply does not absorb later text as a
  cumulative continuation.
- Tool results use `tool_use_id`, scoped to the source and agent stream. UUID
  ancestry selects the matching preceding call when available. Without parent
  evidence, only a unique preceding call is accepted. Ambiguous or orphaned
  results stay visible. Only explicit `is_error: false` is labelled success;
  absent status is labelled recorded. Missing output never implies running or
  successful execution. Multiple recorded outputs are retained.
  UUIDs reused ambiguously cannot prove a match; ancestry walks stop after 1,024
  links with an explicit notice rather than spending unbounded time on a bad log.
- Retries and forks remain in source order with identifiers and a branch notice
  when detected. This is not AgentsView's reconstructed branch/session tree.
- Images, binary attachments and unsupported blocks get placeholders. External
  persisted-output files are not opened; their references remain visible as text.
  Syntax highlighting, rendered Markdown, recursive subagent expansion and
  full-text search are not implemented.
- Each read is bounded to the first 32 MiB, 20,000 physical records and 2 MiB per
  record. Incomplete trailing records wait for a later read; malformed and
  oversized records are skipped with notices. Messages keep up to 256 content
  blocks. Display text is capped at 20,000 characters per field and 500,000 per
  page, with shortening markers. These limits do not modify original files.
- Reads are synchronous snapshots, not a background watcher. Concurrent log
  changes are reported when detected. Reload to read newer content; pagination
  can shift if the source is edited or replaced.

## Module ownership

`transcripts/models.py` owns transient display types. `parser.py` interprets
content and pairs tools without filesystem, SQL or web dependencies. `reader.py`
owns read limits and configured-root checks. `service.py` selects only sources
already linked to the requested Claude session and projects bounded pages.

`web/conversations.py` registers the separate HTML and JSON endpoints:

```text
GET /sessions/{session_pk}/conversation
GET /api/sessions/{session_pk}/conversation
```

Both accept only optional positive integer `file_id` and `page` parameters.
Arbitrary paths, duplicate parameters and usage filters are rejected. File IDs
belonging to another session return 404. Sources outside configured roots,
redirected/symlink paths, non-JSONL paths and non-regular files are not opened.
Database snapshots close before source content is read. The new template and
scoped CSS/JavaScript own the presentation; no transcript parser is imported by
the accounting domain or ingestion adapter.

The Claude parser and tool disclosure behavior in the sibling AgentsView project
were reviewed as reference. This implementation is independent and deliberately
smaller; it does not import that application's database or standalone inspector.
Tests use synthetic transcripts and isolated ledgers, including before/after
ledger comparisons to verify that viewing content makes no accounting writes.
