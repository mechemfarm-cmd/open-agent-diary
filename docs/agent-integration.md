# Agent integration

> For autonomous agents that need durable, inspectable local recall.

Open Agent Diary is a local HTTP server and browser UI. It stores raw source entries, work traces, and derived layers. **Raw entries are authoritative.** Derived summaries, graph facts, and beliefs must retain source links and must not be presented as unqualified raw truth.

The project is early working software. A fresh installation starts empty; do not seed real deployments with invented facts.

## Start the server

Run from the intended data directory. In v0.1, storage is `<current working directory>/data`.

```bash
agent-diary serve --host 127.0.0.1 --port 8041
agent-diary doctor
```

Bind beyond loopback only with explicit operator consent. v0.1 write routes are unauthenticated.

## Write/import contract

Use canonical JSONL for session imports:

```jsonl
{"entry_type":"chat_log","source":"my-agent","author_role":"user","content":"...","created_at":"2026-06-16T14:30:00Z"}
```

Preserve the source, timestamp, author role, and source identifiers. Import with:

```bash
agent-diary import-session-and-analyze --path /path/to/entries.jsonl --import-id import-20260616
```

See [Import pipelines](import-pipelines.md) and [canonical transcript schema](canonical-conversation-transcript.md) for the complete shape.

## Recall order

When a human asks about prior work:

1. `POST /search_memory` for broad cross-session recall.
2. `POST /search_work_trace` for what the agent actually did.
3. `POST /graph/search` when a populated graph is relevant.
4. Fetch raw entry/evidence before making a strong claim.
5. Use `POST /recall_beliefs` only deliberately when salient durable facts are relevant.

Belief recall is not query-aware in v1 and records exposure. Use `/list_beliefs` for read-only UI/inspection. See [Belief layer](belief-layer.md).

## Optional graph pipeline

Graph extraction is not automatic merely because graph tables exist. Its required loop is:

```text
import → enqueue jobs → extract → verify queue declines
```

See [Knowledge graph](knowledge-graph.md). If `pending` jobs never decline, the graph is stale.

## Integration choices

- HTTP API: simplest portable integration.
- MCP: use `agent-diary-mcp.py` with an MCP host; see [MCP setup](mcp.md).
- Hermes primary memory: install the provider example for pre-turn recall plus mirrored memory writes; see [`examples/hermes-memory-provider`](../examples/hermes-memory-provider/README.md).

Installing the diary is not the same as making it primary memory. Choose and verify an integration explicitly.

## API response shape

HTTP responses use an envelope:

```json
{"ok": true, "result": {}}
```

On errors, inspect `ok` and `error`; do not assume a transport success means a derived action happened. See [API reference](api-reference.md).