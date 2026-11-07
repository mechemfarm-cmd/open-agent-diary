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

## Onboarding a new installation

`agent-diary onboard` takes a fresh diary to one holding the user's own history
and connected to their agent. Start by asking where things stand:

```bash
agent-diary --json onboard --status
```

The response gives the current step and `action.command` — run that command
verbatim; it is complete as emitted.

Sequence: `init → discover → consent → export → preview → import → review → connect → done`.

You may run every step yourself EXCEPT these, which belong to the user:

- choosing the data root (`init` reports `needs_human`)
- granting content scope (`consent`)
- confirming the import after seeing samples (`preview --confirm`)
- approving derived knowledge (`review`)

Never choose a data root, a consent scope, or an approved fact on the user's
behalf. If a step returns `needs_human: true`, stop and ask.

Content is read only after an approved `content` scope, and only by `export`,
which opens the source database read-only. `discover` reads no content.

Full walkthrough and troubleshooting: `docs/onboarding.md`.

## Read next

- `docs/onboarding.md`
- `docs/getting-started.md`
- `docs/agent-integration.md`
- `docs/import-pipelines.md`
- `docs/knowledge-graph.md`
- `docs/belief-layer.md`
- `docs/api-reference.md`
- `docs/mcp.md`
