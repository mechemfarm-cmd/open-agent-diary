# Agent Diary — Agent Instructions

You are an autonomous agent that has been pointed at this repository.
Agent Diary is a permanent memory and work-trace store.
Your job is to integrate with it.

## What Agent Diary Is

- A local HTTP server (port 8041) that stores chat entries, derived memory, and work traces
- A browser UI for the human to inspect and manipulate data
- A daily import pipeline that converts your session transcripts into diary entries

It is **not** a replacement for your working memory.
It is a permanent, inspectable, cross-session record that survives resets.

## Setup

1. Install from source:

   ```bash
   cd /path/to/agent-diary
   pip install -e .
   ```

2. Start the server:

   ```bash
   agent-diary serve --host 0.0.0.0 --port 8041
   ```

3. Schedule daily import:

   Adapt the reference script `scripts/hermes-to-diary.sh` for your session store format.
   The script should:
   - Extract new sessions from your session database
   - Convert them to the canonical JSONL format (see `docs/agent-integration.md`)
   - Import via `agent-diary import-session-jsonl`
   - Backfill work traces from tool-call records

## Querying Protocol

When the human asks about past work or conversations:

1. **POST /search_memory** with their question (fast cross-session search)
2. **POST /search_work_trace** with keywords (operational evidence)
3. **POST /list_entries** with scope filters (narrow to a session or import)
4. Only fall back to native session search if the diary is thin

The diary's memory index spans **all** imported sessions — it covers more ground than individual session transcript search.

## Canonical Import Format

Every entry must have these fields:

```json
{
  "entry_type": "chat_log",
  "source": "my-agent",
  "author_role": "user",
  "content": "...",
  "created_at": "2026-06-16T14:30:00Z"
}
```

See `docs/agent-integration.md` for the full API reference.

## Files to Read

- `docs/agent-integration.md` — Full API contract and JSONL schema
- `docs/user-guide-v1.md` — Human-facing documentation
- `scripts/hermes-to-diary.sh` — Reference import pipeline implementation
- `scripts/backfill-hermes-work-traces.py` — Work trace backfill implementation