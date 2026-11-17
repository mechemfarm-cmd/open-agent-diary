# Machine plane — Stage 2: the ingestion boundary, made real

**Status:** built, not yet reviewed.
**Predecessor:** Stage 1 (`machine-plane-stage1-result.md`), approved as final.

---

## 0. Resolving what "Stage 2" means

Two of this project's own documents disagree, and the disagreement is material
enough to state before anything else:

| Document | What it calls Stage 2 |
|---|---|
| `machine-plane.md` §16 (staged plan) | *privilege separation, proven* |
| `machine-plane-stage1-producer-evaluation.md` §120 | *ingestion boundary and schema* |

They are not in conflict about the goal, only about the numbering. §8 of the
design defines the **protected ingestion boundary**, and the acceptance conditions
it lists — the agent cannot submit a machine event, cannot alter one, authorship
is distinguishable, version mismatch fails closed, partial ingestion is
detectable and resumable — are *exactly* the properties that make privilege
separation real. "Privilege separation, proven" is the test of "the ingestion
boundary"; they are one piece of work with two names.

**Stage 2 is therefore: make the ingestion boundary real and demonstrate the
separation rather than asserting it.** Schema for machine events already exists
in the core and is approved; this stage does not redefine it.

## 1. What already exists (do not rebuild)

The core was approved at `3cbf14a` (validator follow-up at `b3549de`) and already
contains the boundary:

| Module | What it provides |
|---|---|
| `spool.py` | append-only spool; one `write` on an `O_APPEND` fd so records cannot interleave; version-checked envelope; reads return `(start, end, envelope)` and deliberately stop before a trailing partial line, so a restart resumes rather than half-reading |
| `importer.py` | unprivileged importer; stamps `plane=machine` from the **envelope**, never from the payload; the event schema has no `plane` field, so a forged payload has nowhere to put the claim; fails closed on an unreadable record; checkpoint advanced per record |
| `producer.py` | the `Producer` ABC: `collect → normalize → redact → annotate → emit`, with redaction inside the pipeline |
| `capabilities.py` | per-capability reports with intervals and reasons; an unmentioned capability is not available |
| `privacy.py` | `redact_path`, `coarsen_destination`, `sanitize_details` |

What was **not** built, and is this stage's work: nothing has ever produced a
machine event. Every one of those modules has only ever been exercised by test
doubles. Stage 1 measured whether a real producer was viable; Stage 2 is the
producer.

## 2. What this stage adds

1. **A real Linux producer** — `machine_plane/producers/linux_ebpf.py`,
   implementing the approved `Producer` ABC against the bpftrace probes Stage 1
   evaluated. It parses the same `MARK`/`EV` line format those probes emit, so the
   sensor that was measured is the sensor that ships.
2. **The documented gaps, declared rather than discovered.** Stage 1 found six
   things the probe set cannot see. They become explicit `available=False`
   capability reports with reasons — not silence.
3. **The separation, demonstrated.** A proof that the agent cannot write to the
   spool and cannot submit an event, expressed as checks that fail if the
   boundary is weakened.

## 3. The six declared gaps

From the Stage 1 result. Each is a capability the producer reports as
unavailable, with the reason, so a missing event is never mistaken for "nothing
happened":

| Gap | Why |
|---|---|
| rename not observable | no probe attaches to a rename tracepoint |
| unlink not observable | no probe attaches to an unlink tracepoint |
| process exit not observable | no probe attaches to any process-exit tracepoint |
| file paths not captured | `file.bt` does not dereference `args->filename` at all |
| connection destinations not captured | `net.bt` does not read `sockaddr` |
| connect outcomes unresolved | non-blocking sockets return `-115` EINPROGRESS, so success and failure are indistinguishable |

The last one is the subtlest: the probe records *that* a connect was attempted,
and the outcome genuinely cannot be resolved from that event. The producer reports
it as unavailable rather than guessing.

## 4. Privacy position, restated because it is load-bearing

- **Metadata only.** No argv, envp, file contents, buffers, packet payloads or
  sockaddr — ever.
- **One pointer read**, approved and reviewed: `str(args->filename)` on
  `sched_process_exec`, for successfully executed images only. It is a pathname,
  not content. No other pointer is dereferenced anywhere in the probe set.
- **Redaction is inside the pipeline**, before the event reaches the spool. A
  filter applied afterwards cannot deliver a metadata-only guarantee.
- **comm truncates at 15 characters** and is therefore carried as a label only,
  never as identity.

## 5. Acceptance conditions

Carried from design §8, and the things a reviewer should check:

1. the agent cannot submit a machine-plane event;
2. the agent cannot alter an existing machine event;
3. the importer can distinguish recorder-authored events from agent-authored work
   traces;
4. schema or version mismatch **fails closed**;
5. partial ingestion is **detectable and resumable**;
6. every Stage 1 gap is declared as an unavailable capability with a reason;
7. nothing in the producer can carry content into a stored event.

### The proof, and one correction it produced

`stage2/separation-proof.sh` exercises conditions 1–5 against a real root-owned
spool on this host, as the unprivileged user the agent actually runs as. Current
result: **9 passed, 0 failed**, with the spool and its directory removed on exit.

It produced one finding that changes the design's wording. Building it with the
spool at `0600 root:root` — the strictest reading of "root-owned spool" — makes
the **unprivileged importer unable to read it**, so ingestion cannot happen at
all. The deployment shape therefore has to be:

- **root-owned**, and writable **only** by the recorder;
- **group-readable**, so the unprivileged importer can consume it;
- read access is not a boundary violation. The design's conditions are about
  *submitting* and *altering* machine events, not reading them — and the view
  (§12) deliberately shows machine events to the agent.

So "the agent cannot reach the recorder" is precise about *write* and *alter*,
and the proof asserts exactly that. It does not claim the agent cannot read, and
should not, because that would contradict the view.

## 6. What this stage still does not do

- No systemd unit, no installed service, no always-on recorder. This is the
  component, not its deployment.
- No Agent Diary ingestion in this stage — the store is a test double, and no
  Diary file is read or written.
- No Windows or macOS producer. §13.2 keeps Linux first.
- No change to the event schema, which is approved.

## 7. Bounds

- Development only. No probe attached to any host by the test suite; the producer
  is exercised against captured logs, which is why its log-reading mode exists.
- One host was measured (Emily); the producer carries that provenance in its
  capability reasons rather than implying fleet-wide support.
