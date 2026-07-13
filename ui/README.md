# Agent Diary UI Shell

This local UI focuses on raw-first truth inspection with provenance-scoped browse/search and import-batch audit helpers.

## What it covers

- Timeline list from `list_entries`
- Recent import-batch inspector from `list_imports`:
  - import id
  - imported timestamp
  - imported/skipped duplicate counts
  - source conversation/session ids when present
  - click an import batch to apply timeline/search scope
- Timeline provenance scope controls for:
  - `source_conversation_id`
  - `import_id`
  - `truthful_only`
- Entry detail from `fetch_entry_detail`
- Linked `Agent Work` panel in entry detail showing attached work-trace events:
  - summary
  - event type / actor / project / source surface
  - touched paths
  - related artifact ids
  - structured event details
- Explicit in-UI derived refresh controls in entry detail:
  - Refresh Open Loops
  - Refresh Conversation Briefs
  - Refresh Compressed Memory
  - uses active scope (`source_conversation_id`, `import_id`, `truthful_only`) when set, otherwise falls back to selected `entry_id`
- Memory search from `search_memory`
- Search requests inherit active timeline provenance scope (`source_conversation_id`, `import_id`, `truthful_only`) so browse/produce/search stay aligned
- Click-through from search hit to entry detail via `entry_id`
- Raw entry content shown as primary body
- Mixed-speaker `chat_log` entries rendered as speaker-separated dialogue turns with distinct speaker colors
- Artifacts shown as secondary metadata
- Derived artifacts with `overlay_stale=true` show a visible warning badge:
  - “May be stale after overlay”
  - compact timing context (`artifact_generated_at`, `latest_overlay_at`) when available
- `analysis:open-loop` artifacts rendered in the secondary artifact section with:
  - title
  - summary
  - strength/confidence
  - clickable supporting entry ids (navigates to raw entry detail)
- Lightweight local UI state persistence (`localStorage`) for:
  - selected entry
  - list paging (`offset`, `limit`)
  - active scope filters
  - current search query
  - selected search-hit entry
- Lightweight URL state for reopenable/deep-link context:
  - `?entry=<entry_id>`
  - `?q=<search_query>`
  - `?offset=<timeline_offset>`
  - `?source_conversation_id=<conversation_id>`
  - `?import_id=<import_batch_id>`
  - `?truthful_only=1`
  - URL state is restored first; localStorage is fallback when URL has no state.

## Run locally

Terminal 1 (backend service):

```bash
cd /Users/sampleuser/agent-diary
PYTHONPATH=src python3 -m agent_diary.cli.main serve --host 127.0.0.1 --port 8041
```

Terminal 2 (static UI server):

```bash
cd /Users/sampleuser/agent-diary/ui
python3 -m http.server 5173
```

Open:

- http://127.0.0.1:5173

If needed, set `API Base` in the header (default: `http://127.0.0.1:8041`).

## Manual accessibility check (lightweight)

1. Use `Tab` from the top and confirm visible focus is clear on API controls, timeline buttons, search controls, and artifact summary.
2. In timeline or search results, use `ArrowUp`/`ArrowDown` plus `Home`/`End` to move between result buttons.
3. Press `Enter` on a focused timeline/search item and confirm entry detail loads and focus moves to the diary body.
4. Use the “Skip to diary entry” link at top to jump directly to the raw entry body.
5. Open a mixed-speaker `chat_log` entry and confirm each turn is separated into a colored speaker block instead of a single wall of text.
6. If an entry has `analysis:open-loop` artifacts, expand “Artifacts (secondary)” and verify loop cards render as derived interpretation with supporting-entry navigation.
7. Add an overlay to an entry with derived artifacts and confirm stale artifacts show “May be stale after overlay”.

No auassistantated UI accessibility tests are included in this shell pass.

## Operator loop in UI

1. Open `Recent Imports` and select a batch.
2. Confirm timeline status reflects active scope.
3. Browse and search within that scope.
4. Open entry detail and treat raw entry content as primary truth.
5. Use artifact sections as secondary analysis/provenance context.
6. If a derived artifact is marked “May be stale after overlay”, rerun the relevant scoped producer explicitly.
7. Use `Refresh Derived Layers` in entry detail for explicit refresh without leaving the UI.
