#!/usr/bin/env bash
# workload.sh — the scripted action list from action-manifest.json.
#
# Runs INSIDE the dedicated systemd scope. Sequence:
#   1. assert the environment actually arrived (SCRATCH, PORT, CLOSED_PORT)
#   2. record identity: cgroup id, cgroup path, PID, PID start ticks
#   3. wait for the gate file, so the probes are attached before any action
#   4. run A1..A10
#   5. record the exit identity and the end marker
#
# The join to the kernel stream is by cgroup id, not timestamp: this log uses
# wall clock, bpftrace uses monotonic nanoseconds, and they are different clocks.

set -u

# --- 1. environment assertion -------------------------------------------------
# systemd-run --scope does not inherit non-exported shell variables, so the
# variables are passed explicitly with --setenv. If they did not arrive, say so
# loudly rather than failing halfway through the action list.
missing=""
for v in SCRATCH PORT CLOSED_PORT; do
    eval "val=\${$v:-}"
    [ -n "$val" ] || missing="$missing $v"
done
if [ -n "$missing" ]; then
    echo "FATAL workload environment incomplete:$missing" >&2
    exit 90
fi

record() { printf '%s\n' "$*" >>"$SCRATCH/workload.log"; }

# Field 22 of /proc/<pid>/stat is the start time in clock ticks since boot.
# PID + start ticks + boot id is the only sound identity; the process name is not.
start_ticks() { cut -d' ' -f22 "/proc/$1/stat" 2>/dev/null || echo gone; }

CGROUP_PATH="$(cut -d: -f3 /proc/self/cgroup)"
CGROUP_ID="$(stat -c '%i' "/sys/fs/cgroup${CGROUP_PATH}" 2>/dev/null || echo unknown)"
PID=$$
START="$(start_ticks $$)"

# Read by run.sh to build the manifest before any probe attaches.
printf '%s %s %s %s\n' "$CGROUP_ID" "$CGROUP_PATH" "$PID" "$START" >"$SCRATCH/identity"

record "PRECHECK scratch=${SCRATCH} port=${PORT} closed_port=${CLOSED_PORT} pid=${PID} cgroup_id=${CGROUP_ID} cgroup_path=${CGROUP_PATH} start_ticks=${START}"
record "MARK workload phase=start wall=$(date +%s.%N)"

# --- 3. gate ------------------------------------------------------------------
# Nothing runs until the probes are attached. 30 s is generous for three
# attachments and short enough that a stuck run aborts rather than hangs.
for _ in $(seq 1 300); do
    [ -e "$SCRATCH/go" ] && break
    sleep 0.1
done
[ -e "$SCRATCH/go" ] || { record "FATAL gate timeout"; exit 91; }
record "MARK workload phase=go wall=$(date +%s.%N)"

# --- 4. actions ---------------------------------------------------------------
# A1 — a successful exec.
/bin/echo stage1-a1 >/dev/null
record "ACT A1 exec_success rc=$?"

# A2 — an exec that cannot succeed. Stage 0 showed this produces several failed
# attempts for a single intent, which is why outcomes are counted, not entries.
/nonexistent/stage1-binary-that-cannot-exist 2>/dev/null
record "ACT A2 exec_failure rc=$?"

# A3 — write-intent open in the scratch directory.
printf 'stage1-a3\n' >"$SCRATCH/a3.txt"
record "ACT A3 write_file rc=$?"

# A4 — read-only open of the same file.
cat "$SCRATCH/a3.txt" >/dev/null
record "ACT A4 read_file rc=$?"

# A5 — rename. NOT OBSERVED by the Stage 1 probe set: file.bt instruments
# openat only. Recorded here so the gap is explicit, and reported as
# intentionally unsupported rather than as a missed action.
mv "$SCRATCH/a3.txt" "$SCRATCH/a5.txt"
record "ACT A5 rename_file rc=$? observed_by=none"

# A6 — unlink. Same: not instrumented by this probe set.
rm -f "$SCRATCH/a5.txt"
record "ACT A6 unlink_file rc=$? observed_by=none"

# A7 — a child that outlives its parent briefly. It inherits the scope cgroup,
# which is what makes subprocess attribution testable.
bash -c '(sleep 2) &'
record "ACT A7 background_child rc=$?"

# A8 — a connect that succeeds, against the loopback listener.
python3 -c "import socket; s=socket.create_connection(('127.0.0.1',$PORT),2); s.close()" 2>/dev/null
record "ACT A8 connect_success rc=$?"

# A9 — a connect that is refused, against a closed loopback port.
python3 -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1',$CLOSED_PORT))" 2>/dev/null
record "ACT A9 connect_failure rc=$?"

# A10 — a process that exits immediately after an observed action.
bash -c "printf x >'$SCRATCH/a10.txt'; exit 0"
record "ACT A10 exit_during_action rc=$?"

record "IDENT pid=${PID} end_start=$(start_ticks $$)"
record "MARK workload phase=end wall=$(date +%s.%N)"
