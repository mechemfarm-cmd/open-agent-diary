# Changelog

All notable changes to Open Agent Diary will be documented here.

## 0.1.0 - unreleased

Initial public-release candidate.

### Added

- Local Python service with static browser UI.
- Append-only raw entry store plus SQLite metadata/search index.
- Synthetic demo import flow.
- Generated conversation briefs, compressed search memory, open-loop analysis, and work-trace records as inspectable secondary layers.
- `doctor` consistency checks.
- CI checks for backend tests and compile validation.

### Security / privacy

- Loopback-first server defaults and documentation.
- Same-origin CORS behavior instead of wildcard CORS.
- Browser UI XSS hardening for API-controlled/imported fields.
- Public repository contains synthetic examples only; runtime `data/` is ignored.

### Known limitations

- The v0.1 API has unauthenticated write routes; do not expose it casually on LAN/public networks.
- The Tauri shell is experimental/development-only and does not yet manage the Python backend lifecycle.
