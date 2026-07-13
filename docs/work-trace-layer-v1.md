# Work Trace Layer V1

## Purpose

Agent Diary currently preserves conversation truth well enough to answer:

- what User said
- what Assistant said
- what later derived artifacts inferred

That is not the same as preserving what Assistant actually did between those messages.

This doc defines a first work-trace layer whose job is to capture meaningful execution activity in a searchable, non-destructive way.

## Position In The Model

The product should treat these as distinct layers:

1. conversation truth
2. work trace
3. derived interpretation

### 1) Conversation truth

Authoritative raw human/agent exchange.

Examples:

- Telegram direct messages
- imported session transcript chunks
- manual diary notes

### 2) Work trace

Structured records of meaningful agent work between visible messages.

Examples:

- inspected files
- ran commands
- edited code
- made a decision
- hit a blocker
- completed a handoff

### 3) Derived interpretation

Secondary artifacts computed from truth and/or work trace.

Examples:

- compressed-memory artifacts
- conversation briefs
- open loops
- future trend/mood/topic artifacts

Conversation truth remains authoritative for what was said.
Work trace remains authoritative for what was done.
Derived interpretation remains secondary.

## What This Is Not

This is not a chain-of-thought dump.

V1 should not try to store:

- hidden token-by-token reasoning
- every tiny observation or UI repaint
- full stdout/stderr for every command by default
- redundant copies of whole files when a path + diff summary would do

The goal is operational provenance, not exhaustive mental exhaust.

## V1 Event Types

V1 does not need a rigid global ontology, but these event classes are worth treating as first-class:

- `observation`
  - looked at something important
  - example: inspected a failing test file, reviewed a PR diff, checked a timeline entry
- `action`
  - did something meaningful but not necessarily shell-based
  - example: refreshed derived artifacts, imported a transcript batch, applied a scope
- `command`
  - ran a command or script
  - example: `python3 -m unittest ...`, `agent-diary import-telegram-direct ...`
- `file_change`
  - created, edited, or removed a file
  - example: changed `ui/app.js`, added `docs/work-trace-layer-v1.md`
- `test_run`
  - verification step with outcome
  - example: unit tests passed, lint failed, syntax check succeeded
- `decision`
  - committed to a direction after evaluating options
  - example: preserve raw truth as authority, defer nav-column tab strip, stop UI churn
- `blocker`
  - could not proceed cleanly
  - example: browser driver issue, missing session file, auth failure
- `handoff`
  - state packaged for the next human or agent step
  - example: "UI stable enough to stop", "next focus is ingestion/chunking"

These types are intentionally plain-language and implementation-friendly.

## Common Fields

Every work-trace record should preserve a compact common envelope.

Recommended fields:

- `event_id`
- `created_at`
- `event_type`
- `summary`
- `session_key` or equivalent run/session context
- `task_id` or work-group id when available
- `project`
- `source_surface`
- `actor`
- `details` object for event-specific payload
- `related_entry_ids`
- `related_artifact_ids`
- `related_paths`
- `tags`

### Field intent

- `summary`
  - one short, searchable human-readable sentence
- `project`
  - keeps unrelated work separable across repos/domains
- `source_surface`
  - where the work came from
  - examples: `telegram-direct`, `cli`, `web-ui`
- `actor`
  - usually the agent identity, later possibly human/tool/subagent variants
- `details`
  - structured payload per event type
- `related_*`
  - keeps trace linked back into truth, artifacts, and file-level work

## Event-Specific Guidance

### observation

Good payload:

- what was inspected
- why it mattered
- optional short finding

Avoid storing giant raw payloads when a path, identifier, and finding are enough.

### command

Store:

- command string or normalized argv
- cwd when relevant
- success/failure
- short outcome summary
- optional references to fuller logs

Do not require full stdout in the main record unless the output itself is the durable fact.

### file_change

Store:

- path
- change kind: create/update/delete
- short summary of what changed
- optional diff/hash references later

This is one of the most valuable event types because it bridges conversation intent to actual repo movement.

### test_run

Store:

- test or check name
- command
- result: pass/fail
- short outcome

The important thing is preserving that verification happened, not only that code changed.

### decision

Store:

- the decision
- short rationale
- optional alternatives considered

This is critical because many important project turns are not obvious from raw commands alone.

### blocker

Store:

- what blocked progress
- whether the blocker is environmental, product, or workflow-related
- what would unblock it

### handoff

Store:

- current state
- next recommended step
- important warnings or assumptions

This should make later resumption materially easier.

## Searchability Requirements

If this layer exists but is not searchable, it does not solve the problem.

Minimum search value:

- search by summary text
- search by event type
- search by path
- search by project
- search by time window
- search by related entry/artifact id

Useful examples:

- "show me when Assistant changed the timeline layout"
- "find the last test run touching append_entry_slice"
- "show work trace around this import batch"
- "what decisions were made before the UI was left alone"

## V1 Capture Rule

Do not try to capture everything.

V1 should capture only meaningful work events that help answer:

- what happened
- why it happened
- what changed
- how it was verified
- what remains unresolved

A good rule of thumb:

- if it materially changed repo state, task state, or operator understanding, capture it
- if it was transient noise with no later explanatory value, skip it

## Storage Direction

V1 should stay compatible with the append-only philosophy.

Recommended direction:

- store work-trace records as append-only event records
- keep them separate from raw conversation entries
- allow explicit linkage between work-trace records and raw entries/artifacts

That separation matters.

Conversation truth should not be polluted with synthetic execution chatter just because the chatter is useful.

## Relationship To The Current Artifact Model

Work trace is not just another derived artifact.

Why:

- a command run is not an interpretation of a conversation
- a file edit is not a summary
- a blocker is not a compressed-memory artifact

The cleaner model is:

- raw entries for truth
- work-trace records for execution provenance
- artifacts for interpretation and helper layers

Later features may derive artifacts from work trace, but the trace itself should remain a first-class record stream.

## V1 Non-Goals

Not for the first pass:

- full agent introspection logging
- universal replay of every tool call
- perfectly normalized schema for every future workflow
- heavy analytics over work trace before basic search exists
- merging work trace into the visible conversation timeline by default

## Recommended Next Step

The next practical move is not UI-first.

It is:

1. define a minimal append-only work-trace record shape
2. capture a few high-value event types first
3. make them searchable
4. only then decide how prominently they belong in the UI

Best first captured events:

- `file_change`
- `command`
- `test_run`
- `decision`
- `blocker`

That would already close a major gap between "what we said" and "what actually happened."
