# Semantic Memory Evaluation

The offline semantic-memory fixture in `tests/fixtures/semantic-situations.jsonl` is synthetic and schema-driven. It exists to exercise failure modes before any live recall integration.

Each JSONL case contains:

- `case_id`: unique stable identifier.
- `query`: user-style retrieval request.
- `purpose`: one of `current_status`, `decision_rationale`, or `next_action`.
- `expected_elements`: source-linked elements a correct situation should show.
- `required_source_refs`: references that must remain visible, or `none` for empty-corpus cases.
- `known_traps`: stale state, conflicts, unresolved work, unsupported agent narration, inferred links, or empty corpus.

The loader validates required fields, uniqueness, and allowed purposes. The fixture intentionally avoids private transcripts, credentials, real operator names, and deployment-specific project names.

This milestone does not claim measured improvement over existing retrieval. It only establishes the hand-labelled set and schema validation needed for later comparison.
