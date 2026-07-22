# Release readiness

Open Agent Diary's supported v0.1 public shape is the **local Python server plus the browser UI served by that same process**.

The browser UI is included in the repository and is part of the supported v0.1 operator experience. The UI is intentionally simple: search or browse, open the raw source record, then inspect/correct supporting layers without hiding the original record.

## Current public status

- **Backend/API:** Included and supported for local-first use.
- **Browser UI:** Included and supported as the v0.1 human-facing surface.
- **Data policy:** Public installs start empty and gather the operator's own local data.
- **Examples:** Synthetic fixtures only; no real chat history ships with the project.
- **License:** MIT.
- **Release object:** The public repository is available. Formal tagged GitHub releases may be added later.
- **Desktop shell:** Source exists under `src-tauri/`, but standalone desktop bundles are not the supported release artifact yet.

## What is ready enough for v0.1

- Local HTTP API and CLI.
- Static browser UI served from the backend.
- Raw source-record browsing and detail view.
- Memory search with source-record navigation.
- Pagination and date-like search behavior.
- Annotation/correction overlays without mutating raw entries.
- Generated conversation briefs, compressed search memory, open-loop analysis, and work traces as secondary/inspectable layers.
- Read-only `doctor` consistency checks.
- Transparent archived-entry storage for old monthly raw-entry files.
- Synthetic demo import path.
- Backend regression tests and CI.

## Known limitations / expectations

- The v0.1 API has unauthenticated write routes. Bind to `127.0.0.1` by default. Only bind to LAN/Tailscale/private interfaces on trusted networks with firewall/Tailscale restrictions.
- The Tauri shell is experimental/development-only until it can start, configure, and supervise the Python backend as a sidecar. Do not publish desktop bundles as standalone release artifacts before that lifecycle is implemented and smoke-tested.
- The UI is intentionally operator-focused rather than polished consumer software. It prioritizes truthful inspection over visual flash.
- There is no hosted/cloud service. Agent Diary is local-first software.

## Public-repo verification checklist

Before announcing a public release or creating a tagged GitHub Release:

- Run backend tests:

  ```bash
  PYTHONPATH=src python3 -m unittest -v tests.test_append_entry_slice
  PYTHONPATH=src python3 -m compileall -q src scripts tests
  ```

- Run consistency checks:

  ```bash
  PYTHONPATH=src python3 -m agent_diary.cli.main --json doctor
  ```

- Smoke-test the browser UI:
  - load with an empty `data/` directory
  - import `examples/synthetic-session-import.jsonl`
  - search for `privacy review`
  - open an entry and confirm raw content is primary
  - add an annotation/correction overlay
  - check Summary and Advanced tabs
  - test common widths: 1440px, 1280px, 1024px, and narrow/mobile

- Confirm public hygiene:
  - no runtime `data/entries`, `data/work_trace`, overlays, artifacts, imports, or real SQLite database committed
  - examples remain synthetic
  - package metadata points to `mechemfarm-cmd/open-agent-diary`
