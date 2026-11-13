#!/usr/bin/env bash
# workload.sh — the scripted action list from action-manifest.json.
#
# Runs INSIDE the dedicated systemd scope created by the run sheet, so every
# process it starts inherits the same cgroup and is attributable to this run.
#
# It writes its own record to $SCRATCH/workload.log: identity at entry and exit,
# and one line per action with the return code. That record is the human-readable
# side of the comparison; the join to the kernel stream is by cgroup id, not by
# timestamp, because the two sides use different clocks.

set -u

SCRATCH="${SCRATCH:?SCRATCH must be set}"
PORT="${PORT:-18080}"
CLOSED_PORT="${CLOSED_PORT:-18081}"

record() { printf '%s\n' "$*" >>"$SCRATCH/workload.log"; }

# Field 22 of /proc/<pid>/stat is the start time in clock ticks since boot.
# PID + start time + boot id is the only sound identity; the process name is not.
start_ticks() { cut -d' ' -f22 "/proc/$1/stat" 2>/dev/null || echo gone; }

CGROUP_PATH="$(cut -d: -f3 /proc/self/cgroup)"
CGROUP_ID="$(stat -c '%i' "/sys/fs/cgroup${CGROUP_PATH}" 2>/dev/null || echo unknown)"

record "MARK workload phase=start wall=$(date +%s.%N)"
record "IDENT pid=$$ start=$(start_ticks $$) cgroup_id=${CGROUP_ID} cgroup_path=${CGROUP_PATH}"

# A1 — a successful exec.
/bin/echo stage1-a1 >/dev/null
record "ACT A1 exec_success rc=$?"

# A2 — an exec that cannot succeed. This is the case Stage 0 showed produces
# several failed attempts for a single intent.
/nonexistent/stage1-binary-that-cannot-exist 2>/dev/null
record "ACT A2 exec_failure rc=$?"

# A3 — write-intent open in the scratch directory.
printf 'stage1-a3\n' >"$SCRATCH/a3.txt"
record "ACT A3 write_file rc=$?"

# A4 — read-only open of the same file.
cat "$SCRATCH/a3.txt" >/dev/null
record "ACT A4 read_file rc=$?"

# A5 — rename inside the scratch directory.
mv "$SCRATCH/a3.txt" "$SCRATCH/a5.txt"
record "ACT A5 rename_file rc=$?"

# A6 — unlink inside the scratch directory.
rm -f "$SCRATCH/a5.txt"
record "ACT A6 unlink_file rc=$?"

# A7 — a child that outlives its parent briefly. It inherits the scope cgroup,
# which is the case that makes subprocess attribution testable.
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

record "IDENT pid=$$ end_start=$(start_ticks $$)"
record "MARK workload phase=end wall=$(date +%s.%N)"
