# Getting started

Open Agent Diary is a local Python server with a browser UI. It is early, working software: keep backups, expect interfaces to evolve, and start with synthetic data before importing private history.

## Choose your data directory

Run `agent-diary init` once. It proposes a path, asks you to confirm it, scaffolds the diary there, and records the choice in `~/.config/agent-diary/config.json` so later commands find the same diary from anywhere.

```bash
agent-diary init                                  # interactive: confirm the proposed path
agent-diary init --data-root /srv/diary --yes     # non-interactive
```

Resolution order, highest first: an explicit `--data-root`, then `AGENTDIARY_DATA`, then the config file, then `<current working directory>/data`.

`init` does not adopt an existing data root without asking, and it will not overwrite a configured root unless you pass `--force`.

## Install and start

```bash
cd /path/to/open-agent-diary
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
agent-diary init
agent-diary onboard
agent-diary serve --host 127.0.0.1 --port 8041
```

`onboard` is optional. It looks for history an agent already has on this machine, shows you exactly what it would read, and imports only what you approve.

Open <http://127.0.0.1:8041>. Loopback is the safe default because v0.1 write routes are unauthenticated.

## First successful run

In a second terminal, still in the same directory and virtual environment:

```bash
agent-diary import-session-and-analyze \
  --path examples/synthetic-session-import.jsonl \
  --import-id demo
agent-diary --json search-memory --query "release checklist"
agent-diary doctor
```

The UI should now show imported entries and derived artifacts. The `doctor` report should be healthy after storage has been initialized.

## What first-run states mean

- **Empty timeline / graph / beliefs:** correct before importing data.
- **Doctor reports missing data before first startup/import:** expected; doctor intentionally does not create storage while diagnosing it.
- **UI opens but is empty after import:** confirm that the server and import command ran from the same directory, then run `agent-diary doctor`.

## Next steps

- Import your own canonical JSONL: [Import pipelines](import-pipelines.md).
- Connect an agent through HTTP or MCP: [Agent integration](agent-integration.md) and [MCP](mcp.md).
- Add graph extraction only if you need entity/fact recall: [Knowledge graph](knowledge-graph.md).
- Read the operational and backup advice: [Operating guide](operating-guide.md).