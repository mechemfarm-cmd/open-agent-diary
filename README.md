# Open Agent Diary

Open Agent Diary is a local-first, inspectable memory and work-trace store for human/agent collaboration.

It separates three layers:

1. **Raw entries** — what was said or imported.
2. **Work traces** — what the agent actually did: commands, file edits, tests, tool calls.
3. **Derived memory** — summaries, compressed memory, open loops, and other analysis artifacts.

The core rule is simple: **derived memory is never the hidden source of truth.** Users can inspect the raw records and the derived layers that agents rely on.

## Quick start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
agent-diary serve --host 127.0.0.1 --port 8041
```

Then open:

```text
http://127.0.0.1:8041
```

## Import sample data

```bash
agent-diary import-session-jsonl --path examples/synthetic-session-import.jsonl --import-id demo
agent-diary produce-conversation-briefs --import-id demo --force
agent-diary produce-compressed-memory --import-id demo --force
agent-diary produce-open-loops --import-id demo
agent-diary search-memory --query "release checklist" --json
agent-diary doctor --json
```

## What is included

- Local HTTP API and static browser UI
- Append-only raw entry store on disk
- SQLite metadata/search index
- FTS5 acceleration for memory and work-trace search
- Work-trace layer for operational evidence
- Derived artifacts for conversation briefs, compressed memory, and open loops
- Read-only `doctor` command for consistency checks
- Synthetic examples only — no real chat history ships with the project

## Project layout

| Path | Purpose |
|---|---|
| `src/agent_diary/` | Python backend, CLI, indexing, storage, producers |
| `ui/` | Static browser UI served by the backend |
| `tests/` | Backend regression tests |
| `examples/` | Synthetic JSONL fixtures for demo/import testing |
| `docs/` | Integration and design documentation |

Runtime data is created under `data/` when you run the app. That directory is ignored by Git.

## For agents

Start with:

- `AGENTS.md`
- `docs/agent-integration.md`

The intended recall flow is:

1. Query `/search_memory` for cross-session memory.
2. Query `/search_work_trace` for evidence of what happened.
3. Fetch raw entries or work traces when details matter.
4. Let the human inspect and correct the record through the UI/overlay layer.

## For users

Agent Diary is designed so each installation gathers **your own data locally**. The public repository contains only code, docs, tests, and synthetic fixtures.

## Development checks

```bash
PYTHONPATH=src python3 -m unittest -v tests.test_append_entry_slice
PYTHONPATH=src python3 -m compileall -q src scripts tests
PYTHONPATH=src python3 -m agent_diary.cli.main --json doctor
```

## Author

Created by Willard Mechem.

## License

MIT License. See `LICENSE`.
