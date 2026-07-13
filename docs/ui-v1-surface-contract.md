# Agent Diary UI V1 Surface Contract

## Purpose
Define the current human-facing local UI surface for truthful ingest, scoped inspection, and raw-first review.

## Core screens (current)

1. Diary/Timeline view
2. Entry Detail view
3. Minimal Search surface
4. Optional Artifact/Meta side panel (collapsed by default)

## Screen behavior

### 1) Diary/Timeline view
Primary job: browsing diary entries as human-readable raw memory records.

Show per row:
- `created_at`
- `entry_type`
- `source`
- `author_role`
- `preview` (compact raw-entry preview)
- optional `open_loop` hint when entry participates in unresolved-analysis lineage:
  - `has_open_loops` (bool)
  - `count` (int, number of unresolved loops in the latest linked open-loop analysis artifact)
  - `representative_title` (string|null, one compact unresolved-concern label from the latest linked open-loop analysis artifact)
  - `last_seen_at` (string|null, ISO-8601 timestamp of the latest linked open-loop analysis artifact)

Visual priority:
- Primary: diary entry list rows (raw-entry based)
- Secondary: entry metadata chips (`entry_type`, `source`, `author_role`)

Data source:
- `list_entries(limit, offset)`
- optional provenance scope filter via `list_entries(..., filters={...})`:
  - `source_conversation_id`
  - `source_session_id`
  - `import_id`
  - `truthful_only`
- optional browse focus filter: `list_entries(..., only_with_open_loops=true)` returns only rows participating in open-loop analysis (anchor or lineage-linked)
  - in this mode, `limit`/`offset` apply to the filtered open-loop-participating result set
  - in this mode, rows are ordered by `open_loop.last_seen_at` descending (ties: raw `created_at`, then `entry_id`)

Companion import-batch inspector in the same panel:
- data source: `list_imports(limit)`
- show:
  - `import_id`
  - `imported_at`
  - `imported_count`
  - `skipped_duplicate_count`
  - `source_conversation_id` / `source_session_id` when present
- interaction:
  - selecting one batch applies timeline/search provenance scope for inspection
  - expected applied scope: `import_id`, conversation when present, `truthful_only=true`

### 2) Entry Detail view
Primary job: show one authoritative raw entry in full.

Show:
- full raw entry body from `raw_entry.content` (primary text block)
- entry header metadata (`created_at`, `entry_type`, `source`, `author_role`)
- attached artifacts as secondary supporting items (metadata-first list)
- explicit derived refresh controls (operator-triggered, not automatic):
  - refresh open loops
  - refresh conversation briefs
  - refresh compressed memory
  - uses current provenance scope when present; falls back to selected entry scope otherwise
- overlay-aware derived freshness signal on artifacts:
  - `overlay_stale` (bool)
  - optional `artifact_generated_at`
  - optional `latest_overlay_at`
  - optional `overlay_stale_reason`
  - when `overlay_stale=true`, UI should show a clear warning (for example: “May be stale after overlay”)

Visual priority:
- Primary: `raw_entry` body
- Secondary: artifact list/meta and truth-model labeling

Data source:
- `fetch_entry_detail(entry_id)`
- fallback/verification path: `fetch_raw_entry(entry_id)` when needed

### 3) Minimal Search surface
Primary job: quickly locate likely relevant entries via compressed-memory recall, then open truth.

Show per hit:
- `match_text` snippet
- `indexed_at`
- link target (`entry_id`)

Interaction rule:
- Clicking a hit opens Entry Detail for the same `entry_id`.
- Search results are navigation aids, not diary-body replacements.

Data source:
- `search_memory(query, limit, filters)`
- when timeline scope is active, search should inherit the same provenance scope by default

### 4) Optional Artifact/Meta panel
Behavior:
- Hidden/collapsed by default on Entry Detail.
- Expands to show artifact metadata from `fetch_entry_detail(...).artifacts`.
- Never replaces or displaces raw body as the primary reading surface.

## Interaction rules

1. Browse flow:
- User lands on Timeline.
- Scroll/paginate with `list_entries`.
- User can narrow browse scope by provenance (conversation/import/truthful-only) while keeping the same timeline paging model.

2. Open entry flow:
- Selecting a row opens Entry Detail for that `entry_id`.

3. Memory-hit-to-truth flow:
- User enters search query.
- UI shows compressed-memory hits.
- Selecting a hit navigates to Entry Detail (`entry_id`) and reads raw truth.

4. Overlay/edit mental model:
- Raw entry is immutable source record.
- Human corrections/annotations should appear as overlays, not destructive edits.
- After adding overlays, previously generated derived artifacts may be stale until producers are rerun explicitly.
- `overlay_stale=true` means overlays were added after artifact generation; it does not mean the artifact was auto-updated.
- Rerunning derived layers is an explicit operator action from UI controls or CLI; no hidden auto-regeneration.

## API dependency map

- Timeline: `list_entries`
- Entry Detail: `fetch_entry_detail`
- Search surface: `search_memory`
- Deep truth fetch (optional fallback): `fetch_raw_entry`

## Explicit non-goals

- No admin-console/system-management surface.
- No presentation where compressed-memory artifact text is the main diary body.
- No multi-user controls, role management, sync admin, or deployment controls.

## Tiny API clarification for later

- `list_entries` currently builds `entry_type` and `preview` by reading raw files via indexed paths. Later, backend may optionally persist `entry_type` and a preview cache in SQLite for faster timeline reads, but the truth model should remain unchanged.
