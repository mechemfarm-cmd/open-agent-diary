# Release readiness

Open Agent Diary is backend-ready enough for staging, but it should not be announced as a public release until the UI is easier to understand on first launch.

## Current status

- Backend: staged in a clean public tree.
- Data policy: public install starts empty and gathers the operator's own local data.
- Examples: synthetic only.
- License: MIT.
- UI direction: simplified around search, source-record reading, and correction/annotation first. Advanced provenance remains available but is deliberately secondary.
- Remaining release blocker: live user-facing UI review and polish.

## UI improvements before public release

- First-run empty state that explains what the user is looking at and how to import sample data. Basic empty state is present; review in browser with a new user mindset.
- Clear left-to-right mental model: search/browse, source record, correction/inspection tools.
- Better visual hierarchy for raw truth vs derived artifacts.
- Cleaner navigation between memory search, work-trace search, imports, and entry detail.
- Obvious labels for "raw entry", "annotation/correction", "summary", "search memory", and "possible follow-ups".
- Better responsive layout for laptop screens and narrow windows.
- Safer copy for local network binding: default local-only, explain private-network use separately.
- A visible privacy note: runtime data is local and not included in the public repo.

## Suggested UI verification checklist

- Load the app with an empty `data/` directory.
- Import `examples/synthetic-session-import.jsonl`.
- Confirm the UI explains empty/imported states without requiring docs.
- Search memory for `privacy review`.
- Open an entry and verify raw content is clearly primary.
- Generate derived artifacts and verify they are visually secondary.
- Run `agent-diary doctor --json` and expose/describe the result somewhere sensible.
- Test at common widths: 1440px, 1280px, 1024px, and mobile/narrow.


## Tauri desktop shell status

The supported v0.1 release path is local Python server + browser UI. The Tauri shell is experimental/development-only until it can start, configure, and supervise the Python backend as a sidecar. Do not publish desktop bundles as standalone release artifacts before that lifecycle is implemented and smoke-tested.
