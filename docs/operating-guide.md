# Operating guide

Open Agent Diary is local-first working software. Treat the data directory as valuable: back it up before upgrades, migrations, or experiments.

## Daily operating loop

1. Import new source records.
2. Backfill work traces if your integration supports them.
3. If using the graph, enqueue and drain extraction jobs.
4. Check health with `agent-diary doctor` and `agent-diary graph-queue-status`.
5. Use the UI to inspect raw records before relying on derived layers.

## Health checks

Run from the intended data root:

```bash
agent-diary doctor
agent-diary graph-queue-status
```

`doctor` is read-only. Before the first server run/import it reports missing storage rather than creating it; this is expected. After initial bootstrap, investigate unexpected errors.

## Backups

Stop the server or use a SQLite-safe backup mechanism before copying a live database. Preserve the whole local `data/` directory: database, raw entries, artifacts, work traces, and archives belong together.

A simple offline copy after stopping the service:

```bash
cp -a data data-backup-$(date +%Y%m%d)
```

Do not publish backups, SQLite files, raw entry JSON, or runtime data to a public repository.

## Common problems

### The UI is empty

Check that the server and import command ran from the same working directory. v0.1 resolves storage relative to the current directory.

### Search is thin

Confirm that source data was imported, then inspect `list-entries` and raw records. Search quality cannot exceed imported material.

### Graph queue stays pending

The extractor is not running, lacks an external-model credential, or has failed. A graph needs both enqueue and drain. See [Knowledge graph](knowledge-graph.md).

### Belief panel is empty

Correct for a new graph or facts without computed belief metadata. It is not a reason to seed sample knowledge.

### Exposing the service remotely

Do not expose unauthenticated v0.1 write routes to the public internet. Bind to loopback by default; use SSH forwarding or a trusted private network only after reading the [privacy model](privacy-model.md).

## Supported versus experimental

The supported v0.1 shape is the Python local server and browser UI served by that process. The Tauri shell remains experimental until it supervises the backend lifecycle. See [release readiness](release-readiness.md).