# Security Policy

Open Agent Diary is local-first software for private agent memory and work traces. Treat a running diary as sensitive: it may contain private conversations, local paths, operational notes, and generated summaries.

## Supported versions

The project is pre-1.0. Security fixes target the current `main` branch until formal releases begin.

## Reporting a vulnerability

Please report suspected vulnerabilities privately to the project maintainer before public disclosure. If GitHub private vulnerability reporting is enabled for this repository, use that channel. Otherwise, open a minimal GitHub issue requesting a private contact path without including exploit details.

Please include:

- Affected commit/version.
- What is exposed or corrupted.
- Minimal reproduction steps using synthetic data.
- Whether the issue requires non-default LAN/public exposure.

## Local-first safety notes

- The safe default is binding to `127.0.0.1`.
- Binding to `0.0.0.0` or a LAN/Tailscale address is an explicit risk decision. Use firewall/Tailscale restrictions and trusted networks only.
- The v0.1 API has unauthenticated write routes. CORS is not authentication.
- Do not commit runtime `data/`, SQLite databases, private transcripts, chat exports, secrets, or machine-specific config.

## Desktop shell status

The supported v0.1 UI is the browser UI served by the local Python backend. The Tauri shell is experimental/development-only until it starts, configures, and supervises the Python backend as a sidecar. Do not treat generated desktop bundles as standalone secure release artifacts yet.
