# Future Derived Data Model (Backend Note)

## Purpose
Keep Agent Diary open for future derived views (charts, mood trends, longitudinal summaries) without weakening the truth model.

## Truth Model (unchanged)

- `raw_entry` files are authoritative records.
- future `work-trace` records capture meaningful execution activity without replacing conversation truth.
- `compressed-memory` artifacts are retrieval aids.
- future analytics/derived signals are **secondary artifacts**, never replacement truth.

Keep these layers separate:

1. conversation truth
2. work trace
3. derived interpretation

See also: `docs/work-trace-layer-v1.md`

## Future derived artifact classes (examples)

Possible artifact families this architecture should support later:

- `compressed-memory`: retrieval summaries/snippets for agent recall
- `analysis:mood`: inferred mood/emotional signal over one or more entries
- `analysis:trend`: longitudinal trend markers (frequency, topics, shifts)
- `analysis:timeline-summary`: period rollups (day/week/month summaries)
- `analysis:graph-signal`: graph-ready aggregates/points/edges for UI charts

These are examples, not a locked enum.

## Placement rule

Derived outputs belong in artifact records (`artifacts/` + index linkage), not in raw entries.

Raw entries remain immutable source text. Derived outputs may be recomputed, replaced, or versioned as new interpretations.

Work-trace records are also not raw entries, but they are not merely derived artifacts either. They are their own execution-provenance layer and should remain separable from both conversation truth and secondary interpretation.

## Linkage rule

Every derived artifact must maintain explicit lineage:

- primary anchor: `entry_id` (single-entry artifacts)
- optional source set in metadata for multi-entry artifacts

Recommended metadata keys for lineage and reproducibility:

- `source_entry_ids`: list of source entry ids used by this interpretation
- `analysis_window`: optional time window (start/end) for rollups
- `schema_version`: version of artifact payload shape
- `method`: model/tool name used to derive artifact
- `method_version`: version/hash of deriving logic
- `generated_at`: timestamp for derivation event
- `confidence`: optional score if applicable

## Minimal metadata convention to preserve now

For new artifact producers, reserve these metadata keys when relevant:

- `schema_version`
- `source_entry_ids`
- `method`
- `method_version`
- `generated_at`

No hard validation is required in this pass; this is a compatibility contract.

## What NOT to hard-code yet

- no fixed ontology/taxonomy for all future analysis types
- no rigid multi-table analytics schema
- no mandatory confidence semantics
- no requirement that all derived artifacts share one payload structure
- no chart/mood-specific backend subsystem yet

## Practical next-step boundary

When future derived features begin, first add narrow producer-specific artifact conventions while preserving:

1. raw-entry authority
2. explicit source linkage
3. separability between truth and interpretation
