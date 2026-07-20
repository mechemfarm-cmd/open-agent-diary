# Contributing to Open Agent Diary

Thanks for considering a contribution. Open Agent Diary is early-stage local-first software, so the most useful contributions are small, reviewable, and backed by tests or clear manual verification.

## Development setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

## Run checks

```bash
node --check ui/app.js
PYTHONPATH=src python3 -m compileall -q src scripts tests
PYTHONPATH=src python3 -m unittest -v tests.test_append_entry_slice
agent-diary doctor --json
```

Plain `python3 -m unittest` may not discover the full suite in every environment; use the explicit module command above for now.

## Contribution guidelines

- Keep raw source records authoritative. Generated memory, summaries, open loops, and work traces must remain inspectable secondary layers.
- Do not commit runtime `data/`, local databases, private transcripts, chat exports, tokens, or machine-specific config.
- Use synthetic fixtures for tests and examples.
- Keep browser UI rendering safe: API-controlled/imported fields should use DOM construction and `textContent`, not raw HTML interpolation.
- Preserve local-first safety defaults. Loopback (`127.0.0.1`) should remain the default; LAN exposure needs explicit documentation and caution.

## Pull requests

Please include:

1. What changed and why.
2. Verification commands and results.
3. Any privacy/security impact.
4. Screenshots for visible UI changes when practical.
