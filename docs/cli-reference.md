# CLI reference

Run commands from the directory that owns your diary `data/` directory.

## Server and diagnosis

```bash
agent-diary serve --host 127.0.0.1 --port 8041
agent-diary doctor
agent-diary --json doctor
```

`doctor` is read-only. It reports missing storage on an uninitialized directory instead of creating it.

## Import and inspect

```bash
agent-diary import-session-jsonl --path entries.jsonl --import-id import-20260616
agent-diary import-session-and-analyze --path entries.jsonl --import-id import-20260616
agent-diary list-imports --limit 20
agent-diary list-entries --limit 20
agent-diary fetch-entry-detail --entry-id <ENTRY_ID>
```

## Recall and work evidence

```bash
agent-diary search-memory --query "release checklist" --limit 10
agent-diary search-work-trace --query "test command" --limit 10
agent-diary search-all --query "release checklist" --limit 10
agent-diary fetch-raw-entry --entry-id <ENTRY_ID> --include-artifacts
```

## Derived artifacts

```bash
agent-diary produce-conversation-briefs --import-id <IMPORT_ID>
agent-diary produce-compressed-memory --import-id <IMPORT_ID>
agent-diary produce-open-loops --import-id <IMPORT_ID>
agent-diary refresh-derived-for-import --import-id <IMPORT_ID>
```

## Knowledge graph

```bash
agent-diary graph-backfill --dry-run
agent-diary graph-backfill --batch-size 100
agent-diary graph-queue-status
agent-diary graph-search --query "project"
agent-diary graph-find-entity --query "project"
agent-diary graph-get-entity --entity-id <ENTITY_ID>
agent-diary graph-explain-fact --fact-id <FACT_ID>
agent-diary graph-correct-fact --fact-id <FACT_ID> --correction "correct value" --reason "source correction"
```

Graph extraction requires a separate worker; see [Knowledge graph](knowledge-graph.md).

## Source adapters

The CLI also includes source-specific import/build helpers for transcripts, Telegram, and OpenClaw. They are adapters, not universal defaults. Use `agent-diary <command> --help` and [Import pipelines](import-pipelines.md) before applying an adapter to real data.

For the full current command list, run:

```bash
agent-diary --help
```