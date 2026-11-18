# Machine plane — Stage 3 proposal: the privileged recorder, deployed

**Status:** proposal for review. Nothing here has been built or run.
**Revision:** 3 — revised after review of `3d2e1f7`. Retention is recorded in a
single authoritative journal rather than in a segment marker, which also fixes the
one-sealed-segment case (§8.1), and that journal is now bounded and compacted
(§8.2). Revision 2's changes, from the review of `77892f6`: bounded/manual service
(§3), consent bound to a privilege fingerprint (§2.5), ownership matrix (§6),
startup consent matrix (§7.1), rotation failure semantics (§8), pre-attach privacy
gate (§11.0).
**Predecessors:** Stage 1 closed (final result approved); Stage 2 approved at
`495e48d` / `b64fd7e` for the producer and the demonstrated ingestion boundary.
**Target host:** the reference host.
**Author:** the implementer. Written for review, then the operator's
authorization.

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

Review of `77892f6` accepted this numbering. On approval, design §16 is amended so
the plan follows the sequence that actually happened: Stage 3 deployment, Stage 4
chain and checkpoints, Stage 5 view and switch.

## 2. Containment: what this protects, whose decision it is

§1.3 of the design says it plainly: *"Agent with sudo. Explicitly outside the
guarantee."*

**On the reference host, the agent has passwordless sudo.** It is the same
account that owns the agent's gateway process, and it can become root in one
command without a password.

Verified at proposal time, not assumed: `sudo -n true` from that account succeeds
with no password, and more than one drop-in under `/etc/sudoers.d/` names it. The
account, the host and the grant filenames are deliberately **not** recorded here —
this document is published, and the installer reports them at runtime to the
person who owns the machine.

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

The agent runs as account "agent-account", which can become root without
a password.

Matching grants:
  /etc/sudoers.d/<grant>
  /etc/sudoers.d/<grant>

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

The operator has chosen **(a): deploy now, advisory, with the trade-off stated to
the user and the decision left to them.** That is what this section specifies, and it
is why the detection, the prompt, and the standing display are in Stage 3 rather
than deferred. A user who is told plainly and chooses anyway has made an informed
decision; a user who is told nothing has been misled, whether or not the code is
correct.

Option (b) — moving Hermes to a dedicated unprivileged account — remains the way
to reach `enforced`, and remains a separate piece of work. This stage does not
perform it and does not pretend to.

### 2.5 Consent is bound to the privilege fingerprint that was detected

Revision 1 recorded that the user accepted advisory mode and re-evaluated the
level at every start. That is not enough: re-evaluating what the situation *is*
says nothing about whether the user agreed to *that* situation. A consent obtained
when the agent had one grant remains on disk and would silently cover a different
set of grants after the next change.

**The fingerprint.** Detection produces a canonical record, hashed with SHA-256
over JSON with sorted keys, sorted lists, UTF-8, no insignificant whitespace:

| Field | Source |
|---|---|
| `account`, `uid` | the agent's account and numeric id |
| `host_id` | stable machine identifier |
| `grants` | matching `NOPASSWD`/`ALL` grant names, sorted |
| `groups` | privilege-relevant memberships (`sudo`, `wheel`, `docker`, `disk`, `lxd`, `mp-read`), sorted |
| `is_root` | whether the agent's account is root |
| `can_control_units` | polkit result for starting/stopping system units |
| `level` | `enforced` or `advisory` |
| `reasons` | the individual matches that produced the level, sorted |
| `detector` | detector version, so a detection change cannot silently reuse an old consent |

`/etc/machine-plane/containment.json` stores the fingerprint, the level, the
reasons, when detection ran, and the consent record (how it was given, when).
It also stores a fingerprint of the **recorder configuration** — unit, probe set,
spool path, allocation — because consent to a different recording configuration is
also not consent to this one.

**Validity rule.** Consent is honoured only when *both* fingerprints match what is
stored. Anything else requires fresh explicit consent. Concretely:

| Situation | Behaviour |
|---|---|
| no consent file, detection `advisory` | refuse, print the block from §2.2 |
| consent valid, fingerprint unchanged | start |
| consent valid, fingerprint changed | **refuse**, name which field changed, require fresh consent |
| detection `enforced`, no advisory consent | start; record the new state; mark any old advisory consent superseded |
| `enforced` → `advisory` | **refuse**; fresh explicit acceptance required before any attach |
| detection error or unknown | refuse — never claim `enforced` from an unknown |
| non-interactive and consent invalid | exit non-zero; no `--force`, no default-accept |

The `enforced → advisory` case is the one that matters most: it is the transition
where a machine quietly becomes less protected than it was, and the previous
consent was given under a materially better guarantee. It is never automatic.

**Stated plainly for the user**, when consent is refused:

```
MACHINE PLANE — CONSENT EXPIRED

The protection situation changed since advisory mode was accepted.

  Level:    enforced -> advisory
  Changed:  grants
            + /etc/sudoers.d/<grant>

The previous acceptance does not cover this. Re-run the installer to review
and accept the new state, or restore the previous account configuration.
```

Nothing is recorded in this state; the recorder does not start.

Revision 1's failure was that it treated consent as a fact about the past rather
than a claim about a specific present. The fingerprint is what makes it a claim.

## 3. What the recorder is

A systemd **template** service, invoked by name for one run, running as root:

0. determine the protection level (§2.1) and verify that consent matches the
   current privilege fingerprint (§2.5) — if either check fails, the run does not
   happen;
1. create a cgroup scope for a named workload;
2. launch that workload inside the scope;
3. measure the scope's cgroup inode;
4. attach the probes (one `bpftrace` process per probe script);
5. read `EV`/`MARK` lines, normalise them, write records to the spool;
6. detach, seal the segment, exit.

It never reads the agent's files, never talks to the Diary, never holds a database
handle, and never executes anything the agent asks for.

**It does not run at boot, and it cannot be enabled.** The unit is
`machine-plane-recorder@.service` — a template with `Type=oneshot` and **no
`[Install]` section**, so `systemctl enable` has nothing to install and a reboot
starts nothing. A run is invoked explicitly, once, by name:

```
systemctl start machine-plane-recorder@mp-stage3-<capture-id>.service
```

The instance name is the capture id: two runs cannot collide, and the unit that
produced a segment is recorded in the segment's own path. "Bounded" is therefore a
property of the unit design rather than of anyone's intentions — which is the
correction to revision 1, where "one bounded window" and "the service is
`enabled`" could not both be true.

The importer is a oneshot service too, started by hand after the run, not a timer.
Nothing in Stage 3 fires on a schedule.

```
  started by hand — no [Install], no boot activation
      |
  machine-plane-recorder@<capture>.service    root, oneshot, hardened, no network
      |
      +-- systemd-run --scope  -->  the workload
      |        (join key: cgroup inode)
      +-- bpftrace exec.bt
      +-- bpftrace file.bt
      +-- bpftrace net.bt
      |
      v
/var/lib/machine-plane/spool/    root:mp-read 0750
      |   segment-<boot>-<seq>.ndjson   (sealed; one segment per run)
      v
machine-plane-import.service     mp-import:mp-read, oneshot, started after the run
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

## 6. Privilege model and the complete ownership matrix

Revision 1 said "importer: unprivileged" without naming it, which leaves the
deployment with no concrete identity to implement. Named in full:

**Identities**

- **Recorder** — Unix user `root`. `CapabilityBoundingSet` exactly
  `CAP_BPF CAP_PERFMON CAP_SYS_ADMIN CAP_SYS_RESOURCE`, everything else dropped.
  `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=yes`,
  `PrivateTmp=yes`, `RestrictAddressFamilies=AF_UNIX`, no network.
  `ReadWritePaths=` exactly the spool directory.
- **Importer** — Unix user `mp-import`, primary group `mp-import`, supplementary
  group `mp-read`. Not in `sudo`, `wheel`, `docker`, `disk` or `lxd`. Writes only
  its own store.
- **Agent** — Unix user `agent-account`. **Not** in `mp-read`, not in
  `mp-import`. No access to the spool at all.
- **Probes** — root-owned, `0644`, under `/usr/lib/machine-plane/probes/`,
  installed by the package. Executed by the recorder; nothing else may modify them.

**Objects**

| Object | Owner:Group | Mode | Create | Read | Write / delete |
|---|---|---|---|---|---|
| `/var/lib/machine-plane` | root:root | 0755 | installer | all | root |
| `spool/` | root:mp-read | 0750 | recorder | root, `mp-read` | **root only** |
| `spool/segment-*.ndjson` (sealed) | root:mp-read | 0640 | recorder | root, `mp-read` | **root only** |
| `spool/segment-*.partial` (live) | root:mp-read | 0640 | recorder | root only | root only |
| `spool/retention.journal` | root:mp-read | 0640 | recorder | root, `mp-read` | root only |
| `/etc/machine-plane/containment.json` | root:root | 0600 | installer | root | root |
| probes | root:root | 0644 | package | all | root |
| importer store | mp-import:mp-import | 0700 | importer | `mp-import` | `mp-import` |

**Reading while the recorder appends — avoided by construction, not by locking.**

A segment is written as `segment-<boot>-<seq>.ndjson.partial`. Sealing writes the
terminal record, `fsync`s, and then **renames** to `segment-<boot>-<seq>.ndjson`.
The importer scans only `*.ndjson`, so it can never read a segment that is still
being appended to. No reader lock, no torn line, no "is this complete yet" check
to get wrong.

It also gives torn-tail detection for free: a `.partial` file at startup is a run
that did not seal, and that is exactly what the recorder reports as `torn_tail`.

The guarantee this buys, stated exactly: **an unprivileged process in `mp-read` can
read a sealed segment and cannot write, truncate, rename, or delete anything in the
spool.** Not "the agent cannot see it" — the view has to read it (§12 of the
design), so read access is required, and claiming otherwise would be the same
overclaim the Stage 2 separation proof corrected.

## 7. Lifecycle — one invocation, one run, one segment

```
invoked by name -> preflight -> measure -> attach -> run -> detach -> seal -> exit
```

No timer, no socket, no queue, no daemon. Nothing invokes this except a person.

- **Seal** means the segment ends with a terminal record carrying: end reason
  (`workload_exit`, `duration_elapsed`, `probe_error`, `recorder_signal`), the
  final loss counter, the protection level in force, and the count of records
  written. The `fsync` then the rename (§6) is what makes the seal real; a
  `.partial` file is by definition not sealed.
- **One run, one segment.** No appending across runs, so a crash cannot corrupt a
  segment that was already sealed.
- **Restart** seals any `.partial` file as `torn_tail` before starting a new one.
- **The unit is inactive when the run ends.** `Type=oneshot` with
  `RemainAfterExit=no`, so `systemctl is-active` reports `inactive`. There is no
  resident process that could be left running by accident.

### 7.1 The startup matrix, as a test and not only as prose

Evaluated in this order at every invocation, before any scope is created or any
probe is attached:

| # | Condition | Outcome |
|---|---|---|
| 1 | detection raised or returned unknown | **refuse**, exit 2 — "cannot determine protection level" |
| 2 | `advisory`, no consent file | **refuse**, print the §2.2 block, exit 3 |
| 3 | `advisory`, consent fingerprint mismatch | **refuse**, print the §2.5 block naming the changed fields, exit 4 |
| 4 | `advisory`, consent valid | start, stamp `advisory` into the segment header |
| 5 | `enforced`, no advisory consent | start, stamp `enforced`, write the new state to `containment.json` |
| 6 | `enforced`, advisory consent present | start, stamp `enforced`, mark the old consent superseded |

Installer consent and startup consent are the **same record**. The installer
writes it; the recorder verifies it. There is no second, weaker path, and no flag
that bypasses rows 1–3.

## 8. Loss, retention, rotation, clock, crash

§10 requires these before the chain, and the recorder is where they live.

### 8.1 Rotation, and every way it can fail

**Allocation.** A byte cap, `machine-plane-max-bytes`, enforced by the recorder and
authoritative. An optional filesystem quota is a backstop against something else
writing into the same filesystem — not the mechanism, because a quota fails at
write time rather than prompting a rotation, and a rotation triggered by `ENOSPC`
has already lost the record that hit it.

**Who records a retention event: the journal, always.**

Revision 2 wrote the `retention_dropped` marker into the newest segment. Review
found the case where there is exactly one sealed segment — newest and oldest are the
same file, so the marker is written into it and then deleted with it. But the defect
is general, not an edge case: **any marker stored in a segment is destroyed by the
mechanism it describes.** With several segments, each marker survives only until its
own segment ages out, so the record of retention erodes at exactly the rate that
retention happens. Over a long life the journal of drops becomes a history of the
most recent few.

So retention is recorded in `spool/retention.journal`, and **that file is the
authoritative record of every retention event**. Segments carry no retention
markers. The importer reads the journal alongside sealed segments, and the store's
`retention_dropped` events come from it.

It also removes a decision rather than adding one: there is no "is the victim also
the newest?" branch, no distinct-surviving-segment requirement, and no rule about
which of two places wins. Requiring a second segment would deadlock the case the
cap exists for — when the allocation is smaller than one segment, dropping the only
segment is the only way to stay under it.

**Order of operations.** Only sealed segments are candidates; the active `.partial`
is never deleted, because deleting the file being appended to loses the run in
progress. Oldest sealed segment first:

1. choose the oldest sealed segment; compute what will be freed;
2. append a retention record to `retention.journal` — time, victim name, the
   victim's oldest record timestamp, bytes freed, running total;
3. `fsync` the journal;
4. `unlink` the victim;
5. `fsync` the spool directory.

The record is written **before** the deletion, for the same reason as before: a
crash between 3 and 5 leaves a claim of a drop that has not yet happened, which the
next start can reconcile, rather than a deletion nobody recorded. On start, every
retention record's victim is checked; if a victim still exists (crash between 3 and
4), a `retention_aborted` note is appended and the deletion is retried.

**If the journal cannot be written**, the run stops with `retention_unrecordable`
and a non-zero exit. There is no second fallback, because the journal *is* the
record; a fallback would only relocate the same failure.

### 8.2 The journal is bounded, because an unbounded journal is a second spool

The journal grows for as long as retention happens. "Never rotated" would make it
an unbounded second spool sitting beside the first, which defeats the cap.

Bound it with `machine-plane-retention-journal-max-bytes` (default 1 MiB). This is
metadata, not evidence, so it is excluded from the segment allocation and reported
separately in `status`.

When the journal exceeds the cap, **compact** it:

1. compute a summary from the current journal — events compacted, segments dropped,
   bytes freed, first and last dropped timestamps, cumulative count;
2. write `retention.journal.new` containing that summary record and nothing else;
3. `fsync`, `rename` over `retention.journal`, `fsync` the directory.

The summary is O(1), so compaction never leaves the journal larger than one summary
plus whatever has accumulated since.

**Compaction loses per-event detail, and says so.** The summary carries
`compactions` and `detail_from`. The view reports *"retention detail before ⟨ts⟩
has been compacted"* rather than quietly showing fewer events than occurred. A
compaction that made the record look cleaner than reality would be the same class
of error as a silent gap.

The `rename` is the commit point. A leftover `.new` is discarded on start and the
summary recomputed — safe, because the summary is derived from the journal and
recomputation is idempotent. If compaction fails for lack of space, the run stops
with `retention_unrecordable`.

**Distinguishing retention loss from recorder loss.** Every loss record carries
`loss_kind`, and the view reports them separately:

| `loss_kind` | Means |
|---|---|
| `retention_dropped` | evidence deliberately deleted under the allocation |
| `recorder_lost` | the recorder could not write (disk full, sink unavailable) |
| `probe_lost` | the probes failed to emit (buffer loss) |
| `torn_tail` | a segment that did not seal |

They are not the same event and must not be summed into one total: a deliberate
policy and a fault look identical once added together.

**Deletion while the importer is reading.** Permitted, and safe. The importer opens
a sealed segment and holds the descriptor; POSIX keeps an unlinked file readable to
an open descriptor, so that read completes. What the reader loses is any *later*
re-read — which is exactly what the `retention_dropped` marker tells it. Rotation
takes no lock and does not consult the importer; the marker is the contract.

**Disk full.** `sink_unavailable`-style gap, run continues, loss counted. Never
silent degradation.

**Clock.** Monotonic for ordering, wall clock as evidence only. Boot id
recorded. `clock_jump` emitted on an NTP step or suspend/resume, detected by
comparing monotonic deltas against wall deltas across a sampling interval.

**Crash.** A `.partial` file is sealed as `torn_tail` on the next start, and the
truncation is recorded as a gap.

**Schema version** on every record; the importer fails closed on mismatch
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
| Retention journal unwritable | run stops, `retention_unrecordable`, non-zero exit |
| Journal compaction fails | run stops, `retention_unrecordable` |
| Crash between journal write and unlink | next start reconciles, appends `retention_aborted`, retries |
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
- how much of the segment allocation is in use;
- **retention state**: how many segments have been dropped, how large the journal
  is against its own cap, when it was last compacted, and the `detail_from` horizon
  — so a shortened retention history is visible rather than looking like it never
  happened.

This matters because §17 asks whether privilege separation can be made
comprehensible to a non-technical operator. Stage 3's answer is a small, honest
status surface, not a dashboard.

## 11. Acceptance checks

Each is runnable and falsifiable. A failure voids the run rather than being
written around.

### 11.0 Two privacy gates, and the first one comes before anything attaches

A runtime scan of a segment proves what *was* recorded. It cannot undo a probe that
already ran with a dereference nobody approved. So there are two gates, and the
first is a precondition for the deployment existing at all.

**Gate 1 — static, pre-install and pre-attach.** Inspects the probe sources, the
producer and the schema as artifacts, on the machine, before installation and again
before every attach:

- every field a probe emits is enumerated from its source, not from documentation;
- no probe dereferences `args->filename`, `bprm->filename`, argv, environment, or
  any content-bearing pointer, with **the one approved pathname dereference as the
  sole exception**;
- the schema's declared field set equals the union of fields the probes emit;
- no field is typed as free text, path or blob.

A failure blocks installation and blocks the attach. It is not a warning.

**Gate 2 — runtime, post-window, before any number is reported.** Scans the sealed
segment for fields the schema does not define, and for any value that looks like a
path or free text. A failure voids the run.

Both gates are required. Gate 2 does not substitute for Gate 1.

### 11.1 The checks

**Service shape (blocker 1)**

1. The unit installs and is **startable**: `systemctl start
   machine-plane-recorder@<capture>` works. It is **not enabled**: no symlink is
   created under any `*.wants/`, `systemctl is-enabled` reports `disabled` or
   reports no installation config, and after a reboot nothing has run.
2. After the window: unit `inactive`, still not enabled, no `bpftrace` process,
   no `mp-stage3-*` scope, no `.partial` file left behind.

**Consent (blocker 2)**

3. Detection reports `advisory` for the agent's account and names every matching
   grant — not a generic "your agent may escalate".
4. The install prompt defaults to no: an empty answer aborts and writes nothing
   under `/etc/machine-plane/`.
5. A non-interactive install without `--accept-advisory` aborts.
6. Accepting writes `containment.json` with the fingerprint, level, reasons, time
   and how consent was given.
7. Startup matrix, each row exercised: unknown detection → exit 2; `advisory` with
   no consent → exit 3; `advisory` with a changed fingerprint → exit 4; `advisory`
   with valid consent → start.
8. **Fingerprint change invalidates consent:** add a grant to the test account
   between install and start; the recorder refuses, exits 4, and names the field
   that changed.
9. **`enforced` → `advisory` requires fresh consent:** begin from the no-sudo test
   account, then add a sudo grant; the next start refuses rather than continuing.
10. **`advisory` → `enforced` proceeds without advisory consent** and records the
    new state.
11. Detection on the purpose-made no-sudo account reports `enforced` — proving the
    detector discriminates rather than always answering `advisory`.
12. `status` prints the level first, and every terminal record carries the level in
    force for its run.

**Recording and ingestion**

13. The recorder launches the named workload in a scope; the measured cgroup inode
    equals the value the scope reports.
14. Exactly three probes attach; the process count is exact, not inferred from a
    substring match.
15. Events are ingested by `mp-import`; the ingested count equals emitted minus
    declared loss.
16. A workload signalled mid-run produces a sealed segment with an explicit end
    reason — not a silent stop.
17. A deliberately broken probe fails the run closed with a named error and
    produces no segment claiming completeness.
18. `clock_jump` is produced by a simulated step.

**Ownership (clarification 3)**

19. An account that has no sudo and is only in `mp-read` **can read** a sealed
    segment and **cannot** write, truncate, rename or delete anything in the spool.
    Attempted and denied, from outside the recorder.
20. `mp-import` cannot write the spool.
21. The agent's account cannot read the spool at all — not a member of `mp-read`.
22. The importer never reads a `.partial`: while a run is live, ingestion finds no
    new sealed segment.

**Retention (clarification 5, and the follow-up blocker)**

23. Rotation across several segments: force the allocation below total size; the
    oldest sealed segment is dropped and the journal records it with the right
    count and byte total. The active `.partial` is never chosen.
24. **Exactly one sealed segment over the cap** — the case found in review, where
    the victim is also the newest. After rotation: the segment is gone, the journal
    holds the retention record, and that record survives. Restart and recovery: the
    journal is intact, no spurious `retention_aborted` is written, and the importer
    reports one retention event rather than zero.
25. **Retention history outlives its segments.** Rotate repeatedly until every
    segment that ever carried a run has been dropped; every drop is still accounted
    for in the journal. This is the property revision 2 could not have passed, and
    it is what makes the fix general rather than an edge-case patch.
26. Journal compaction: write past
    `machine-plane-retention-journal-max-bytes`; the journal compacts to one summary
    record, cumulative totals are unchanged, `detail_from` is set, and the view
    reports the horizon instead of silently showing fewer events than occurred.
27. Compaction crash: leave a `retention.journal.new` behind; start discards it,
    recomputes the summary, and the totals are identical — recomputation is
    idempotent.
28. Journal unwritable: the run stops with `retention_unrecordable` and a non-zero
    exit, and no segment is deleted.
29. `loss_kind` values are distinct in the view — retention, recorder, probe and
    torn tail are not summed into one number.

**Privacy (clarification 6)**

30. Gate 1 passes on the shipped probes and fails on a probe with an injected
    `args->filename` dereference — proven both ways, before any attach.
31. Gate 2 finds no field outside the schema in a real sealed segment.

Check 19 must run as an account that has no sudo, because the agent's own account
has sudo and would pass for the wrong reason. Checks 8–11 are the pair that prove
the consent model is bound to a present state rather than to a remembered one.

## 12. Non-goals

- **Not** chain or checkpoints. Those are Stage 4.
- **Not** the kill switch, not the view. Stage 5.
- **Not** observer mode, not gateway integration.
- **Not** content capture. Metadata only, unchanged.
- **Not** a change to how Hermes runs, its account, or its configuration.
- **Not** any second host. The reference host only.
- **Not enabled at boot, and not always-on.** The unit has no `[Install]` section
  and Stage 3 has no timer. Turning this into a resident recorder is a separate
  decision requiring its own storage, rotation, CPU and wear measurements.

## 13. Risk and rollback

The recorder is an addition, not a modification: no existing service changes. It
touches one new path (`/var/lib/machine-plane`), one template unit, one importer
oneshot, and one consent file.

Rollback is: `systemctl stop 'machine-plane-recorder@*'` if a run is live, then
remove the unit files, the importer unit, `/etc/machine-plane/containment.json` and
the spool. **Nothing was enabled**, so there is nothing to disable and nothing that
survives a reboot. No agent behaviour changes at any point, because nothing in the
agent path is modified.

**The real risk is write volume, not the media.** Stage 1 measured roughly 400 KB/s
of probe output for a busy workload. Sustained, that is about **35 GB a day** — on
the reference host's 240 GB SSD it would fill the disk in roughly a week, and it is
a meaningful amount of write endurance regardless. "Always-on" is therefore not a
footnote to this stage; it is a different product, and it needs its numbers before
anyone assumes it.

Revision 1 attributed that write pressure to an SD card on the reference host.
**That was wrong** — the reference host is a laptop with a SATA SSD and no SD
device at all. The write-rate argument is unaffected; the media claim was simply
false, and the hosts that do boot from SD cards are not part of this stage.

**CPU.** Three attached probes are measurable load. The Stage 1 run sheet already
defines the abort threshold for that, and Stage 3 inherits it rather than inventing
a second one.

## 14. Decisions received, and what remains open

Review of `77892f6` settled all five questions:

| Question | Decision |
|---|---|
| Renumbering | Accepted. Deployment is Stage 3; chain/checkpoints move to Stage 4; view/switch to Stage 5. Amend design §16 on approval. |
| Advisory gate | Right shape, not ceremony — now bound to a privilege fingerprint (§2.5). |
| Supervisor only | Correct for a first deployment. Observer mode stays a separate stage. |
| Retention here | Yes, before chain/checkpoints, with the failure semantics now in §8.1. |
| No-content check | Static gate before install and before every attach (§11.0); runtime scan before reporting. |

Open, and deliberately not decided by this proposal:

- whether Stage 3 ever becomes enabled, which needs its own storage, rotation, CPU
  and wear measurements;
- whether option (b) — moving the agent to a dedicated unprivileged account — is
  undertaken, which is the only route to `enforced` on the reference host.

## 15. What this does not claim

- It does not claim the agent is contained. On today's account layout it is not,
  and the system says so at install, at every start, in `status`, and in every
  run's terminal record. A user who accepts advisory mode has made an informed
  choice — and that consent expires when the situation it described changes.
- It does not claim the probes are complete. The seven declared gaps are unchanged.
- It does not claim always-on is affordable. Only that one bounded run works.
- It does not claim a runtime scan compensates for an unsafe probe. Gate 1 exists
  because it does not: a segment scan cannot unrun a dereference.
- It does not claim the view exists. It does not.
