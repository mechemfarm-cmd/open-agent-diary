# Open Agent Diary — Agent Boot Contract

Open Agent Diary is a local-first, inspectable durable-memory store. Raw entries are authoritative; summaries, graph facts, and beliefs are derived layers with source links.

It is early working software. Do not promise a hosted service, automatic graph updates, or proven belief-ranking quality. A new diary starts empty by design.

## Start and verify

Resolve your data root before anything else. Precedence is: `--data-root` flag,
then `AGENTDIARY_DATA`, then the config file, then the default proposal. Run
`agent-diary init` to set it deliberately — it never adopts a root silently.

```bash
agent-diary init --data-root /path/to/diary   # once, explicit
agent-diary serve --host 127.0.0.1 --port 8041
agent-diary doctor
```

`doctor` is read-only. After `init` it should report zero errors; a root with
missing directories or no database has not been initialized.

## Recall protocol

When a human asks about prior work:

1. Use `POST /search_memory` for cross-session recall.
2. Use `POST /search_work_trace` for operational evidence.
3. Use `POST /graph/search` when graph facts are relevant and populated.
4. Fetch the raw entry/source evidence before making a strong claim.
5. Use `POST /recall_beliefs` only deliberately when globally salient graph facts are relevant. It records exposure; it is not a query-aware automatic prefetch.

Use `POST /list_beliefs` for read-only inspection. Do not use it as a substitute for raw evidence.

## Import and graph discipline

- Import canonical JSONL; preserve source, author role, content, and timestamp.
- Backfill work traces separately when the source platform supports them.
- The graph is optional. To feed it: import entries → enqueue jobs → run extraction worker → verify queue health.
- A `pending` queue that never declines means extraction is not running.
- Do not seed facts into a real installation. Empty is correct; knowledge must grow from the operator’s source records.

## Privacy

Bind to `127.0.0.1` unless the operator explicitly accepts private-network exposure. The v0.1 write API is unauthenticated. Never place secrets or real private data in public fixtures or documentation.

## First-time setup

`agent-diary onboard` walks a new user (or their agent) from an empty diary to
one preloaded with their own history, consent-gated at every source. It is
resumable and has a JSON interface: `agent-diary --json onboard --status`.

See `docs/plans/2026-11-05-onboarding-implementation.md` for what is built so far.

## Read next

- `docs/getting-started.md`
- `docs/agent-integration.md`
- `docs/import-pipelines.md`
- `docs/knowledge-graph.md`
- `docs/belief-layer.md`
- `docs/api-reference.md`
- `docs/mcp.md`
