# Open Loops Producer V1 (Design Note)

## Purpose
Define the first concrete analytic producer: unresolved concerns / open loops.

This is a secondary interpretation layer. It must never replace raw truth.

## What counts as an open loop

An open loop is a concern, commitment, question, or pending decision that appears unresolved in the available source entries.

V1 examples:

- explicit TODO/follow-up not yet completed
- unanswered question with stated importance
- pending task with owner/time intent but no closure signal
- recurring concern mentioned across entries without resolution

## What does NOT count

- generic topic mentions with no unresolved action/concern
- completed/closed items with clear closure language
- speculative mood-only interpretation with no concrete unresolved thread
- concerns inferred without source grounding in raw entries

## Artifact placement and truth boundary

Use `artifact_type = "analysis:open-loop"`.

- raw entries remain authoritative record
- open-loop artifacts are derived interpretations
- each loop record must point back to supporting raw entries

## Minimum artifact payload shape (content)

Store JSON in `artifact.content` with this minimum shape:

```json
{
  "loops": [
    {
      "loop_id": "loop_<stable-ish-id>",
      "title": "Short unresolved concern label",
      "status": "open",
      "summary": "1-2 sentence explanation of what remains unresolved.",
      "supporting_entry_ids": ["entry_..."],
      "evidence_snippets": [
        {
          "entry_id": "entry_...",
          "quote": "short source excerpt"
        }
      ],
      "signals": {
        "strength": "low|medium|high",
        "confidence": 0.0
      },
      "first_seen_at": "ISO-8601",
      "last_seen_at": "ISO-8601"
    }
  ]
}
```

Notes:

- `loop_id` needs stability within one producer run/window, not global permanence yet.
- `status` can stay `open` only for V1 producer output.
- `quote` should be short and attributable; raw entry is still the truth source.

## Metadata requirements (artifact.metadata)

At minimum preserve:

- `schema_version`: e.g. `"open-loop.v1"`
- `source_entry_ids`: all entries considered
- `analysis_window`: optional `{ "start": ..., "end": ... }`
- `method`: producer name/model
- `method_version`: producer logic/model version
- `generated_at`: derivation timestamp

## Multi-entry lineage rule

Open loops are often multi-entry interpretations.

- `entry_id` field on artifact stays as primary anchor (choose newest source entry in window for now)
- full lineage must be carried in `metadata.source_entry_ids`
- each loop must also include `supporting_entry_ids`

This keeps compatibility with current artifact storage while preserving traceability.

## Confidence/strength for V1

Keep V1 scoring simple and explainable:

- `strength` bucket: `low` / `medium` / `high`
- `confidence` float: `0.0 .. 1.0` (optional but recommended)

Interpretation:

- `strength` is coarse user-facing salience
- `confidence` is producer certainty, not truth certainty

No strict calibration required in first implementation.

## UI treatment guidance (later)

When surfaced in UI:

- show loops as secondary “analysis” cards/panels
- always link loop evidence back to raw entry detail
- never render loop summary as the diary body itself

## Out of scope for this step

- implementing producer logic
- closure lifecycle/state machine design
- cross-producer ontology unification
- emotional scoring subsystem
