# Stage 1 run sheet — machine plane producer evaluation

**Status:** not approved to run. Requires Alice's review of this sheet and
Bill's explicit window. Nothing here has been executed.

This is the acceptance record requested in the Stage 1 review. If any step
differs from what is written here during the run, the run is void.

---

## 0. Approval record

| Field | Value |
| --- | --- |
| Host | Emily only (do not run on Art or any other machine) |
| Operator | Tom (agent), supervised by Bill |
| Window | **NOT SET** — Bill names a 20-minute window explicitly |
| Prerequisite | Alice's review of this sheet at this commit |
| Probe files | `docs/plans/stage1/probes/{exec,file,net}.bt` |
| Baseline | `docs/plans/stage1/action-manifest.json` |

No window exists until Bill states it. This document is the checklist, not the
authorisation.

---

## 1. Run identity and cgroup setup

A normal gateway session does not put a manually launched workload into a
unique scope, and Emily's own gateway, watchers and system services generate
traffic continuously. Without a dedicated scope the attribution question cannot
be answered at all — every event would be ambiguous between the workload and the
host.

### Identifiers, captured before anything starts

```bash
CAPTURE_ID="mp1-$(date -u +%Y%m%dT%H%M%SZ)-$(head -c3 /dev/urandom | od -An -tx1 | tr -d ' \n')"
BOOT_ID="$(cat /proc/sys/kernel/random/boot_id)"
SCOPE="mp-stage1-${CAPTURE_ID}"
SCRATCH="/var/tmp/machine-plane-stage1/${CAPTURE_ID}"
```

Both `CAPTURE_ID` and `BOOT_ID` are written into the run manifest before the
first probe attaches, so a record can never be separated from the boot it came
from.

### The scope

The workload runs inside a transient systemd scope, created out of band:

```bash
systemd-run --user --scope --collect --unit="$SCOPE" \
  --property=MemoryMax=768M --property=CPUQuota=200% \
  -- bash -lc "source ${SCRATCH}/workload.sh"
```

- `--scope` puts the whole workload in one cgroup rather than one service unit
  per command.
- `--collect` removes the scope even if the command fails, so a crash does not
  leave a unit behind.
- `MemoryMax` and `CPUQuota` keep the workload from competing with Emily's
  gateway. They are limits on the *workload*, not on the probes.

### Expected membership

Inside the workload, before any action runs, record:

```bash
cat /proc/self/cgroup                 # the scope path
stat -c '%i' /sys/fs/cgroup"$(cat /proc/self/cgroup | cut -d: -f3)"
echo "$$ $(cut -d' ' -f22 /proc/$$/stat)"     # PID and start time
```

The cgroup id reported by the `cgroup` builtin in bpftrace is the inode of the
cgroup directory, so this `stat` gives the exact value the probes will print.
That is the join key.

Expected PIDs inside the scope: the workload shell, each short-lived child it
spawns, and the background child in A7 which outlives its parent by ~2 s. The
loopback listener is **outside** the scope deliberately — it is infrastructure,
not workload, and should not appear in attributed events.

The manifest records: capture id, boot id, scope name, cgroup path, cgroup id,
the workload PID and its start time at entry and at exit, and the action list.

### PID start time, and why

`/proc/<pid>/stat` field 22 is the process start time in clock ticks since boot.
PID + start time + boot id is the only sound way to distinguish this run's
process from a later process that reuses the number. Process names are recorded
as `label` only and are never used as identity — they truncate at 15 characters,
which Stage 0 already demonstrated.

Parsing hazard: `comm` is field 2 and may contain spaces and parentheses. The
`cut -d' ' -f22` above is safe for these short names but should be checked
against the manifest rather than trusted blindly.

### Removal

```bash
systemctl --user stop "$SCOPE.scope" 2>/dev/null || true
systemctl --user reset-failed "$SCOPE.scope" 2>/dev/null || true
```

`--collect` normally removes it at exit; the explicit stop is for the case where
the run is aborted.

---

## 2. The comparison baseline

**An independent action manifest is used. Agent Diary is not read, exported, or
touched in any way.**

The kernel stream is compared against `action-manifest.json`, which states what
the workload intends to do. This deliberately avoids making the Stage 1 result
depend on the Diary export path, and it removes any question about importing
machine events into memory.

The cost of that choice is stated plainly: this measures whether the instrument
sees *the actions the manifest lists*. It does not measure whether the
instrument agrees with what an agent recorded about itself. That comparison is
Stage 2 work and needs its own boundary.

---

## 3. Probes

Three probe files, attached read-only, reviewed before attach:

| File | Probes | Records |
| --- | --- | --- |
| `probes/exec.bt` | `sys_enter_execve`, `sys_exit_execve`, `sched_process_exec` | attempt, outcome, cgroup, label; path only on a successful transition |
| `probes/file.bt` | `sys_enter_openat`, `sys_exit_openat` | write-intent flag, outcome, cgroup, label |
| `probes/net.bt` | `sys_enter_connect`, `sys_exit_connect` | outcome, cgroup, label |

### The one pointer read, surfaced for review

`exec.bt` reads `args->filename` in `sched_process_exec`. This is the only
pointer dereference in the probe set.

- It is a pathname, not content.
- It fires only on a successful image transition, not on every attempt.
- It is the only way to know *which* binary ran, which is central to action
  recall.

If this is not acceptable, drop that one line and action recall becomes
event-class and timing based, with the consequence that "a process exec'd" is
recorded but not "which process". **This is a decision for review, not something
to be settled during the run.**

### Prohibited, everywhere

- `argv`, `envp`, any argument vector
- the `filename` pointer in `sys_enter_openat` / `sys_enter_execve`
- file buffers, read/write payloads, `cat()` of user memory
- `sockaddr`, IP addresses, ports, packet payloads
- plaintext model traffic of any kind
- `--unsafe`

Metadata-only is a property of the probe definitions. It is not something to be
checked for in the output afterwards.

### Pre-attach inspection

Two checks, because a naive grep for `str(` counts the comments that explain what
is *not* captured.

```bash
# Calls only, ignoring comment lines.
grep -nE '\b(str|cat|buf)\(' probes/*.bt | grep -v '^[^:]*:[0-9]*: *[*/#]'

# Any mention of argument vectors, comments included.
grep -nE 'argv|envp' probes/*.bt
```

Expected: exactly one call — `str(args->filename)` in `exec.bt`, line 47. Every
`argv`/`envp` hit must be inside a comment. Anything else voids the run.

---

## 4. Scratch space and the byte cap

The scratch directory is a **256 MB tmpfs**, not a directory on Emily's disk:

```bash
sudo mkdir -p "$SCRATCH"
sudo mount -t tmpfs -o size=256M,mode=0700 tmpfs "$SCRATCH"
```

This answers "what happens if it fills" without risking the host: when the tmpfs
reaches 256 MB, further writes fail with `ENOSPC`. Probe output is lost, the host
filesystem is untouched, and the run is void — but Emily is unaffected. That is
the whole point of using tmpfs rather than a quota.

Preconditions, checked before mount:

- at least 2 GB of free RAM on Emily (tmpfs lives in memory)
- no existing scope, scratch directory, or probe process with this capture id

Post-run, the scratch is read for the result, then:

```bash
sudo umount "$SCRATCH"
sudo rmdir "$SCRATCH"
```

---

## 5. Sequence

Each numbered step is a separate command. Stop at the first failure.

```bash
# 1. preconditions
pgrep -a bpftrace            # must be empty
systemctl --user list-units --all 'mp-stage1-*' --no-legend   # must be empty
free -m | awk 'NR==2 {print $7}'   # available MB, require > 2000

# 2. identity and manifest
#    (CAPTURE_ID, BOOT_ID, SCOPE, SCRATCH as in section 1)
#    write run-manifest.json into SCRATCH before any attach

# 3. scratch
sudo mkdir -p "$SCRATCH"
sudo mount -t tmpfs -o size=256M,mode=0700 tmpfs "$SCRATCH"

# 4. loopback listener, outside the scope
python3 -m http.server 18080 --bind 127.0.0.1 &
LISTENER=$!

# 5. probes, background, logging to scratch
sudo bpftrace probes/exec.bt -o "$SCRATCH/exec.log" 2>"$SCRATCH/exec.err" &
sudo bpftrace probes/file.bt -o "$SCRATCH/file.log" 2>"$SCRATCH/file.err" &
sudo bpftrace probes/net.bt  -o "$SCRATCH/net.log"  2>"$SCRATCH/net.err" &
sleep 2   # allow attach; verify each is running before continuing

# 6. the workload, inside its dedicated scope
systemd-run --user --scope --collect --unit="$SCOPE" \
  --property=MemoryMax=768M --property=CPUQuota=200% \
  -- bash -lc "source ${SCRATCH}/workload.sh"

# 7. stop
sudo pkill -INT -f 'bpftrace probes/' ; sleep 1
kill "$LISTENER" 2>/dev/null

# 8. capture the result out of the tmpfs, then tear down
sudo cp -r "$SCRATCH" "/home/willard/reviews/stage1-${CAPTURE_ID}"
sudo umount "$SCRATCH" && sudo rmdir "$SCRATCH"

# 9. verification
pgrep -a bpftrace                                   # empty
systemctl --user list-units --all 'mp-stage1-*'     # nothing
ls /sys/fs/bpf/                                     # no pinned objects (we pin none)
ss -ltnp | grep 18080                               # nothing
mount | grep machine-plane-stage1                   # nothing
```

The run manifest is written **before** attach, so that a failed run still leaves
a record of what was attempted.

---

## 6. Loss experiment

**A separate session, separate capture id, separate scratch.** Deliberately
induced loss must not be confused with the ordinary attribution result.

The lever is `BPFTRACE_PERF_RB_PAGES`, which defaults to 64 pages per CPU on
bpftrace v0.20.2 and is undersized here deliberately.

| Parameter | Value | Why |
| --- | --- | --- |
| `BPFTRACE_PERF_RB_PAGES` | **2** (vs default 64) | a 32× smaller buffer per CPU |
| Workload | tight loop of `openat` on one scratch file | a high, steady, predictable rate |
| Duration | **60 s hard limit** (`timeout 60`) | bounded, and short enough to abort cheaply |
| Target rate | ~5,000 opens/s | above what 2 pages can carry |
| Output cap | 256 MB tmpfs | same mechanism as the main run |
| Loss signal | bpftrace's own `Lost N events` on stderr | the producer's own count, not an inference |
| Stop condition | 60 s elapsed, or bpftrace exits, or ENOSPC | any of the three ends it |

What is measured: whether loss is *reported at all*, and whether it is bounded
to an interval. The expected interval start and end are recorded from the run's
own timestamps so the reported loss can be checked against it, rather than taken
on trust.

If no loss occurs at the capped rate, the honest finding is "no loss observed at
this rate", not "loss does not happen". The rate is not raised beyond 5,000/s to
chase a result.

The design's requirement is not that loss never happens. It is that loss is
**stated**. A probe that overflows silently fails the experiment even if its
coverage numbers look good.

---

## 7. Abort conditions

Stop immediately and tear down if any of these occur:

- tmpfs usage exceeds 200 MB and is still climbing
- Emily's load average exceeds 4× the pre-run baseline for 30 s
- any probe writes outside the scratch directory
- a bpftrace process survives the stop step
- the workload escapes its scope (a PID in the workload reports a different
  cgroup)
- the session passes 10 minutes
- Bill says stop — this overrides everything above, without discussion

An aborted run is reported as aborted, with the reason. It is not quietly
re-run.

---

## 8. No-content inspection

Run against the captured output before any number is reported:

- [ ] `grep -c` for the forbidden builtins in the probe sources: one `str()`,
      the exec pathname, and nothing else
- [ ] sample 50 output lines per probe: fields are pid/tid/rc/cgroup/ts/label
      (+ path for exec only)
- [ ] confirm no file contents, no command lines, no addresses, no payloads
- [ ] confirm the exec pathname column contains binary paths only
- [ ] confirm the scratch directory contains no copy of any user file

Any failure here voids the run regardless of the coverage numbers.

---

## 9. Result

Reported as separate numbers, not a single score:

1. identity/attribution precision
2. scripted-action recall
3. subprocess attribution
4. noise, including failed syscall attempts
5. loss detection and boundedness
6. CPU / memory / output / latency cost
7. privilege required
8. content-safety inspection
9. cleanup success

The headline must carry three limitations, in these words or equivalent:

- **coverage of the tested action classes under the tested workload** — not
  agent coverage;
- **one host**, so cost, coverage and privilege do not generalise to the fleet;
- **optimistic**, because a scripted workload is easier than an agent doing
  something unexpected.

A "do not proceed" is a successful outcome.
