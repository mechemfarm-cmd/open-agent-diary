# Deployment model

Open Agent Diary has one portable source of truth: the `main` branch of the
public GitHub repository. Every deployed machine uses a clean checkout of that
same commit. A machine's diary entries, SQLite indexes, import markers,
staging files, secrets, service units, and branding are local operational state
and never belong in the portable repository.

## The three-copy rule

```text
GitHub main (canonical source)
        ↓
clean Emily checkout (Tom's deployment)
        ↓
clean Art checkout (Alice's deployment)
```

The two deployed checkouts must identify the same Git commit as GitHub before
calling the fleet synchronized. A dirty working tree, an inaccessible remote,
or an ahead/behind branch is an explicit unsynchronized state, not a harmless
status message.

## Safe update sequence

1. Make a SQLite backup of the target machine's existing local data.
2. Fetch and record GitHub `main`; do not overwrite a live tree.
3. Create or update a clean source checkout separately from local data.
4. Run the project test suite, compile check, and `agent-diary --json doctor`
   against a disposable data root.
5. Point the service to the verified checkout while keeping its data path
   outside the source tree.
6. Restart only that machine's Diary service, then verify the process import
   path, HTTP health, search, and graph queue.
7. Record the commit IDs, service path, data path, and rollback location in a
   private operator state record.

## What never gets copied as source

Do not synchronize any of these between machines or commit them to the public
repository:

- `data/`, databases, backups, staging files, import/backfill markers
- `.env`, credentials, tokens, or host-specific settings
- user/session content or local branding overlays
- virtual environments, `node_modules`, generated build output, or caches

## Graph ingestion

A graph deployment needs both halves of the pipeline:

```text
import entries → enqueue extraction jobs → drain a bounded worker slice
```

The reference `scripts/hermes-to-diary.sh` performs both. The worker uses a
one-hour lease, one claimed job per invocation, and a configurable bounded
slice (`GRAPH_EXTRACTOR_DAILY_JOBS`, default `3`) so a scheduler does not kill
an extraction mid-lease. Operators who choose not to configure external graph
extraction may leave the queue undrained; the rest of Agent Diary remains
local and functional.

## Recovery rule

If the locations diverge again, stop before copying files. Inspect the live
process import path, the GitHub commit, the checkout commit, and the local data
location first. Reconcile portable source through Git; preserve local state
separately.
