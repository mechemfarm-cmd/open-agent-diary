# Semantic Situation Memory

Semantic situation memory is a read-only derived layer over Agent Diary's existing raw entries, work trace, graph facts, belief metadata, conversation briefs, and open-loop artifacts. Raw entries remain authoritative. A situation is an inspectable view for one retrieval purpose; it is not a new truth class and is not injected automatically into agent context.

## Concepts

- `episode`: a bounded continuing project, entity, or problem thread inferred from a topic/entity anchor, timestamps, explicit links, and shared subjects. Episode boundaries are inference, not raw facts.
- `situation`: the current semantic view of an episode for a purpose such as `current_status`, `decision_rationale`, or `next_action`.
- `decision`: an intentional choice with rationale, supporting evidence, and rejected alternatives when available.
- `state`: current status plus effective timestamp, confidence/provisional labels, and supersession/history links.
- `open_question`: unresolved work, blocker, review gate, or question with evidence and a possible resolution condition.
- `evidence_ref`: source pointer to a raw entry, work trace event, graph fact evidence row, or belief source.
- `inference_note`: a derived relationship or grouping note that must not be presented as raw truth.

## Minimal schema

A compiled situation contains:

- `topic` and `purpose`.
- `episode`: anchor, status, candidate count, and subjects.
- `current_state`: current non-superseded items.
- `history`: historical, planned, retracted, or superseded items.
- `decisions`: decision/rationale items.
- `open_questions`: unresolved next actions and blockers.
- `conflicts`: conflicting current candidates shown together.
- `evidence`: bounded recent source candidates.
- `inference_notes`: explicit derived grouping notes.

Every displayed factual item includes `source_refs`. When a relationship is inferred from grouping, it appears under `inference_notes` rather than as fact.

## Example: generic project status

Topic: `Project Atlas`; purpose: `current_status`.

```json
{
  "current_state": [
    {
      "text": "Project Atlas runs on Server Beta",
      "status": "current",
      "timestamp": "2026-09-19T10:00:00+00:00",
      "source_refs": [{"source_kind": "raw_entry", "source_id": "atlas-current"}]
    }
  ],
  "history": [
    {
      "text": "Project Atlas ran on Server Alpha",
      "status": "superseded",
      "source_refs": [{"source_kind": "raw_entry", "source_id": "atlas-old"}]
    }
  ],
  "inference_notes": [
    {"kind": "grouping", "text": "Project Atlas items were grouped by topic and explicit links."}
  ]
}
```

## Example: generic deployment decision

Topic: `Service Cedar`; purpose: `decision_rationale`.

```json
{
  "decisions": [
    {
      "text": "Decision: keep Service Cedar offline until manual approval is recorded",
      "source_refs": [{"source_kind": "raw_entry", "source_id": "cedar-decision"}]
    }
  ],
  "open_questions": [
    {
      "text": "Open question: who approves the deployment window?",
      "source_refs": [{"source_kind": "work_trace", "source_id": "cedar-review-001"}]
    }
  ]
}
```

## Prototype boundaries

The first version is deterministic and local-only. It does not call an LLM, use a vector store, update belief usage, change live recall, or send data to any external service. Empty/new-user diaries produce an explicit empty situation rather than seeded facts.

Classification is generic: source adapters prefer explicit metadata, author/evidence roles, event types, status/supersession fields, and source links. They do not require project-specific names or graph predicate names. Text-only fallback is conservative and may miss decisions or open loops when entries lack role/event metadata.

## Read-only preview

A bounded preview is available through the local CLI and `POST /semantic/preview` for inspection only. Required payload fields are `topic` and `purpose`; `limit` and `char_budget` are capped. The preview returns both rendered text and structured source-linked situation data, and it must not be called automatically from the Hermes recall path without a later review plan.

