# Agent Diary — Machine Plane

**Status:** design sketch, revision 2. Nothing is built.

**Revision 1 was reviewed** (Alice, branch `docs/machine-plane`, tip `64feaa1`).
Its central finding is accepted: revision 1 described an observability pipeline
while claiming a trustworthy audit product. This revision separates those two
things, states the trust boundary explicitly, and replaces the guarantee,
privacy, correlation, ingestion and loss models the review identified as
blockers.

**One-line idea:** Agent Diary records what an agent *said* it did. This adds an
independent record of what the machine *observed happening*, and compares them —
with explicit uncertainty and explicit gaps.

---

## 1. The claim, stated honestly

### 1.1 What the strong version of this is

Not this:

> "The agent says X; the machine proves whether X happened."

This:

> "A machine-observed evidence stream lets us compare the agent's declared
> actions against independently observed effects, with explicit uncertainty and
> loss states."

The headline — *the mismatch is the product* — survives only in that weaker form.
A mismatch is a **signal for investigation**, not a finding of fact. It cannot be
a finding of fact while two other explanations remain open: the correlation
failed, or the sensor did not see the event.

### 1.2 The trust boundary, stated plainly

**Guaranteed:** an unprivileged agent cannot stop, reconfigure, or write to the
recorder, and cannot author or alter a machine-plane event through normal
user-space permissions.

**Not guaranteed:** a compromised host; a root or sudo-capable agent; a kernel
exploit against the eBPF path; rollback of the disk to an earlier state; event
loss under buffer exhaustion; or availability of the recorder at all.

The consequence is a design rule, not a caveat: **if this document uses the words
*evidence*, *integrity*, or *flight recorder*, then off-box checkpoints are
required, not optional.** A local hash chain detects modification of the
surviving chain; it cannot prove that an earlier segment was not deleted, nor
that the machine was not rolled back. Where checkpoints are absent, the honest
name for this system is *observability with loss reporting* — not audit.

### 1.3 Threat model

| Question | Answer |
| --- | --- |
| Protects against an unprivileged, prompt-injected agent? | **Yes** — the primary threat. |
| Protects against an agent with sudo or root? | **No.** Out of scope; see §11. |
| Protects against a compromised kernel? | **No.** |
| Protects against physical disk access? | **No**, absent off-box checkpoints. |
| Is a full disk an availability or integrity failure? | **Availability** — but reported as a gap event, never as silence. |

The scope of the guarantee is deliberately narrow. An honest narrow guarantee is
worth more than a broad one that cannot be tested.

## 2. Privacy: two modes, and they are different products

Revision 1 claimed metadata-only while naming AgentSight as its model sensor.
AgentSight's intent stream captures plaintext at the TLS boundary — prompts,
responses, headers, network targets. That is content. The contradiction was real;
it is resolved by separating the modes rather than by softening the claim.

**Mode A — metadata-only. The default. Always on unless explicitly changed.**

- syscall type, process name, file path (subject to §9), network destination
  (coarse), timestamps, result
- never file contents, never command arguments, never payloads, never prompts
- never credential, key, browser-data or cache paths

**Mode B — content-bearing intent capture. Off by default.**

- prompt and model-traffic capture, of the kind AgentSight performs at the TLS
  boundary
- requires explicit, recorded consent for that source
- labelled as content-bearing everywhere it appears in a view
- revocation is terminal, as elsewhere in Agent Diary

**The consequence the review drew, and it is the important one:** with Mode B
off, the intent stream disappears. The machine plane then holds only the
system-effect side, compared against Agent Diary's *existing* work traces. That
is still useful — but it is a different product from the one revision 1
described, and it is the product this document is actually about. Mode B is an
extension, not the premise.

## 3. What already exists — do not rebuild these

**Agent plane — already ours.** Agent Diary work traces: tool calls, terminal
commands, file edits. This is the agent's own account.

**Machine-plane sensors:**
- `eunomia-bpf/agentsight` — the closest existing work (arXiv:2508.02736). eBPF
  at two boundaries: intent (TLS) and action (tracepoints). Per §2, only its
  action side is usable in Mode A.
- Tetragon (Cilium), Falco (CNCF), Tracee (Aqua), auditd, Sysdig.

**Integrity:** immudb, Trillian, hash chaining, signed checkpoints.

**Kill-switch prior art:** OWASP AISVS C09-06; CrowdStrike Falcon Guardian
(November 2026); Stanford Law, March 2026 — *kill switches don't work if the
agent writes the policy*.

**Therefore the deliverable is not a sensor.** It is a record format, an
identity model, an ingestion boundary, a loss model, and a view that ranks
exceptions. The sensor is a dependency, and Stage 0 decides which one.

## 4. Architecture

```
  agent plane        ─┐
  (work traces)       │
                      ├──►  correlated record  ──►  append-only log  ──►  ranked view
  machine plane     ──┤        (per run)            (+ off-box          (exceptions,
  (eBPF, unpriv.)     │                              checkpoints)        not a log)
                      │
  approval plane    ──┘
  (consent decisions)
```

**The machine plane is a separate companion package.** The reason is not merely
that Agent Diary's `pyproject.toml` declares no dependencies. The stronger
reasons are: root and capability requirements; Linux/eBPF-specific dependencies;
a separate security review; an independent release cadence; optional
installation; keeping privileged code out of the Diary server; different failure
and upgrade semantics; and keeping core Agent Diary usable on macOS, Windows and
unprivileged installs.

Interfaces between the two:

- a **versioned machine-event ingestion protocol** that the companion targets
- **no importing of Agent Diary internals** by the companion
- **no direct writes to the Diary SQLite database** by the companion

## 5. Correlation identity — supervisor-issued, not agent-declared

Revision 1 proposed `run_id` propagated through the environment, plus PID
ancestry, with time as a tiebreaker. That model fails silently in at least twelve
ways: background processes outliving the run; sudo sanitising the environment;
systemd-run, cron, containers and SSH detaching; worker pools reused across runs;
shared helpers; PID reuse; delayed daemon work; concurrent sessions sharing a
helper tree; namespace crossings; a tool overwriting `run_id`; pooled network
connections; and filesystem activity outside the original time window.

**The worst outcome is not an unmatched event. It is a plausible but wrong
match.** So identity moves outside the agent:

- a capture ID **assigned by the supervisor**, outside the agent
- each run launched inside a **dedicated cgroup / systemd scope**
- the recorded identity tuple: **capture ID, cgroup ID, boot ID, PID, PID start
  time, namespace IDs, ancestry**
- environment `run_id` is a **hint, never authority**
- process identity is `boot_id + pid + start_time`, never a bare PID

Every machine event carries a **reason code and a confidence**, not a boolean:

```
confirmed_match          machine effect and agent claim agree, identity certain
ambiguous_match          identity plausible but not unique
delayed_match            effect arrived after the run's window closed
machine_only             observed effect with no agent claim
agent_only               agent claim with no observed effect
unmatched_due_to_scope   outside the recorder's configured scope
unmatched_due_to_gap     the recorder could not have seen it (§7)
recorder_unavailable     the recorder was not running
```

Time proximity alone may never promote an event to a confident match.

## 6. Mismatch taxonomy

A missing agent claim may mean: a hidden action; an untraced tool wrapper; a
dropped event; delayed asynchronous work; a daemon acting on the agent's behalf;
correlation failure; the machine plane not running; a trace written after the
effect; or the human doing it themselves. These must not all present as
"divergence".

The categories in §5 are the taxonomy. The view ranks *investigable exceptions*
and names the category each belongs to. A headline count of "3 divergences"
without categories is not an honest artefact.

## 7. Coverage: gaps are first-class

> A missing event must be distinguishable from "nothing happened."

This is the finding that reshaped the document. Without it the central claim
collapses: a mismatch could simply mean the recorder blinked.

The recorder therefore emits explicit **gap events**, as first-class records:

```
recorder_started
recorder_stopped
buffer_overflow
permission_gap
unsupported_kernel_feature
sink_unavailable
clock_jump
```

Plus dropped-event counters, ring-buffer overflow counts, and restart markers.
Any interval not covered by the recorder is a reported gap, never an implicit
"nothing happened". Coverage appears in every view.

## 8. Protected ingestion boundary

Revision 1 said the machine plane "writes into Agent Diary" without saying how.
That matters: if the agent can call the same endpoint as the recorder, it can
submit fake machine events; if the recorder writes straight into the Diary
database, a privileged process is coupled to an application schema.

```
  privileged recorder
    -> protected local Unix socket / root-owned spool
      -> unprivileged importer
        -> Agent Diary machine-event records
```

Acceptance conditions:

- the agent cannot submit a machine-plane event
- the agent cannot alter an existing machine event
- the importer can distinguish recorder-authored events from agent-authored work
  traces
- schema or version mismatch **fails closed**
- partial ingestion is **detectable and resumable**

This boundary is defined before the Agent Diary schema, not after it.

## 9. Path privacy

Paths are not automatically private. `/home/x/Documents/medical/diagnosis.pdf`
and `/home/x/.ssh/id_ed25519` are as sensitive as contents. Required:

- configurable path roots, with redaction by default outside them
- an explicit decision on **basename versus full path**
- credential, key and browser-data paths excluded by default
- a stated hashing rule, salted, resistant to dictionary attack
- retention and deletion rules for machine events
- **explicit user consent for the machine plane itself**, separate from the
  ordinary diary consent
- a coarse default for network destinations
- command lines **never captured** — and, because some sensors expose them
  naturally, an explicit stripping step rather than an assumption

## 10. Loss, retention, clock, crash, disk-full

Specified before the hash chain, not after:

- **Retention and rotation.** Machine events accumulate far faster than entries.
  A maximum allocation and a rotation policy are required.
- **Sink unavailable.** Write to the protected spool; if the spool also fills,
  emit `sink_unavailable` and keep counting the loss.
- **Disk full.** An availability failure, reported as a gap — never silent
  degradation.
- **Clock.** Monotonic clock for ordering; wall clock as evidence only. Boot ID
  recorded. NTP steps and suspend/resume detected and emitted as `clock_jump`.
- **Ordering.** Per-CPU sequences give only a partial order, so the authoritative
  order is a **logical sequence number assigned at ingestion**. Timestamps are
  evidence, not identity.
- **Crash recovery.** A chain tail may be partial. On restart the tail is sealed
  with a terminal marker, recovery truncates to the last verified link, and the
  truncation is recorded as a gap.
- **Backup and restore.** Restoring an old segment must not resurrect a chain
  that conflicts with a checkpoint.
- **Schema migration.** Every record carries its schema version; the importer
  fails closed on mismatch.

## 11. Kill switch

The requirements are easy; the review is right that pause/freeze/isolate/stop is
not yet an architecture. Contracts to settle before building:

- **Owner.** A separate system service, out-of-band — not the agent, and not a
  process the agent can signal.
- **Freeze.** The cgroup freezer, not `SIGSTOP` against a guessed PID.
- **Isolate.** Network namespace or firewall rule, with a stated answer on
  whether it is reversible.
- **Agent with sudo.** Explicitly outside the guarantee (§1.3).
- **Controller unavailable.** State the fail direction deliberately, and whether
  the agent stops or continues.
- **Testing.** Verified from outside the agent; never trust an agent-reported
  "stopped".
- **First implementation.** One manual out-of-band path, no UI, proven once.

## 12. The view

Not a log viewer. Ranked investigable exceptions, showing:

- a timeline in plain sentences, with coverage gaps drawn as gaps
- exceptions grouped by the §6 categories, not a single "divergence" count
- authority: which actions had human approval and which did not
- switch state, and the time of its last verification

Nobody reads logs. The value is in what gets ranked to the top.

## 13. Stage 0 — evaluate, do not build

Run against real activity on Art (the agent's own process tree). Ten questions:

1. Can it trace the agent's actual process tree?
2. Can it see exec, file writes, unlink, chmod and network effects?
3. Can it trace processes launched by tool calls?
4. Does it capture prompts or payloads when Mode A is intended? (§2)
5. What happens with Python, Node, statically linked binaries, sudo, background
   jobs?
6. What percentage of events are dropped?
7. What privilege does it require on the current kernel?
8. Can output be exported without adopting its content-bearing schema?
9. How often does its correlation produce ambiguous or wrong-looking joins?
10. What are the storage and CPU costs over a normal workday?

Baseline scenarios, each run deliberately and observed: a harmless shell command;
file creation and modification; a subprocess; a background process; a network
request; a sudo operation; a command that intentionally fails; two concurrent
runs.

**Nothing is built until those results exist.**

## 14. Non-goals

- Not a general EDR or intrusion-detection system.
- Not cross-platform in v0: Linux only. macOS (Endpoint Security) and Windows
  (ETW) are separate efforts with different mechanisms.
- Not a content archive. Mode B exists but is not an archive.
- Not a compliance product.
- Not a replacement for AgentSight, Tetragon or Falco — a consumer of one.

## 15. Staged plan

- **Stage 0 — evaluate** (§13). No code.
- **Stage 1 — ingestion boundary and identity** (§5, §8). No UI, no chain.
- **Stage 2 — privilege separation, proven.** The agent cannot reach the
  recorder, demonstrated rather than asserted.
- **Stage 3 — chain and checkpoints** (§1.2, §10).
- **Stage 4 — view and switch** (§11, §12).

Approval for Stage 0 does not imply approval for Stage 1.

## 16. Open questions

- Retention horizon for machine events, which accumulate far faster than entries.
- Does a confirmed mismatch ever raise an alarm, or only rank higher in the view?
- Can privilege separation be made comprehensible to a non-technical operator, or
  does it need a one-command setup that hides the Unix reasoning?
- When the agent runs inside a container or VM, does the machine plane belong
  inside it or outside it?
- Is Mode B (§2) a road worth travelling at all, given it makes the recorder
  content-bearing and therefore subject to the strongest consent rules?
- Off-box checkpoints imply a second machine. Is that acceptable for a
  single-machine personal install, or does the guarantee have to shrink further?
