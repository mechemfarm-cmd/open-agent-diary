# Machine plane — Stage 3 proposal: the privileged recorder, deployed

**Status:** proposal for review. Nothing here has been built or run.
**Predecessors:** Stage 1 closed (final result approved); Stage 2 approved at
`495e48d` / `b64fd7e` for the producer and the demonstrated ingestion boundary.
**Target host:** Emily.
**Author:** Tom. Written for Alice's review, then Bill's authorization.

---

## 0. Plain language first

Right now the machine plane is a library that can read events if somebody hands it
a file of them. Nothing on any machine is watching. Every test passes, every
boundary check passes, and no probe has ever been attached outside a Stage 1
window.

Stage 3 is the step that makes it real: **one small privileged program that
watches, and one unprivileged program that reads what it wrote.**

Nothing in between. The watcher cannot be reached by the agent; the reader cannot
write. That is the whole shape, and everything below is the detail of making it
true rather than approximately true.

## 1. Scope, and the numbering question (please check this first)

The design's §16 numbers the stages:

| §16 | What it says |
|---|---|
| Stage 0 | evaluate (§14). No code |
| Stage 1 | ingestion boundary and identity (§5, §8). No UI, no chain |
| Stage 2 | privilege separation, proven |
| Stage 3 | chain and checkpoints (§1.2, §10) |
| Stage 4 | view and switch (§11, §12) |

The executed sequence has not matched that numbering:

- Stage 0 was run as designed.
- **Stage 1 was inserted** — a producer evaluation (probes, coverage, loss) that
  §16 does not list.
- **Stage 2 merged §16's Stages 1 and 2** — the producer plus the ingestion
  boundary plus the separation proof.

So §16's Stage 3 — chain and checkpoints — is the next item *in §16's numbering*,
but it is not the next thing that needs to exist. Your Stage 2 approval was
explicit that it covered "the producer and demonstrated ingestion boundary" and
that "the privileged live deployment/service still needs separate implementation
and acceptance checks."

**This proposal is that deployment, and calls it Stage 3.** Consequences, stated
rather than implied:

- chain and checkpoints move to Stage 4;
- view and switch move to Stage 5;
- §16 should be amended when this is approved, because a plan whose numbering no
  longer matches its execution is how the last two rounds of confusion started.

The justification is that §16 assumed the deployed recorder was part of "privilege
separation, proven". Your review correctly separated the *property* (proven, on
the spool boundary) from the *deployment* (not built). A property that no running
process provides is not yet a system, and a chain over evidence nobody produces
would be a chain over nothing.

If you would rather keep §16's numbering and fold the deployment into Stage 4 as
its first half, say so and I will restructure — the content does not depend on
the label.

## 2. Containment: what this protects, whose decision it is

§1.3 of the design says it plainly: *"Agent with sudo. Explicitly outside the
guarantee."*

**On Emily today, the agent has passwordless sudo.** It is the same account
(`willard`, uid 1000, home `/home/willard`) that owns the Hermes gateway, and it
can become root in one command without a password.

Verified on Emily while writing this, not assumed:

```
$ sudo -n true          -> succeeds, no password
$ grep -rl willard /etc/sudoers.d/
  /etc/sudoers.d/tom
  /etc/sudoers.d/netmap-wifi
```

That has a specific consequence:

> A root-owned recorder on this account layout produces an **advisory** property,
> not an **enforced** one. It stops an agent that has been prompt-injected into
> doing something *careless*. It does not stop an agent that decides to become
> root, because nothing stops that.

Recording still works. What is not guaranteed is that the record is untampered.

### 2.1 The product states its own protection level

This must not live in a caveat in a design document that the user never reads. It
is a property of the running system, and the system reports it.

**Two levels, and only two:**

| Level | Condition | What it means |
|---|---|---|
| **enforced** | the agent's account cannot escalate to root | the recorder and spool are a real boundary; an agent that wanted to tamper cannot |
| **advisory** | the agent's account can escalate to root | recording works, but the protection is a convention; an agent that chose to could alter or delete records |

**Detection**, run at install and at every recorder start. Any one match means
`advisory`:

- the agent's account has a `NOPASSWD` sudo grant (checked with `sudo -n -l -U`);
- the agent's account is in `sudo`, `wheel`, `docker`, `disk`, or `lxd`;
- the agent's account can start or stop system units via polkit;
- the agent runs as root, or the recorder cannot determine which account owns the
  agent process (unknown is `advisory`, never `enforced`).

The reasons are reported individually — *which* grant matched — because "your
agent can become root" without naming the grant is not actionable.

### 2.2 What the user is actually shown

At install, in plain language, before anything is written:

```
MACHINE PLANE — PROTECTION LEVEL: ADVISORY

The agent runs as account "willard", which can become root without a
password.

Matching grants:
  /etc/sudoers.d/tom
  /etc/sudoers.d/netmap-wifi

The recorder will still record what the agent does. What it cannot
guarantee is that the record is untampered — an agent that decided to
become root could alter or delete it.

Recording works. The protection is a convention, not a boundary.

To get the enforced level, run the agent as an account that cannot become
root. That is a separate change; this installer will not make it for you.

Continue in advisory mode? [y/N]
```

Three rules about that prompt:

- **It defaults to no.** A silent `y` on a protection question is how people end
  up believing they have a guarantee they do not have.
- **Non-interactive installs must pass an explicit flag** (`--accept-advisory`)
  or they abort. No default-accept path exists, including for automation.
- **No `--quiet` suppresses it.** The whole point is that the user sees it.

### 2.3 The decision is recorded, and stays visible

Accepting advisory mode writes a record — `/etc/machine-plane/containment.json` —
carrying the level, the account, the matching grants, the time, and how consent
was given (interactive or flag). That makes "the user decided this" a fact about
the installation rather than something we remember saying.

The level is then shown permanently, not once:

- `machine-plane-recorder status` prints it first, before anything else;
- the view carries a standing line: `Protection: ADVISORY (agent can become root)`;
- every segment's terminal record carries the level in force for that run, so a
  record can never be read later as more authoritative than it was when written.

If the account situation changes — the grant is removed, or added — the level is
re-evaluated on the next start and a change is written to the same file. **A
downgrade is surfaced, never silent**, for the same reason the loss runner voids
rather than continues.

### 2.4 Decision

Bill has chosen **(a): deploy now, advisory, with the trade-off stated to the
user and the decision left to them.** That is what this section specifies, and it
is why the detection, the prompt, and the standing display are in Stage 3 rather
than deferred. A user who is told plainly and chooses anyway has made an informed
decision; a user who is told nothing has been misled, whether or not the code is
correct.

Option (b) — moving Hermes to a dedicated unprivileged account — remains the way
to reach `enforced`, and remains a separate piece of work. This stage does not
perform it and does not pretend to.

## 3. What the recorder is

A systemd system service, running as root, with one job:

0. determine the protection level (§2.1) and **refuse to start silently in
   `advisory`** — if the user has not accepted it, the run does not happen;
1. create a cgroup scope for a named workload;
2. launch that workload inside the scope;
3. measure the scope's cgroup inode;
4. attach the probes (one `bpftrace` process per probe script);
5. read `EV`/`MARK` lines, normalise them, write records to the spool;
6. detach, seal the segment, exit.

It never reads the agent's files, never talks to the Diary, never holds a database
handle, and never executes anything the agent asks for.

```
  systemd (pid 1)
      |
      +-- machine-plane-recorder.service   root, hardened, no network
              |
              +-- systemd-run --scope  -->  the workload
              |        (join key: cgroup inode)
              +-- bpftrace exec.bt
              +-- bpftrace file.bt
              +-- bpftrace net.bt
              |
              v
        /var/lib/machine-plane/spool/    root:mp-read 0750
              |   segment-<boot>-<seq>.ndjson   (sealed, append-only in practice)
              v
        machine-plane-import.timer       unprivileged
              |
              v
        machine events store  -->  view
```

## 4. Supervisor mode, and why not the gateway yet

The recorder needs to know *which* cgroup belongs to *which* run. There are two
ways to get that:

- **Supervisor mode** — the recorder creates the scope and launches the workload,
  so the join is structural: it knows the scope because it made it.
- **Observer mode** — something else (the Hermes gateway) launches the scope, and
  the recorder discovers and attaches to it.

**Stage 3 builds supervisor mode only.** It is proven, it gives the cgroup join
for free, and it does not touch how Hermes runs.

Observer mode is the thing that eventually records real agent sessions, and it is
deliberately not in this stage: it needs to answer what happens when a scope
appears and disappears between two polls, what happens when several runs overlap,
and how attachment latency is bounded. Those are real questions with wrong answers
available, and answering them while also deploying a service for the first time
would mean debugging two unknowns at once.

So Stage 3's live proof is **a named workload in a named window** — the same
discipline Stage 1 used, for the same reason.

## 5. The launch contract

Everything the producer needs, spelled out, because Stage 2's blocker was exactly
this seam being unspecified:

| Input | Source | Why |
|---|---|---|
| `host_id` | supervisor | stable per machine |
| `boot_id` | `/proc/sys/kernel/random/boot_id` | identity changes across reboot |
| `scope` | the scope the recorder created | correlation matches on this, not on the inode |
| `cgroup` inode | measured from the scope before attach | joins probe events to the run |
| probe paths | installed read-only at `/usr/lib/machine-plane/probes/` | not writable by the agent |
| segment path | derived from boot id + monotonic sequence | restart produces a new segment |

The `scope_by_cgroup` map from Stage 2 is populated here, from measurement, not
from a guess. If the measured inode differs from the one the scope reports, the
run fails closed and says which two values disagreed — the same check that caught
the `--expand-environment` bug in Stage 1's preflight.

## 6. Privilege model

- **Recorder:** root, started by systemd, `User=root`.
  `CapabilityBoundingSet` limited to what attaching actually needs
  (`CAP_BPF`, `CAP_PERFMON`, `CAP_SYS_ADMIN`, `CAP_SYS_RESOURCE`), everything else
  dropped. `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=yes`,
  `PrivateTmp=yes`, `RestrictAddressFamilies=AF_UNIX`, no network.
  `ReadWritePaths=` exactly the spool directory.
- **Spool:** `root:mp-read 0750`, files `0640`. The agent's account is not in
  `mp-read`.
- **Importer:** unprivileged, reads the spool, writes the store. Cannot alter a
  record, cannot create one.
- **Probes:** read-only, root-owned, installed by the package. The recorder
  executes them; nothing else may modify them.

The guarantee this buys, stated exactly: **an unprivileged process can read the
spool and cannot write, truncate, rename, or delete anything in it.** Not "the
agent cannot see it" — the view has to read it (§12 of the design), so read access
is required and claiming otherwise would be the same overclaim the Stage 2
separation proof corrected.

## 7. Lifecycle

```
start  -> measure -> attach -> run -> detach -> seal -> exit 0
```

- **Seal** means the segment ends with a terminal record carrying: end reason
  (`workload_exit`, `duration_elapsed`, `probe_error`, `recorder_signal`), the
  final loss counter, and the count of records written. A segment without a
  terminal record is a torn tail and the importer treats it as a gap — it is
  never silently accepted.
- **One run, one segment.** No appending across runs, so a crash cannot corrupt a
  segment that was already sealed.
- **Restart** seals any unsealed segment as `torn_tail` before starting a new one.

## 8. Loss, rotation, clock, crash

§10 requires these before the chain, and the recorder is where they live:

- **Rotation.** A hard allocation (`machine-plane-max-bytes`), oldest sealed
  segment deleted first. Deletion is never silent: a `retention_dropped` record is
  written to the newest segment carrying the count and the oldest timestamp
  dropped, so the view can draw the hole.
- **Disk full.** `sink_unavailable`-style gap, run continues, loss counted. Never
  silent degradation.
- **Clock.** Monotonic for ordering, wall clock as evidence only. Boot id
  recorded. `clock_jump` emitted on an NTP step or suspend/resume, detected by
  comparing monotonic deltas against wall deltas across a sampling interval.
- **Crash.** Torn tail sealed on restart, truncation recorded as a gap.
- **Schema version** on every record; the importer fails closed on mismatch
  (already implemented and proven in Stage 2).

## 9. Failure modes, and what each does

| Failure | Behaviour |
|---|---|
| Probe fails to attach | run fails closed, named error, no records claimed complete |
| Workload exits early | clean seal with `workload_exit`; not an error |
| Workload stalls but stays alive | stall detected by the run sheet's progress rule; voided |
| Recorder killed | torn tail on restart, sealed as a gap |
| Spool full | gap + loss count, run continues |
| Disk full | gap, run continues until the run ends |
| Clock step | `clock_jump` record |
| Agent tries to write the spool | permission denied; nothing else needed |
| Scope inode mismatch | fail closed, both values named |

## 10. What an operator can see

Without reading internals, and without root:

- **the protection level first, with the grants that produced it** — `enforced` or
  `advisory`, stated every time, never implied;
- `machine-plane-recorder status` — is the service running, when did the last run
  end, and why;
- the newest segment's terminal record — records written, loss count, end reason;
- whether any segment is unsealed (a torn tail);
- how much of the allocation is in use, and whether rotation has dropped anything.

This matters because §17 asks whether privilege separation can be made
comprehensible to a non-technical operator. Stage 3's answer is a small, honest
status surface, not a dashboard.

## 11. Acceptance checks

Each is runnable and falsifiable. A failure voids the run rather than being
written around.

1. Service installs, starts as root, survives a restart, is `enabled`.
2. Recorder launches the named workload in a scope; measured cgroup inode equals
   the value the scope reports.
3. Attaching succeeds with exactly three probes; the process count is exact, not
   inferred from a substring match.
4. Events are produced, ingested by the unprivileged importer, and the ingested
   count equals emitted minus declared loss.
5. A workload signalled mid-run produces a sealed segment with an explicit end
   reason — not a silent stop.
6. Rotation: force the allocation below segment size; the oldest segment is
   dropped and a `retention_dropped` record appears with the right count.
7. Permission: an unprivileged account that lacks sudo cannot write, truncate,
   rename, or delete a spool file. Attempted and denied, from outside the
   recorder.
8. A deliberately broken probe fails the run closed with a named error and
   produces no segment claiming completeness.
9. `clock_jump` is produced by a simulated step.
10. No content beyond the declared metadata fields appears in any record —
    checked by scanning a real segment for fields the schema does not define.

11. Containment detection reports `advisory` for the agent's account on Emily and
    names every matching grant — not a generic "your agent may escalate".
12. The install prompt defaults to no: an empty answer aborts, and nothing is
    written under `/etc/machine-plane/`.
13. A non-interactive install without `--accept-advisory` aborts. No path,
    including automation, reaches advisory mode without an explicit yes.
14. Accepting writes `containment.json` carrying the level, the account, the
    matching grants, the time, and how consent was given.
15. `status` prints the level before anything else, and every terminal record
    carries the level in force for its run.
16. Detection on the purpose-made unprivileged test account reports `enforced` —
    proving the detector discriminates, rather than always answering `advisory`.

Check 7 must be run with an account that does not have sudo. On Emily, that means
a purpose-made account for the test, because `willard` has sudo and would pass the
check for the wrong reason. This is the concrete consequence of section 2 — and
checks 11 and 16 together are the proof that the system knows which case it is in,
rather than reciting a fixed warning.

## 12. Non-goals

- **Not** chain or checkpoints. Those are Stage 4.
- **Not** the kill switch, not the view. Stage 5.
- **Not** observer mode, not gateway integration.
- **Not** content capture. Metadata only, unchanged.
- **Not** a change to how Hermes runs, its account, or its configuration.
- **Not** any second host. Emily only.

## 13. Risk and rollback

The recorder is an addition, not a modification: no existing service changes. It
touches one new path (`/var/lib/machine-plane`), one unit, and one importer timer.

Rollback is: `systemctl disable --now machine-plane-recorder.service`, remove the
unit and the spool, and the fleet is exactly as it is today. No agent behaviour
changes at any point, because nothing in the agent path is modified.

The real risk is not the recorder; it is **the window**. Attaching probes costs
CPU and generates real output volume — Stage 1 measured approximately 400 KB/s
for a busy workload — and an always-on recorder on Emily's SD card would be a
problem for a host that already shows SATA timeouts. **Stage 3 runs bounded: one
named workload, one measured window, then detach.** Always-on is a separate
decision that needs its own numbers, and should not be assumed to follow from a
successful deployment.

## 14. Questions for Alice

1. Is the renumbering in §1 acceptable, or should the deployment fold into
   Stage 4 as its first half?
2. Is the advisory/enforced gate the right shape — detection at install *and*
   every start, a default-no prompt, a recorded consent file, and the level
   stamped into every terminal record — or is the record more ceremony than the
   protection warrants? Bill's decision is to deploy in advisory mode and let the
   user decide; the question is whether this is the honest way to present it.
3. Supervisor mode only, or is observer mode required before this counts as
   deployed?
4. Should retention/rotation be in this stage, or is it Stage 4's first piece?
   My view: it belongs here, because an unbounded recorder is a disk-full
   incident, and §10 puts it before the chain for that reason.
5. Is check 10 (no content beyond the schema) required before deployment, or
   acceptable as a check run during the first window?

## 15. What this does not claim

- It does not claim the agent is contained. On today's account layout it is not,
  and the system says so to the user's face — at install, at every start, in
  `status`, and in every run's terminal record (section 2). A user who accepts
  advisory mode has made an informed choice; nothing here hides it from them.
- It does not claim the probes are complete. The seven declared gaps from
  Stage 1 and Stage 2 are unchanged.
- It does not claim always-on is affordable. Only that one bounded run works.
- It does not claim the view exists. It does not.
