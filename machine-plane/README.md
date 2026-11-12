# Agent Diary — Machine Plane

A machine-observed evidence plane for Agent Diary.

Agent Diary records what an agent *said* it did. This records what the machine
*observed happening*, independently, and compares the two. The comparison is the
point: when the accounts agree you have a history worth trusting, and when they
disagree you have the most useful signal in agent safety.

Design document: `docs/plans/machine-plane.md` on branch `docs/machine-plane`.

## Status

**Stage 1, in progress.** Nothing here has run against a real kernel yet.

What exists:

- the event record and its envelope;
- neutral, platform-independent identity;
- capability and coverage reporting;
- producer-side redaction;
- the producer pipeline;
- the protected spool and the unprivileged importer;
- correlation, joining machine records to agent claims by identity;
- the hash chain and checkpoint verification.

What does not exist:

- any platform producer (Linux, Windows or macOS);
- an adapter from Agent Diary work traces to agent claims — the shape is
  defined (`AgentClaim`), but nothing reads the Diary yet;
- checkpoint storage off-box, without which the chain proves less than it
  appears to;
- the view, and the kill switch.

This package is deliberately the part that is the same everywhere. The sensor is
not, and cannot be: it is a per-platform problem, and no sensor has been chosen.
Stage 0 — evaluating existing sensors — has not been run.

## Layout

```
machine-plane/
  src/machine_plane/
    events.py        the record: types, envelope, provenance, storage guard
    identity.py      neutral process identity and nestable isolation domains
    capabilities.py  per-capability reporting with coverage intervals
    privacy.py       redaction, applied before anything is written
    producer.py      the pipeline: collect -> normalize -> redact -> annotate -> emit
    spool.py         the protected, append-only ingestion boundary
    importer.py      the unprivileged importer
    correlation.py   joining the agent's account to the machine's
    chain.py         tamper evidence, and the limit of it
    testing.py       test doubles so a producer can be built without a kernel
    cli.py           demos, so the system is observable without a sensor
  tests/
```

## Running it

```bash
cd machine-plane
PYTHONPATH=src python3 -m unittest discover -s tests -t tests
```

Two demos. The first runs a mock producer through the whole pipeline and prints
what survived redaction:

```bash
PYTHONPATH=src python3 -m machine_plane.cli demo --allowed-root /home/x/work
```

The second joins a machine record to a set of agent claims and prints the result
by category:

```bash
PYTHONPATH=src python3 -m machine_plane.cli correlate --allowed-root /home/x/work
```

## Decisions worth knowing

These were not arbitrary, and several came out of getting them wrong first.

**Privacy is enforced at the write boundary, not at construction.** A producer
must be able to build an event that still carries a command line, because
redaction happens after normalisation. Enforcing the metadata-only rule in the
constructor made the redaction stage unreachable. `validate_for_storage()` is
the guard, and the spool calls it before writing, so the guarantee holds even if
a producer skips the pipeline.

**Record identity is position, not content.** Two genuinely different events can
redact to identical content — two different paths both dropped by policy — and
deduplicating on content silently merges them into one. A wrong merge is the same
class of error as a wrong match. So the dedupe key is the record's offset in the
spool, and `event_id` stays content-derived for correlation only.

**Ingestion fails closed.** An unreadable record stops the import rather than
being skipped. A skipped record is indistinguishable from an event that never
happened. The checkpoint advances after each record, so an interruption resumes
at the last record actually stored.

**The importer cannot forge a machine event.** The `plane` field is stamped by
the importer from the spool envelope, and the event schema has no `plane` field
at all — so a payload has nowhere to put the claim.

**Gaps are first-class.** Coverage is part of the record, not a footnote. A
missing event must be distinguishable from "nothing happened", or the comparison
means nothing.

**Capabilities are reported individually, with intervals.** One kernel version
number does not imply uniform capability, and an unmentioned capability is never
read as support.

**File and network semantics are kept distinct.** Opening a file for writing is
not the same as modifying its contents, and `file_changed` would erase the
difference that makes an effect worth investigating.

**Correlation matches on identity, not on time.** A confirmed match requires the
run identity, the action and the target to agree. Time only decides whether an
effect landed inside the run's window or arrived late; it never promotes a match
on its own.

**Both accounts are redacted before they are compared.** The agent's declared
target goes through the same policy as the observed one, so a basename is not
matched against a full path.

**A comparison that cannot be made is not a match.** When redaction drops both
targets there is nothing to compare, and the event is reported ambiguous. But
when only one side is dropped the answer is decidable — a surviving target
proves the path was inside the allowed roots, a dropped one proves it was not —
and treating that as undecidable made every unrelated event look ambiguous.

**A disagreement is categorised, never counted.** Eight statuses, and the view
ranks the investigable ones. A single "divergence" number tells you nothing
about what kind of problem you have.

**Claims are never swallowed to tidy the summary.** When an event's target was
dropped, every claim of that action type is equally incomparable to it, so
consuming them would let one un-attributable event absorb unrelated claims. Both
sides are reported and the reason codes explain why.

**A hash chain answers two different questions, and only one of them locally.**
`verify(chain)` detects modification or reordering of the links that survive.
It cannot detect that an earlier segment was *deleted*, because a valid prefix
of a valid chain is itself a valid chain — there is a test asserting exactly
that, next to one asserting the same prefix fails against a checkpoint. Off-box
checkpoints are therefore required before the words *evidence*, *integrity* or
*flight recorder* are used, and the code refuses to blur them together.

**The chain stores event ids, not event bodies.** That is sufficient, because an
event id is content-derived: altering an event changes its id, which breaks the
chain.

## Dependencies

None. Standard library only, like the core project.
