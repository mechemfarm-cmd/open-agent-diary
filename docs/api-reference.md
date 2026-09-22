# HTTP API reference

The local server uses JSON request bodies and normally returns:

```json
{"ok": true, "result": {}}
```

Errors use `{"ok": false, "error": "..."}`. Keep the server on `127.0.0.1` unless the operator explicitly accepts private-network exposure; v0.1 write routes are unauthenticated.

## Core read routes

| Route | Effect | Use |
|---|---|---|
| `POST /search_memory` | read-only | broad cross-session recall |
| `POST /search_work_trace` | read-only | operational evidence |
| `POST /search_all` | read-only | combined search |
| `POST /list_entries` | read-only | timeline/browse |
| `POST /fetch_entry_detail` | read-only | raw entry plus attached layers |
| `POST /semantic/preview` | read-only | bounded source-linked semantic situation preview |
| `POST /list_imports` | read-only | import batches |

Example:

```bash
curl -sS -X POST http://127.0.0.1:8041/search_memory \
  -H 'Content-Type: application/json' \
  -d '{"query":"release checklist","limit":5}'
```

## Semantic preview

`POST /semantic/preview` requires `topic` and `purpose` (`current_status`, `decision_rationale`, or `next_action`). Optional `limit` and `char_budget` are hard-capped by the handler. The response includes `read_only: true`, rendered `preview` text, and a structured `situation` with source references and inference notes. It does not record belief exposure and is not part of automatic recall.

## Local write/import routes

`/append_entry`, `/append_work_trace`, `/attach_artifact`, `/append_overlay`, import routes, and derived refresh routes write to the local diary. Preserve truthful source metadata and do not expose these routes publicly.

## Graph routes

Graph read routes include `/graph/find_entity`, `/graph/get_entity`, `/graph/neighbors`, `/graph/search`, `/graph/explain_fact`, `/graph/get_subgraph`, `/graph/queue_status`, `/graph/backfill_status`, and `/graph/export`.

Graph write/operation routes include `/graph/add_fact`, `/graph/correct_fact`, `/graph/merge_alias`, `/graph/split_entity`, `/graph/backfill`, `/graph/enqueue_recent`, `/graph/claim_jobs`, `/graph/submit_extraction`, and `/graph/fail_extraction`.

Graph extraction jobs are local queue operations; the extractor is a separate process and may call an external model. See [Knowledge graph](knowledge-graph.md).

## Belief routes

| Route | Effect |
|---|---|
| `POST /recall_beliefs` | returns a bounded recall block and records surfaced facts |
| `POST /list_beliefs` | read-only ranking for UI/inspection |
| `POST /confirm_beliefs` | human usefulness confirmation; adjusts use signal |
| `POST /credit_beliefs` | explicit corroboration credit path |

Do not use `/recall_beliefs` to populate a passive UI: it has an exposure side effect. See [Belief layer](belief-layer.md).

## Discover safely

This guide groups the stable concepts rather than duplicating every field of a fast-moving v0.1 surface. Inspect `agent-diary --help` and the local server routes for the installed version before automating a write flow. Test with synthetic data first.