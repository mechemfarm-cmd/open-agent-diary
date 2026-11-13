# Machine plane — Stage 1: producer evaluation

**Status:** proposal, not approved. Requires a bounded review before anything is
attached to a running host.

**Question this stage answers:** can a real producer observe an agent's activity
accurately enough, and cheaply enough, to be worth building on — and what does it
cost the machine it runs on?

This is an evidence-gathering stage. It produces numbers and a go/no-go
recommendation for Stage 2. It does not produce a product, a package, or anything
that writes to Agent Diary.

## What is already established (Stage 0)

Recorded in `machine-plane-stage0-probes.md`; not repeated here.

- `bpftrace` and `strace` are installed on Emily and Art. Kernels 6.8 and 7.0,
  BTF present, cgroup v2 mounted, no kernel lockdown.
- No tetragon, falco, sysdig or auditd. Nothing to adopt; a sensor would be new.
- `unprivileged_bpf_disabled=2` and `perf_event_paranoid=4` on both. A producer
  as an ordinary process cannot attach. Anything in this stage runs privileged.
- `/usr/bin/env echo hi` produced 16 `execve` entries, 15 of them failed `$PATH`
  probes. Counting syscalls is not counting actions; outcome is the boundary.
- Emily shows roughly 1,600 `openat` in 8 seconds, Art shows 1. The profile is
  host- and workload-specific, so no per-host rate threshold is defensible.
- Process names at the tracepoint truncate at 15 characters, so names cannot be
  used as identity.

## What this stage would do

Attach read-only `bpftrace` probes to **Emily only**, for two bounded sessions,
and compare what the kernel reports against an **independent action manifest** —
a list of actions the scripted workload intends to perform. Agent Diary is not
read, exported or touched at all in this stage.

**The executable form of this proposal is `stage1/run-sheet.md`**, together with
`stage1/probes/*.bt`, `stage1/action-manifest.json` and `stage1/workload.sh`.
Where the two documents differ, the run sheet is authoritative: it holds the
exact commands and is the thing that must be reviewed before attach.

Emily is chosen because it is the busiest host and the one with the worst-case
open rate. If the approach cannot cope with Emily, it will not cope anywhere.

### Measurement 1 — coverage and attribution

Run a scripted, known workload through a Hermes session: execute a few commands,
write and read a handful of files, make a couple of network requests, spawn a
background process, and deliberately fail one command.

Then ask, of the events the kernel recorded:

- How many carry a process identity that resolves to the Hermes run's cgroup,
  and how many do not?
- How many of the scripted actions are represented in the event stream at all?
- How many background or subprocess actions are attributed to the right run?
- How many events are noise — tracepoint firings that did not correspond to an
  action anyone took?

This is the join-quality question, and it is the hard one. eBPF capture is not
the risk; correlation is.

### Measurement 2 — loss under load

Repeat under sustained activity, with the ring buffer deliberately sized smaller
than the observed event rate, to see:

- whether losses are detected at all;
- whether the loss is reported as a bounded interval or silently disappears;
- what the recorder does when the sink cannot keep up.

A recorder that cannot say "I missed this" is the failure mode the whole design
exists to prevent, so it is measured rather than assumed.

### Measurement 3 — cost

For each session, record:

- CPU percentage attributable to the probe;
- resident memory;
- bytes written, and to where;
- effect on the agent's own latency, if measurable.

A sensor that changes the behaviour it is measuring is not neutral.

### Measurement 4 — privilege and content

- Which capabilities are actually required, established by attempting the same
  probes with the narrowest privilege that works.
- Whether any probe can be made to capture file contents, environment variables,
  or plaintext model traffic. It must not. If a probe cannot be scoped to avoid
  doing so, that is a finding that changes the design.

## Bounds

These are conditions of running the stage, not aspirations.

- **One host.** Emily. Not Art, not the rest of the fleet.
- **Time-boxed.** Two sessions, each under ten minutes, plus analysis.
- **Read-only.** No writes to the machine outside a scratch output directory.
  No service installed, no package added, no persistent probe.
- **Metadata only.** No file contents, no command-line arguments containing
  secrets, no environment captures, no packet payloads.
- **No Agent Diary.** Nothing in this stage is ingested into anyone's memory.
  The core's importer is not used, and no Diary file is read. The comparison
  baseline is the action manifest, not a diary export.
- **Explicit markers.** Each session opens with a start marker and closes with a
  stop marker, so a gap is distinguishable from silence.
- **Removal is one step.** The probe is killed and the output directory deleted.
  If removal needs more than that, the design is wrong.

## What this stage will not do

- Build or install the privileged switch component.
- Define the ingestion boundary into Agent Diary.
- Add a producer to the repository.
- Run on any host Bill does not name.
- Continue past the time box to "get a better number".

## Deliverable

A written result, in the same form as the Stage 0 probes: what was measured, the
numbers, what surprised us, and a recommendation.

The recommendation is one of:

- **proceed** to Stage 2 (ingestion boundary and schema);
- **proceed with changes** — naming what must change first;
- **do not proceed**, with the measurement that killed it.

A "do not proceed" is a successful outcome. The purpose is to find out, not to
produce a yes.

## Risks, stated plainly

- **The numbers may be good on Emily and meaningless elsewhere.** One host is a
  deliberate limit for cost, and it means the results generalise weakly. Stage 2
  would need a second host.
- **A synthetic workload is easier than a real one.** A scripted session is not
  an agent doing something unexpected. Coverage measured this way is optimistic.
- **Privileged probes are a real change to a production machine.** Emily runs the
  gateway. The probes are read-only and time-boxed, but they are not free, and
  the host is not a lab.
- **bpftrace may not be the right instrument.** If attribution proves poor, the
  finding is about the approach, not about bpftrace specifically, and should be
  read that way.

## Decision required before this runs

Approval from Alice as a bounded review, and from Bill for the host and the
window. Neither is implied by the core approval at `3cbf14a`.

The run sheet at `stage1/run-sheet.md` is the checklist that has to be satisfied
first. It is not a substitute for Bill naming a window, and no window exists
until he does.
