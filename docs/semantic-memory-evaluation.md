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

This milestone does not claim measured improvement over existing retrieval. It establishes the hand-labelled set, schema validation, and deterministic comparison metrics needed for review. `compare_semantic_retrieval` records baseline coverage, situation coverage, source-reference coverage, stale/conflict handling, unsupported-claim count, output sizes, and baseline failure categories.

## Review gate

A semantic preview is acceptable for manual inspection only when:

- required element and source-reference coverage improve over baseline retrieval on the synthetic set;
- unsupported claims stay at zero, or every exception is documented;
- stale, superseded, resolved, conflicting, planned, and inferred items remain explicitly labelled;
- rendered output fits the configured budget and remains source-linked;
- no belief usage, live recall state, raw entries, or work trace records are mutated.

The current fixture is synthetic and cross-domain, so it is a safety/regression gate rather than proof of production recall quality. A future opt-in Hermes integration requires a separate plan and a new evaluation on representative private data without committing that data.

