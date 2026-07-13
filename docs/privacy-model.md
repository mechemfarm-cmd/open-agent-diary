# Privacy model

Agent Diary is designed for local-first use. A public installation starts empty and gathers only the data the operator imports or writes.

## Principles

- Runtime data lives under `data/` and is ignored by Git.
- Raw entries and work traces remain inspectable files on disk.
- SQLite and FTS indexes accelerate lookup; they are not the hidden source of truth.
- Derived memory artifacts are secondary and linked back to raw entries.
- Users should be able to inspect, correct, or overlay the memory record.

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
