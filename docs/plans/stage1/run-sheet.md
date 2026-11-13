# Stage 1 run sheet — machine plane producer evaluation

**Status:** not approved to run. Requires Alice's review of this revision and
Bill's explicit window. Nothing here has been executed.

Revision 2 — addresses the three execution blockers and two measurement
clarifications from the run-sheet review at `44f91ed`.

The executable form of this sheet is `run.sh`, `preflight.sh`, `workload.sh`,
`probes/*.bt` and `action-manifest.json`, all in this directory. Where prose and
code disagree, the code is what runs — which is why the code is committed.

---

## 0. Approval record

| Field | Value |
| --- | --- |
| Host | Emily only (not Art, not any other machine) |
| Operator | Tom (agent), supervised by Bill |
| Window | **NOT SET** — Bill names a 20-minute window explicitly |
| Prerequisite | Alice's review of this revision at this commit |
| Pointer exception | `str(args->filename)` in `exec.bt` — **approved** as pathname metadata |
| Probe files | `probes/{exec,file,net}.bt` |
| Baseline | `action-manifest.json` |

No window exists until Bill states one. This document is the checklist, not the
authorisation.

---

## 1. What "bounded" means here, precisely

The earlier revision called this run "read-only". That was wrong and worth
correcting: the run mounts a filesystem, creates a systemd scope, starts a
listener, attaches BPF programs and then removes all of it.

The accurate bound is **no persistent host changes**. The following transient
changes are explicitly allowed, and nothing else is:

1. A 256 MB tmpfs mounted at `/var/tmp/machine-plane-stage1/<capture_id>`.
2. One transient systemd user scope named `mp-stage1-<capture_id>`.
3. A loopback HTTP listener on `127.0.0.1:18080`.
4. Three BPF programs attached for the duration of the run and detached after.
5. Files written under the scratch tmpfs, and one copy of that directory at
   `/home/willard/reviews/stage1-<capture_id>`.

Everything in that list is removed before the run is reported. Verification is
in section 9 and is not optional.

**No persistent change** means: no package installed, no service enabled or
disabled, no sysctl changed, no file outside the scratch path created or
modified, no pinned BPF object left behind, no user or group changed, and no
change to Emily's gateway, its config, or anything it supervises.

---

## 2. Run identity and cgroup setup

A gateway session does not put a manually launched workload in a unique scope,
and Emily's own gateway, watchers and system services generate traffic
continuously. Without a dedicated scope every event is ambiguous between the
workload and the host.

### Identifiers, captured before anything starts

`run.sh` generates these and fails if any already exists:

```bash
CAPTURE_ID="mp1-$(date -u +%Y%m%dT%H%M%SZ)-<6 hex>"
SCOPE="mp-stage1-${CAPTURE_ID}"
SCRATCH="/var/tmp/machine-plane-stage1/${CAPTURE_ID}"
RESULT_DIR="/home/willard/reviews/stage1-${CAPTURE_ID}"
BOOT_ID="$(cat /proc/sys/kernel/random/boot_id)"
```

### Environment propagation

The scope does not inherit unexported shell variables. The earlier revision
would have aborted the workload immediately at `SCRATCH="${SCRATCH:?...}"`. The
values are now passed explicitly:

```bash
systemd-run --user --scope --collect --unit="$SCOPE" \
  --setenv=SCRATCH="$SCRATCH" --setenv=PORT=18080 --setenv=CLOSED_PORT=18081 \
  --property=MemoryMax=768M --property=CPUQuota=200% \
  -- bash -c 'timeout 300 bash "$SCRATCH/workload.sh"'
```

And the workload asserts the environment arrived before doing anything:

```bash
# workload.sh, first thing
for v in SCRATCH PORT CLOSED_PORT; do ... done
[ -z "$missing" ] || { echo "FATAL workload environment incomplete:$missing" >&2; exit 90; }
```

The first line the workload writes is a `PRECHECK` recording the received
`SCRATCH`, `PORT`, `CLOSED_PORT`, PID, cgroup id, cgroup path and start ticks.
If that line is absent from `workload.log`, the run is void.

### The scope

- `--scope` keeps the whole workload in one cgroup.
- `--collect` removes the unit even if the command fails.
- `MemoryMax` and `CPUQuota` bound the *workload* so it cannot compete with
  Emily's gateway.
- The loopback listener runs **outside** the scope, so it does not appear in
  attributed events.

### Identity, and why it is not a name

`/proc/<pid>/stat` field 22 is the process start time in clock ticks since boot.
PID + start ticks + boot id is the only sound identity; process names truncate
at 15 characters and are recorded as `label` only.

### Gate ordering

The scope starts first and blocks on a gate file. `run.sh` reads the scope's
identity, writes the run manifest, attaches the probes, and only then touches
the gate. This means:

- the manifest records the real cgroup id **before** any probe attaches;
- no action can run before the probes are observing.

---

## 3. Preflight: the join key is measured, not assumed

The entire attribution measurement depends on bpftrace's `cgroup` builtin
returning the cgroup directory's inode. That is asserted in the design; it is now
also measured.

`preflight.sh` is disposable, takes about ten seconds, and does this:

1. Starts a transient scope that writes `$$` and the inode of its own cgroup
   directory, then sleeps 6 s, then opens a file **in that same process** (via a
   shell redirection, not an external command).
2. Attaches a one-line probe filtered to that PID.
3. Compares the `cgroup` value the probe emits with the inode the process
   recorded.

The trigger has to happen in the recorded process. An external command such as
`cat` forks, so it would open the file under a different PID and the filter would
never match — which is exactly how the first attempt at this preflight failed, on
Emily, before any part of the real run was attempted. The preflight voided the
run, as designed; nothing proceeded and cleanup was verified complete.

**If the values differ, the run is void.** There is no fallback to process names
or timestamps — those would produce a confident answer with no join key behind
it.

The preflight compiles (`bpftrace -d`, checked at review time) and removes its
own temporary directory and scope on every exit path.

---

## 4. The comparison baseline

**An independent action manifest is used. Agent Diary is not read, exported or
touched.**

The cost of that choice, stated plainly: this measures whether the instrument
sees *the actions the manifest lists*. It does not measure whether the
instrument agrees with what an agent recorded about itself. That is Stage 2 work
and needs its own boundary.

### Accounting buckets

Every action is reported in exactly one bucket:

- **not instrumented** — no probe covers this class;
- **instrumented, not observed** — a probe covers it and nothing was recorded
  (a real miss);
- **observed outside scope** — events appeared but not in this run's cgroup;
- **observed and attributed** — recorded and joined to the run's cgroup.

**A5 (rename) and A6 (unlink) are not instrumented.** `file.bt` observes
`openat` only. They are labelled `"instrumented": false` in the manifest, and
they are excluded from the action-recall denominator rather than being counted as
misses. Stage 1 does not claim to observe rename or unlink.

---

## 5. Probes

| File | Probes | Records |
| --- | --- | --- |
| `probes/exec.bt` | `sys_enter_execve`, `sys_exit_execve`, `sched_process_exec` | attempt, outcome, cgroup, label; path on a successful transition |
| `probes/file.bt` | `sys_enter_openat`, `sys_exit_openat` | write-intent flag, outcome, cgroup, label |
| `probes/net.bt` | `sys_enter_connect`, `sys_exit_connect` | outcome, cgroup, label |

### The approved pointer exception

`exec.bt` reads `args->filename` in `sched_process_exec`. This is the only
pointer read in the set, and it is **approved** for this experiment as pathname
metadata.

- It is a pathname, not content.
- It fires only on a successful image transition, not on every attempt.
- Without it, the probe can report that an image transition occurred but not
  which binary succeeded.

### Prohibited, everywhere

`argv`, `envp`, the `filename` pointer in `sys_enter_execve` and in
`sys_enter_openat`, file buffers, read/write payloads, `sockaddr`, IP addresses,
packet payloads, plaintext model traffic, and `--unsafe`.

Metadata-only is a property of the probe definitions, not something to check for
in the output afterwards.

### Pre-attach inspection

Run `scripts/check-probes.sh` from the `bounded-instrumentation-experiment`
skill, or the two greps it performs. Expected: exactly one call,
`str(args->filename)` in `exec.bt`; every `argv`/`envp` hit inside a comment.

Each probe must also compile against Emily's own tracepoint definitions:

```bash
sudo bpftrace -d probes/exec.bt   # dry run: loads nothing, attaches nothing
```

Three scripts that compile is a different claim from three scripts that look
plausible.

---

## 6. Scratch and the byte cap

The scratch directory is a **256 MB tmpfs**, not a directory on Emily's disk:

```bash
sudo mount -t tmpfs -o size=256M,mode=0700 tmpfs "$SCRATCH"
```

This answers "what happens if it fills" without risking the host: writes fail
with `ENOSPC`, probe output is lost, Emily's disk is untouched, and the run is
void. A directory on the real filesystem would turn an induced-loss experiment
into an uncontrolled disk test.

Preconditions: at least 2000 MB available RAM (tmpfs is memory), no existing
scope, no existing scratch directory, and port 18080 not already occupied. The
port check **fails closed** — if anything is listening, the run stops rather than
binding over it.

---

## 7. Sequence

`run.sh` is the executable form. It is not a summary — it is what will run.

1. **Preconditions.** No `bpftrace` running, no stale `mp-stage1-*` scope, no
   existing scratch path, port 18080 free, ≥2000 MB available.
2. **Scratch.** Create and mount the 256 MB tmpfs; stage `workload.sh` into it.
3. **Scope.** Start the workload inside the scope. It blocks on the gate.
4. **Manifest.** Read the scope's identity; write `run-manifest.json` into the
   scratch with capture id, boot id, host, scope name, cgroup id, cgroup path,
   workload PID and start ticks. This happens **before any attach**.
5. **Listener.** Start the loopback listener outside the scope; fail if it dies.
6. **Probes.** Attach the three probes with absolute paths from `$PROBE_DIR`;
   resolve their real PIDs with `pgrep -f "bpftrace $PROBE_DIR/"`; require
   exactly three. If not three, abort.
7. **Release.** Touch the gate; wait for the scope to finish.
8. **Stop.** `kill -INT` each recorded probe PID by number, then confirm each has
   exited. There is no `pkill -f` wildcard anywhere: a broad pattern can miss the
   real process when the working directory changes, and can kill an unrelated one
   if the precondition races.
9. **Verify and clean.** Check the workload log reached both markers; capture the
   result; unmount and remove the tmpfs.

Cleanup is a `trap` on `EXIT`, `INT` and `TERM`, so it runs on abort as well as
success. It stops the probes by PID, stops the listener, stops the scope, copies
the result out and unmounts.

---

## 8. Loss experiment

A **separate session, separate capture id, separate scratch.** Deliberately
induced loss must never be confused with the ordinary attribution result. The
executable form is `loss.sh`.

The lever is `BPFTRACE_PERF_RB_PAGES`, which is 64 pages per CPU by default on
bpftrace v0.20.2 and is deliberately undersized here.

| Parameter | Value | Why |
| --- | --- | --- |
| `BPFTRACE_PERF_RB_PAGES` | **2** (vs default 64) | 32× smaller buffer per CPU |
| Workload | tight loop of `openat` on one scratch file | high, steady, predictable rate |
| Duration | **60 s hard limit** (`timeout 60`) | bounded and cheap to abort |
| Target rate | ~5,000 opens/s | above what 2 pages can carry |
| Output cap | 256 MB tmpfs | same mechanism as the main run |
| Loss signal | bpftrace's own lost-event count on stderr | the producer's own count, not an inference |
| Stop condition | 60 s elapsed, or bpftrace exits, or `ENOSPC` | any of the three ends it |

What is measured is not "did loss happen" but **whether loss was reported**, and
whether it is bounded to an interval. If no loss occurs at the capped rate, the
finding is "no loss observed at this rate", not "loss does not happen". The rate
is not raised to chase a result.

### This measurement is host-wide, not scope-local

The loss probe is deliberately **unfiltered**. It observes `openat` across the
whole host, so Emily's own gateway, watchers and system services contribute to
the pressure the buffer is under. The result is therefore **host-wide buffer
pressure under a known local workload** — never presented as loss attributable
solely to the churn scope. `session.json` records `scoped_to` in those words.

### The script refuses three ways of lying to itself

1. **A dead probe is not a quiet probe.** The window is polled once a second, so
   a probe that exits at second 5 is noticed at second 5, not at second 60. Early
   exit is recorded with the second it happened, and voids the session: the
   specified 60-second window was not observed.
2. **A session where nothing ran is not a result.** The workload writes
   `churn.ready` when it passes the gate and appends a running operation count.
   Both are checked; either missing or at zero voids the session. "The file
   exists" is not evidence the workload started.
3. **Non-empty stderr is not loss.** Output is classified as `loss_reported`
   (with the count), `no_loss_reported`, `probe_error`, or
   `loss_mentioned_unparsed` — the last for text that mentions loss in a form the
   script does not recognise. That last case exists so an unfamiliar message is
   never silently reported as "no loss".

---

## 9. Abort conditions and verification

Stop immediately, and let the trap clean up, if:

- tmpfs usage exceeds 200 MB and is still climbing;
- Emily's load average exceeds 4× the pre-run baseline for 30 s;
- any probe writes outside the scratch directory;
- a probe PID survives `SIGINT`;
- the workload reports a cgroup different from the one recorded;
- the preflight cgroup check fails;
- the session passes 10 minutes;
- Bill says stop — this overrides everything above without discussion.

Post-run verification, all of which must hold:

```bash
pgrep -x bpftrace                              # empty
systemctl --user list-units --all 'mp-stage1-*' # nothing
ls /sys/fs/bpf/                                # no pinned objects (we pin none)
ss -ltn | grep ':18080 '                       # nothing
mount | grep machine-plane-stage1              # nothing
```

An aborted run is reported as aborted, with the reason. It is not quietly
re-run.

---

## 10. No-content inspection

Against the captured output, before any number is quoted:

- [ ] probe sources pass the forbidden-builtin check (one `str()`, the approved
      exec pathname, everything else none);
- [ ] 50 sampled lines per probe: fields are pid/tid/rc/cgroup/ts/label, plus
      path for exec only;
- [ ] no file contents, no command lines, no addresses, no payloads;
- [ ] the exec path column contains binary paths only;
- [ ] the scratch contains no copy of any user file.

Any failure voids the run regardless of the coverage numbers.

---

## 11. Result

Reported as separate numbers, never a single score:

1. identity/attribution precision
2. scripted-action recall, with the **not-instrumented** cases excluded and
   stated
3. subprocess attribution
4. noise, including failed syscall attempts
5. loss detection and boundedness
6. CPU / memory / output / latency cost
7. privilege required
8. content-safety inspection
9. cleanup success

The headline must carry three limitations:

- **coverage of the tested action classes under the tested workload** — not
  agent coverage;
- **one host**, so cost, coverage and privilege do not generalise to the fleet;
- **optimistic**, because a scripted workload is easier than an agent doing
  something unexpected.

A "do not proceed" is a successful outcome.
