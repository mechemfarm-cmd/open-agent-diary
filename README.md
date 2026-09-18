# Open Agent Diary

> **Early, working open-source software.** Open Agent Diary is useful today, local-first, and actively evolving. Install it if you are comfortable keeping backups, seeing workflows change, and reporting what is unclear or broken.

**Your AI agent talks to you every day. Does it remember what it learned — and can you inspect why?**

Open Agent Diary is a local-first, inspectable memory and work-trace store for human/agent collaboration. It keeps raw conversation records, agent work evidence, and derived recall layers in one place so an agent can remember across sessions without turning memory into an uninspectable black box.

![Open Agent Diary demo](docs/demo.gif)

The rule that matters: **raw entries are authoritative.** Summaries, graph facts, and belief rankings are derived support layers. Every useful claim should lead back to source records you can inspect.

## Who is this for?

- People using Hermes, Claude Code, or another autonomous agent who want durable local recall.
- Developers who want an inspectable memory system rather than a hosted black box.
- Early users willing to work with a v0.1 project and give useful feedback.

Wait if you need a hosted service, a finished one-click desktop application, or network-exposed authenticated multi-user storage. This project is none of those yet.

## Quick start

Run commands from the directory that should own the diary data. In v0.1, runtime data lives in **`<current working directory>/data`**.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
agent-diary serve --host 127.0.0.1 --port 8041
```

Open <http://127.0.0.1:8041>.

### Try it with synthetic data

Open a second terminal in the same repository and virtual environment:

```bash
agent-diary import-session-and-analyze \
  --path examples/synthetic-session-import.jsonl \
  --import-id demo
agent-diary --json search-memory --query "release checklist"
agent-diary doctor
```

The repository contains invented fixtures only. A new real installation starts empty by design; an empty timeline, graph, or belief list is not an error.

## What you get

- **Raw entries** — append-only conversation/source records you can browse and inspect.
- **Work traces** — evidence of what an agent did between visible messages.
- **Derived artifacts** — briefs, compressed recall, and open loops; useful but never the source of truth.
- **Optional knowledge graph** — entities, facts, evidence, aliases, corrections, and source links.
- **Optional belief ranking** — a careful ranking over derived graph facts, with provenance and usefulness signals.
- **Browser UI, CLI, REST API, and MCP server** — choose the integration level that fits your agent.
- **`agent-diary doctor`** — a read-only consistency check for local storage.

## Choose an integration level

1. **Archive and API** — import JSONL and search/browse locally. Start with [Getting started](docs/getting-started.md).
2. **MCP tools** — connect an MCP-compatible agent to search and inspect the diary. See [MCP setup](docs/mcp.md).
3. **Hermes primary memory** — pre-turn recall plus mirrored memory writes. See [Hermes primary memory](examples/hermes-memory-provider/README.md).

Installing the server alone creates an archive. The provider/plugin integration is what makes recall automatic for an agent.

## Optional: knowledge graph and belief layer

The graph is built from imported source entries; it is not seeded. Its pipeline has two required halves: **enqueue extraction jobs, then drain them with an extractor**. A queue that only grows is a stalled graph, not progress.

**Privacy boundary:** graph storage and belief ranking stay local, but the default extractor sends the selected source-entry text to an external OpenRouter model to propose facts. Enable extraction only if you accept that provider receiving the selected content; use local/manual facts only if you do not.

The belief layer ranks derived facts but does not replace raw records. On a new installation it has no history and should be empty. Its ranking needs real use over time; it is not yet a claim of proven “better memory.”

- [Knowledge graph and extraction](docs/knowledge-graph.md)
- [Belief layer semantics](docs/belief-layer.md)

## Privacy and safety

By default the server binds to `127.0.0.1`. Your data stays in the local data directory you chose. The current v0.1 API has unauthenticated write routes, so bind to a LAN/Tailscale/private interface only when you understand the exposure and have appropriate network controls.

Read the [privacy model](docs/privacy-model.md) before exposing the service beyond loopback. Keep backups before upgrades or experiments.

## Documentation

- [Getting started](docs/getting-started.md) — first run, demo, data root, and first-run expectations
- [Operating guide](docs/operating-guide.md) — daily use, health checks, backups, and troubleshooting
- [Agent integration](docs/agent-integration.md) — generic agent contract and recall protocol
- [Import pipelines](docs/import-pipelines.md) — canonical JSONL plus integration patterns
- [Knowledge graph](docs/knowledge-graph.md) and [belief layer](docs/belief-layer.md)
- [MCP setup](docs/mcp.md)
- [CLI reference](docs/cli-reference.md) and [API reference](docs/api-reference.md)
- [Release readiness](docs/release-readiness.md) — supported v0.1 shape and deliberate limits

## Development checks

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -q
PYTHONPATH=src python3 -m compileall -q src scripts tests
agent-diary doctor
```

## License

MIT License. See [LICENSE](LICENSE).