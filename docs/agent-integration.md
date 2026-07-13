# Agent Diary — Agent Integration Guide

> **Who this is for:** An autonomous agent that needs to integrate Agent Diary
> as a permanent memory and work-trace store.

---

## The Integration Contract

Agent Diary is a local-first HTTP server on **port 8041**. It serves two things:
- A REST API (for querying, importing, managing entries)
- A browser UI (for human inspection — served from the same port)

Everything an agent needs happens over the API. The UI is optional.

---

## 1. Start the Server

```bash
pip install -e /path/to/agent-diary
agent-diary serve --host 127.0.0.1 --port 8041
```

Or via Python directly:

```bash
cd /path/to/agent-diary
PYTHONPATH=src python3 -m agent_diary.cli.main serve --host 0.0.0.0 --port 8041
```

By default, bind the server to `127.0.0.1` for local-only access. Bind to `0.0.0.0` or a private-network address only when the operator explicitly wants other trusted devices to connect.

---

## 2. Import Data

### Canonical JSONL Format

Every entry the diary accepts must conform to this schema. This is the canonical import format that any agent can emit:

```jsonl
{"entry_type": "chat_log", "source": "my-agent", "author_role": "user", "content": "What was the IP of the server?", "created_at": "2026-06-16T14:30:00Z"}
{"entry_type": "chat_log", "source": "my-agent", "author_role": "assistant", "content": "The server is available on my private network.", "created_at": "2026-06-16T14:30:05Z"}
```

Required fields:

| Field | Type | Description |
|-------|------|-------------|
| `entry_type` | string | `chat_log`, `diary_note`, `system_log` |
| `source` | string | Platform or agent source (e.g. `hermes-session`, `telegram`, `my-agent`) |
| `author_role` | string | `user`, `assistant`, `system` |
| `content` | string | The raw text content |
| `created_at` | string | ISO 8601 timestamp |

Optional fields:

| Field | Type | Description |
|-------|------|-------------|
| `title` | string | A short label for the entry |
| `metadata` | object | Arbitrary metadata (see below) |

### Via CLI

```bash
agent-diary import-session-jsonl \
    --path /path/to/entries.jsonl \
    --source-session-id "session-abc123" \
    --source-conversation-id "chat:demo-conversation" \
    --import-id "import-20260616"
```

### Via API (direct POST)

```bash
curl -X POST http://localhost:8041/append_entry \
    -H "Content-Type: application/json" \
    -d '{
        "entry_type": "chat_log",
        "source": "my-agent",
        "author_role": "user",
        "content": "Hello from my agent!",
        "created_at": "2026-06-16T14:30:00Z",
        "metadata": {
            "source_session_id": "session-abc123",
            "ingestion": {"truthful_source": true, "import_mode": "direct"}
        }
    }'
```

---

## 3. Query Data

### Search memory (fast, cross-session)

```bash
curl -X POST http://localhost:8041/search_memory \
    -H "Content-Type: application/json" \
    -d '{"query": "server IP address", "limit": 5}'
```

Returns briefs, compressed memory, and raw entry excerpts. This is the primary recall endpoint.

### List entries (timeline)

```bash
curl -X POST http://localhost:8041/list_entries \
    -H "Content-Type: application/json" \
    -d '{"limit": 50, "offset": 0}'
```

Returns entries with work trace counts and open-loop indicators. Supports scope filters:

```json
{
    "filters": {
        "source_conversation_id": "chat:demo-conversation",
        "source_session_id": "session-abc123",
        "import_id": "import-20260616",
        "truthful_only": true
    }
}
```

### Get entry detail

```bash
curl -X POST http://localhost:8041/fetch_entry_detail \
    -H "Content-Type: application/json" \
    -d '{"entry_id": "entry_abc123def456"}'
```

Returns full entry body, metadata, work traces, overlays, and artifacts.

### Search work traces

```bash
curl -X POST http://localhost:8041/search_work_trace \
    -H "Content-Type: application/json" \
    -d '{"query": "tailscale ping", "limit": 10}'
```

---

## 4. Work Traces

Work traces capture tool-call evidence — what the agent actually *did* between messages.
They are stored alongside diary entries and linked via a junction table.

### Canonical work trace event

```json
{
    "event_type": "command",
    "summary": "Ran command: tailscale status",
    "created_at": "2026-06-16T14:30:10Z",
    "actor": "assistant",
    "source_surface": "terminal",
    "project": "my-project",
    "session_key": "session-abc123",
    "related_entry_ids": ["entry_abc123def456"],
    "related_paths": [],
    "tags": ["command"],
    "details": {
        "tool_name": "terminal",
        "arguments": {"command": "tailscale status"},
        "is_error": false
    }
}
```

### Via CLI (batch backfill)

```bash
python3 scripts/backfill-hermes-work-traces.py \
    --diary-db data/index/memory.db \
    --hermes-db ~/.my-agent/state.db \
    --data-dir data
```

### Via API (single event)

```bash
curl -X POST http://localhost:8041/append_work_trace \
    -H "Content-Type: application/json" \
    -d '{
        "event_type": "command",
        "summary": "Ran command: tailscale status",
        "actor": "my-agent",
        "source_surface": "terminal",
        "related_entry_ids": ["entry_abc123def456"]
    }'
```

---

## 5. Daily Pipeline (Recommended)

Set up a cron job that runs once per day:

1. **Extract** new sessions from your agent's session store
2. **Convert** to canonical JSONL format
3. **Import** via `agent-diary import-session-jsonl`
4. **Backfill** work traces from tool-call records

The included `hermes-to-diary.sh` script is a reference implementation. Adapt it for your agent's session store format.

---

## 6. Recall Protocol (Recommended Query Order)

When the human asks about past work or conversations:

1. **`POST /search_memory`** with their question → cross-session memory index (fast, broad)
2. **`POST /search_work_trace`** with keywords → operational evidence (execution details)
3. **`POST /list_entries`** with scope filters → narrow to specific session or import batch
4. Fall back to native session search only if diary results are thin

The diary's memory index spans all imported sessions — it is a *better* first stop than session-by-session transcript search.

---

## 7. Metadata Conventions

The diary uses a single metadata convention for provenance tracking:

```json
{
    "metadata": {
        "source_session_id": "session-abc123",
        "source_conversation_id": "chat:demo-conversation",
        "ingestion": {
            "truthful_source": true,
            "import_mode": "session_jsonl",
            "import_id": "import-20260616",
            "imported_at": "2026-06-16T14:35:00Z",
            "source_item_key": "chat_log:session-abc123:msg:42"
        }
    }
}
```

The `ingestion` block is automatically added by `import-session-jsonl`. When posting entries directly via the API, provide at minimum `source_session_id` so work traces can be linked correctly.