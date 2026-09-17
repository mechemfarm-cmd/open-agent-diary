# Import pipelines

Every integration eventually produces truthful source entries. Open Agent Diary accepts canonical JSONL, then optional derived producers, work traces, graph extraction, and belief ranking build on top.

## Canonical JSONL

```jsonl
{"entry_type":"chat_log","source":"my-agent","author_role":"user","content":"What changed?","created_at":"2026-06-16T14:30:00Z"}
{"entry_type":"chat_log","source":"my-agent","author_role":"assistant","content":"I verified the release.","created_at":"2026-06-16T14:30:05Z"}
```

Import it from the intended data root:

```bash
agent-diary import-session-and-analyze \
  --path /path/to/entries.jsonl \
  --source-session-id session-abc \
  --import-id import-20260616
```

This is the portable path. Preserve timestamps, source identifiers, and author roles; they are later used for provenance and work-trace linking.

## Optional source adapters

The repository ships adapters/reference scripts for Hermes, OpenClaw, Telegram, and transcript conversion. They are examples, not universal installers. Read the script header and adapt paths, session-store shape, speaker labels, and scheduling to your environment.

- `scripts/hermes-to-diary.sh` — Hermes-oriented daily import/reference pipeline
- `scripts/backfill-hermes-work-traces.py` — Hermes work-trace backfill
- CLI transcript/session builders — convert supported source formats into canonical JSONL

A public reference script must not be copied blindly into a different agent runtime. Verify its data paths and run it manually once before scheduling it.

## Daily pipeline

A normal recurring pipeline is:

1. Extract new source sessions.
2. Convert to canonical JSONL.
3. Import entries and produce core derived artifacts.
4. Backfill work traces.
5. Optionally enqueue and drain graph extraction.
6. Report remaining graph jobs rather than silently leaving a queue behind.

For graph-specific operation, see [Knowledge graph](knowledge-graph.md). For agent retrieval behavior, see [Agent integration](agent-integration.md).