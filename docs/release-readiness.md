# Release readiness

Open Agent Diary is backend-ready enough for staging, but it should not be announced as a public release until the UI is easier to understand on first launch.

## Current status

- Backend: staged in a clean public tree.
- Data policy: public install starts empty and gathers the operator's own local data.
- Examples: synthetic only.
- License: MIT.
- Remaining release blocker: UI polish and first-run clarity.

## UI improvements before public release

- First-run empty state that explains what the user is looking at and how to import sample data.
- Clear left-to-right mental model: entries, details, derived memory, work traces.
- Better visual hierarchy for raw truth vs derived artifacts.
- Cleaner navigation between memory search, work-trace search, imports, and entry detail.
- Obvious labels for "raw entry", "overlay", "brief", "compressed memory", and "open loops".
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
