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
- the adapter from an exported work trace to agent claims;
- the export format: a writer and a validator for it;
- the hash chain and checkpoint verification;
- off-box checkpoint publication, with the trust domain made explicit;
- the kill switch's authority, state and fail-direction model;
- the view: ranked exceptions, each in one plain sentence.

What does not exist:

- any platform producer (Linux, Windows or macOS);
- a source that reads a real work trace and writes it out — that piece belongs
  outside this package, on the far side of an export someone chose to run;
- a privileged component to actually carry out a switch action — the model
  decides what should happen, nothing here executes it;
- any UI beyond the text page.

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
    claims.py        adapting an exported work trace into claims
    export.py        writing and validating the neutral export format
    chain.py         tamper evidence, and the limit of it
    checkpoints.py   where a checkpoint is worth anything, and where it is not
    killswitch.py    the switch as an architecture: authority, fail direction
    view.py          ranked exceptions, in plain language
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

The third renders that as the page a person would actually read:

```bash
PYTHONPATH=src python3 -m machine_plane.cli view --allowed-root /home/x/work
```

The fourth reads an exported work trace and reports what it could and could not
adapt:

```bash
PYTHONPATH=src python3 -m machine_plane.cli adapt --export /path/to/work-trace.jsonl
```

The fifth writes an export and validates the result, which is the loop a real
user takes:

```bash
PYTHONPATH=src python3 -m machine_plane.cli export --export in.jsonl --out out.jsonl
```

The sixth shows what a checkpoint is worth depending on where it lives:

```bash
PYTHONPATH=src python3 -m machine_plane.cli integrity
```

```
the same intact chain, judged by where its checkpoint lives:
  same_host        evidence=False
  unknown          evidence=False
  separate_host    evidence=True

now delete the tail and check again:
  against an off-box checkpoint : status=truncated
                                  evidence=False
  with no checkpoint at all     : status=ok
                                  evidence=False
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

**A comparison that cannot be made is not a match.** Redaction can drop a
target, and then there is nothing to compare; the event is reported ambiguous.
But when only one side is dropped the answer is decidable — a surviving target
proves the path was inside the allowed roots, a dropped one proves it was not —
and treating that as undecidable made every unrelated event look ambiguous.

**The reason a target is missing decides what follows.** A bare `None` cannot
carry that, because it means three different things: the action had no target, a
target was removed by redaction, or nobody recorded one. Every record therefore
carries a `TargetState`, and only an *explicit* absence on both sides counts as
agreement. Reading `None` as agreement let a redacted private path confirm
against a claim that never had a target — a confirmed match the system had not
earned.

**A disagreement is categorised, never counted.** Eight statuses, and the view
ranks the investigable ones. A single "divergence" number tells you nothing
about what kind of problem you have.

**A gap says what it affects, and whether it has ended.** A gap with no scope
claims *global* blindness, which is almost never true: losing file tracing does
not make an exec observation unobservable, and treating it as though it did turns
real evidence into "unmatched due to gap". So a gap carries the capabilities it
affects and, where the producer knows it, when coverage resumed — a dropped
buffer is a bounded loss, not permanent blindness. Before the first
`recorder_started` nothing is known, and that interval counts as blind rather
than as quiet.

**Wall time is a limitation, so it is handled rather than assumed away.** An
export carries wall times, not monotonic ones, so correlation compares on wall
time — and a clock step can move a claim across a start, stop or gap boundary
without anything else noticing. A `clock_jump` therefore marks the interval as
unreliable, and claims inside it are reported as gaps. A silent wrong placement
is worse than an honest "this comparison is not trustworthy".

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

**The adapter cannot reach a database, and that is enforced by its shape.** It
opens one kind of thing — a text file of JSON lines. It imports no database
driver, knows where no Diary lives, and cannot be pointed at one. Whoever runs
the export decides what leaves their machine. A test reads the module's source
and asserts the forbidden names never appear, so the boundary survives future
edits.

**A checkpoint's worth depends entirely on where it lives.** Same host as the
chain, and it proves nothing — whoever can rewrite the chain can rewrite the
checkpoint beside it. So the default trust domain is `UNKNOWN`, never
`SEPARATE_HOST`: assuming the good case would make every downstream claim wrong
by default, invisibly. `assess()` exposes an `evidence` flag that is true only
when the chain verifies *and* the checkpoint is genuinely off-box, which is the
code refusing to let the document use words the evidence cannot support.

**The kill switch has no default fail direction.** Whether an unreachable
controller leaves the agent running or stops it is a decision someone has to make
out loud — both are defensible, and the wrong one is discovered at the worst
moment. So the constructor refuses to guess. Only registered controllers may
request an action, and an agent asking for its own pause is refused rather than
logged.

**The transition table is a constraint, not a label.** An action is refused
unless the switch is actually in the state that action starts from, so the model
cannot emit a sequence no real switch could follow. Asking what *would* happen if
the controller went away is a named prediction
(`predicted_state_on_controller_loss`); applying it is a separate call that
records an unverified transition. Loss itself needs a lease with a timeout —
without one, "the controller is gone" is an opinion.

**A switch is not `proven` until a registered observer says so.** "Stopped", said
by the thing that was meant to stop, is not evidence that it did. The package
cannot verify that an observer is genuinely outside the scope — only the caller
can, by deciding who goes in the registration list — so it does the part it can:
an observation counts only from a registered observer that declares itself an
external verification. A self-report, or a test calling the method directly, is
recorded and does not count. The old API accepted a `verified_externally=True`
boolean from any caller, which is an assertion rather than an observation, and it
is gone.

**The switch model executes nothing.** It decides authority, state and plans; the
privileged component that issues real commands is separate work. A test asserts
the module never grows a `subprocess` import.

**The export format is three required fields and three optional ones.** Every
field not written is a field that cannot leak. Empty optional fields are omitted
rather than written as nulls, so a reader cannot mistake "no target recorded" for
"target recorded as nothing". The validator reports unknown fields rather than
rejecting them — a validator that refuses a producer's own fields would simply be
bypassed.

**The exporter cannot read a Diary either.** It writes already-neutral records
and validates files someone else produced; it knows no database. The piece that
opens a real store belongs outside, on the far side of an export step someone
chose to run. That is what lets this package be installed without granting it
access to anyone's memory, and a test asserts the forbidden names never appear
in its source.

**Nothing is dropped silently, on either side.** A record the adapter cannot use
is reported with its line number and a reason. Skipping it quietly would make an
unadaptable claim indistinguishable from a claim that never existed — the same
failure the gap events exist to prevent on the machine side.

## Dependencies

None. Standard library only, like the core project.
