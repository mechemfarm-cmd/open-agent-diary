# Knowledge graph and extraction

The optional knowledge graph turns imported source records into inspectable entities, facts, evidence, aliases, and correction history. It is a **derived** layer: raw entries remain authoritative.

A new installation has an empty graph. That is correct. Do not ship or seed facts into a real diary.

## What the graph stores

- **Entity:** a named thing, with aliases.
- **Fact:** a current claim connecting entities or values.
- **Evidence:** a source record supporting or contradicting a fact.
- **Assertion event:** an auditable change to the derived fact state.
- **Extraction job:** queued source content awaiting LLM-assisted fact extraction.

Use graph search for relationship questions; use raw entries and evidence when correctness matters.

## The pipeline has two halves

Importing entries does not itself make the graph learn. A healthy graph requires:

1. **Enqueue** eligible source entries as extraction jobs.
2. **Drain** those jobs with `scripts/hermes-graph-extractor.py`.
3. **Check** the queue afterward.

A growing `pending` count means the graph is stalled, not busy.

## Build a graph from imported entries

First inspect what would be queued:

```bash
agent-diary graph-backfill --dry-run
```

Then create jobs and inspect queue health:

```bash
agent-diary graph-backfill --batch-size 100
agent-diary graph-queue-status
agent-diary graph-backfill-status
```

Extraction calls an OpenAI-compatible model through OpenRouter by default. **This sends the selected source-entry text (up to the extractor request limit) to that external provider** so it can propose entities and facts. It is optional and may incur both an external-model cost and a data-sharing trade. Do not enable it unless the selected source material is acceptable to send to the provider. Storage, graph tables, evidence links, and belief ranking remain local.

```bash
export OPENROUTER_API_KEY="..."
python3 scripts/hermes-graph-extractor.py --limit 1
agent-diary graph-queue-status
```

For a backlog, run a bounded worker with a lease longer than the slowest individual extraction. Do not claim a large batch merely because average jobs are fast: a lease expiring mid-extraction releases work and can cause duplicate LLM calls.

## Use the graph

```bash
agent-diary graph-search --query "project"
agent-diary graph-find-entity --query "project"
agent-diary graph-get-entity --entity-id <ENTITY_ID>
agent-diary graph-explain-fact --fact-id <FACT_ID>
```

The browser UI offers graph search, graph view, fact inspection, and manual fact creation. Correct a fact instead of overwriting history:

```bash
agent-diary graph-correct-fact --fact-id <FACT_ID> --correction "correct value" --reason "source correction"
```

## Health and limits

- `agent-diary graph-queue-status` should show pending work declining while a worker runs.
- `agent-diary doctor` checks local consistency; it does not run extraction.
- Extraction quality depends on source attribution and the chosen model. Treat facts as derived claims with evidence, not immutable truth.
- Graph APIs are local write routes. Keep the server on loopback unless you have a trusted private network and access controls.

See [Belief layer](belief-layer.md) for how eligible graph facts can be ranked without replacing raw records.