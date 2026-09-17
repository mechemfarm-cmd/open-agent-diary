# Release readiness

## Current public status

Open Agent Diary is **early, working open-source software**. It is useful today for local-first, inspectable agent memory and is actively evolving. Early users should keep backups, expect workflows to change, and report confusing or broken behavior.

The supported v0.1 shape is the **local Python server plus the browser UI served by that same process**.

- Backend/API: supported for local-first use.
- Browser UI: supported operator surface.
- Data policy: installs start empty and collect only operator-provided local data.
- Examples: synthetic fixtures only.
- Graph and belief layers: implemented, optional, and actively evaluated through real use; do not claim longitudinal ranking quality yet.
- Tauri desktop shell: experimental/development-only until it starts, configures, and supervises the Python backend as a sidecar.
- Hosted/cloud service: not provided.

## What is ready enough for v0.1

- Local HTTP API, CLI, and served browser UI.
- Raw-entry browsing, work traces, derived artifacts, overlays/corrections, archive support, and read-only doctor checks.
- Optional graph storage, graph extraction queue, evidence inspection, and belief UI/recall routes.
- Synthetic demo import and backend regression tests.

## Deliberate limits

- Write routes are unauthenticated. Bind to loopback by default and use private-network controls deliberately.
- Graph extraction requires an external model worker; it does not run merely because the graph exists.
- A fresh graph/belief list is empty by design.
- Belief recall is not query-aware in v1 and must not be auto-injected on every turn.

## Before announcing a release

1. Run the test suite and compile checks.
2. Validate README quick start in a clean temporary data root.
3. Import synthetic data, search it, run doctor, and inspect the UI.
4. If documenting graph use, enqueue and drain at least one synthetic extraction job with an approved test credential or explicitly mark that path untested.
5. Confirm no runtime data, databases, archives, secrets, or real transcripts are tracked.
6. Check public docs say what is supported, optional, experimental, and still being learned.

See [Getting started](getting-started.md), [Operating guide](operating-guide.md), and [Privacy model](privacy-model.md).