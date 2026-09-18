# Privacy model

Agent Diary is designed for local-first use. A public installation starts empty and gathers only the data the operator imports or writes.

## Principles

- Runtime data lives under `data/` and is ignored by Git.
- Raw entries and work traces remain inspectable files on disk.
- SQLite and FTS indexes accelerate lookup; they are not the hidden source of truth.
- Derived memory artifacts are secondary and linked back to raw entries.
- Users should be able to inspect, correct, or overlay the memory record.

## Optional external processing

The default graph extractor is **not local-only**. It sends each selected source entry's content (up to the extractor's request limit) to the configured OpenAI-compatible provider — OpenRouter by default — so that model can propose entities and facts.

Before enabling extraction, decide whether the selected conversations/work records are acceptable to send to that provider. This is separate from server exposure: the diary can remain bound to `127.0.0.1` while extraction still sends selected text outward.

What remains local: raw-entry files, work traces, SQLite/FTS indexes, graph tables, evidence links, assertion history, belief ranking, and browser UI. The external provider receives extraction inputs and returns proposed structured facts; it does not host the diary database.

If this trade is unacceptable, do not run the extraction worker. You can still use the local archive, search, work traces, manual graph facts, and all non-extraction features.

## Public repository policy

The public repository should contain:

- source code
- tests
- UI assets
- documentation
- synthetic examples

It should not contain:

- real chat exports
- personal memory dumps
- local SQLite databases
- backup archives
- machine-specific runtime configuration
- secrets or access tokens

Before publishing a release, run a privacy grep suitable for your environment and inspect every match.
