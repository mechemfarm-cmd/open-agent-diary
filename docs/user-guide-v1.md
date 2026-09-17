# Agent Diary user guide

Open Agent Diary is a local-first, inspectable memory system for one human and one or more agents. It is early, working open-source software: useful today, still evolving, and best used with backups and honest feedback.

## The mental model

There are three layers:

1. **Raw entries** — what was actually said or imported. This is authoritative.
2. **Agent work** — commands, file changes, tests, and other operational evidence linked to entries.
3. **Derived layers** — briefs, compressed recall, open loops, graph facts, and belief rankings. These help navigation and recall but do not replace source records.

If a summary or graph fact disagrees with raw evidence, inspect the raw entry and correct the derived layer; do not rewrite history.

## First use

Follow [Getting started](getting-started.md) to install, run the synthetic demo, and choose your data root. A brand-new installation is empty by design.

## Browser UI

The UI is for human inspection and correction:

- **Search / recent entries** find a source record.
- **Center record** shows the raw entry first.
- **Derived / support panels** show briefs, work evidence, provenance, refresh controls, graph information, and corrections.
- **Knowledge by Belief** ranks eligible derived facts. Opening it is read-only; **Useful** is an explicit human signal that a fact helped.

The UI should make it easier to ask “where did this come from?” rather than encouraging blind trust in a summary.

## Daily use

1. Import new conversations/source records.
2. Browse or search the raw record when you need to understand prior work.
3. Inspect Agent Work when the visible conversation does not explain what happened.
4. Add corrections/annotations without replacing the raw source.
5. If you use the optional graph, ensure its extraction queue is actually draining.
6. Run `agent-diary doctor` before upgrades or when something looks wrong.

See [Operating guide](operating-guide.md) for health checks and backups.

## Optional graph and beliefs

The graph is useful for durable relationships across many entries. It is not automatic: importing data creates source records, while graph extraction needs a separate queue and worker. See [Knowledge graph](knowledge-graph.md).

The belief layer is a cautious ranking over graph facts. It starts empty, keeps provenance/uncertainty, and needs real use before ranking quality can be judged. See [Belief layer](belief-layer.md).

## Privacy

The diary may contain private conversations, paths, and work evidence. Keep the server on `127.0.0.1` by default. The v0.1 API has unauthenticated write routes, so private-network exposure is an explicit risk decision, not the default.

Read [Privacy model](privacy-model.md) before exposing it remotely.

## What this is not

- Not a hosted cloud memory service.
- Not a replacement for raw records with opaque summaries.
- Not a finished consumer product.
- Not a general reasoning engine or automatic truth detector.

It is a practical local system for agents and humans who want durable memory they can inspect, challenge, and improve.