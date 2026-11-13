#!/usr/bin/env bash
# run.sh — Stage 1 coverage session. Not approved to run yet.
#
# Everything here is deliberate: absolute paths, an explicit scope with exported
# environment, the run manifest written BEFORE any probe attaches, PID-directed
# teardown, and a cleanup trap on every exit path including abort.
#
# Bounds: no persistent host changes. The transient changes this makes are
# enumerated in run-sheet.md section 2.

set -uo pipefail

SHEET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROBE_DIR="$SHEET_DIR/probes"
PORT="${PORT:-18080}"
CLOSED_PORT="${CLOSED_PORT:-18081}"
WORKLOAD_LIMIT=300

CAPTURE_ID="mp1-$(date -u +%Y%m%dT%H%M%SZ)-$(head -c3 /dev/urandom | od -An -tx1 | tr -d ' \n')"
BOOT_ID="$(cat /proc/sys/kernel/random/boot_id)"
SCOPE="mp-stage1-${CAPTURE_ID}"
SCRATCH="/var/tmp/machine-plane-stage1/${CAPTURE_ID}"
RESULT_DIR="/home/willard/reviews/stage1-${CAPTURE_ID}"

LISTENER_PID=""
SCOPE_JOB=""
PROBE_PIDS=()
MOUNTED=0
DONE=0

log() { printf '%s\n' "$*" >&2; }
fail() { log "FATAL: $*"; exit 1; }

cleanup() {
    local rc=$?
    set +e
    [ "$DONE" -eq 1 ] && return 0

    log "cleanup: stopping probes"
    for p in "${PROBE_PIDS[@]:-}"; do [ -n "${p:-}" ] && sudo kill -INT "$p" 2>/dev/null; done
    sleep 1
    for p in "${PROBE_PIDS[@]:-}"; do [ -n "${p:-}" ] && sudo kill -TERM "$p" 2>/dev/null; done
    PROBE_PIDS=()

    log "cleanup: stopping listener"
    [ -n "$LISTENER_PID" ] && kill "$LISTENER_PID" 2>/dev/null
    LISTENER_PID=""

    log "cleanup: stopping scope"
    systemctl --user stop "$SCOPE.scope" 2>/dev/null
    systemctl --user reset-failed "$SCOPE.scope" 2>/dev/null
    [ -n "$SCOPE_JOB" ] && wait "$SCOPE_JOB" 2>/dev/null

    if [ "$MOUNTED" -eq 1 ]; then
        log "cleanup: capturing result and unmounting"
        sudo mkdir -p "$RESULT_DIR"
        sudo cp -a "$SCRATCH/." "$RESULT_DIR/" 2>/dev/null
        sudo umount "$SCRATCH" 2>/dev/null && sudo rmdir "$SCRATCH" 2>/dev/null
        MOUNTED=0
    fi

    DONE=1
    log "cleanup: complete (exit rc=$rc)"
    exit "$rc"
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------- preconditions
log "preconditions: capture_id=$CAPTURE_ID"
pgrep -x bpftrace >/dev/null && fail "a bpftrace process is already running"
systemctl --user list-units --all 'mp-stage1-*' --no-legend 2>/dev/null | grep -q . \
    && fail "a stale mp-stage1 scope exists"
[ -e "$SCRATCH" ] && fail "scratch already exists: $SCRATCH"
ss -ltn 2>/dev/null | grep -q ":$PORT " && fail "port $PORT already in use"
avail_mb="$(free -m | awk 'NR==2 {print $7}')"
[ "${avail_mb:-0}" -ge 2000 ] || fail "only ${avail_mb:-0} MB available; need 2000"

# ---------------------------------------------------------------------- scratch
sudo mkdir -p "$SCRATCH" || fail "cannot create $SCRATCH"
sudo mount -t tmpfs -o size=256M,mode=0700 tmpfs "$SCRATCH" || fail "tmpfs mount failed"
MOUNTED=1
# The mount is created by root, so hand ownership to the invoking user before
# anything tries to read or write through it. Without this the runner cannot
# read its own identity file and every subsequent check fails on permissions.
sudo chown "$(id -u):$(id -g)" "$SCRATCH" || fail "cannot chown scratch"
sudo cp "$SHEET_DIR/workload.sh" "$SCRATCH/workload.sh" || fail "cannot stage workload"
sudo chmod 0755 "$SCRATCH/workload.sh"

# ------------------------------------------------------------------------- run
# The scope starts first and waits behind a gate, so the probes are attached
# before any action runs and the manifest can record the real cgroup id.
log "starting scope $SCOPE"
systemd-run --user --scope --collect --unit="$SCOPE" \
    --setenv=SCRATCH="$SCRATCH" --setenv=PORT="$PORT" --setenv=CLOSED_PORT="$CLOSED_PORT" \
    --property=MemoryMax=768M --property=CPUQuota=200% \
    -- bash -c 'timeout '"$WORKLOAD_LIMIT"' bash "$SCRATCH/workload.sh"' &
SCOPE_JOB=$!

for _ in $(seq 1 100); do [ -s "$SCRATCH/identity" ] && break; sleep 0.1; done
[ -s "$SCRATCH/identity" ] || fail "scope did not report identity"

CGROUP_ID="$(cut -d' ' -f1 "$SCRATCH/identity")"
CGROUP_PATH="$(cut -d' ' -f2 "$SCRATCH/identity")"
WORKLOAD_PID="$(cut -d' ' -f3 "$SCRATCH/identity")"
WORKLOAD_START="$(cut -d' ' -f4 "$SCRATCH/identity")"

cat >"$SCRATCH/run-manifest.json" <<JSON
{
  "capture_id": "$CAPTURE_ID",
  "boot_id": "$BOOT_ID",
  "host": "$(hostname)",
  "scope": "$SCOPE",
  "cgroup_id": "$CGROUP_ID",
  "cgroup_path": "$CGROUP_PATH",
  "workload_pid": "$WORKLOAD_PID",
  "workload_start_ticks": "$WORKLOAD_START",
  "probe_dir": "$PROBE_DIR",
  "port": "$PORT",
  "closed_port": "$CLOSED_PORT",
  "manifest_started": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
log "manifest written: cgroup_id=$CGROUP_ID pid=$WORKLOAD_PID"

# ---------------------------------------------------------------------- listener
python3 -m http.server "$PORT" --bind 127.0.0.1 >"$SCRATCH/listener.log" 2>&1 &
LISTENER_PID=$!
sleep 1
kill -0 "$LISTENER_PID" 2>/dev/null || fail "loopback listener failed to start"

# ------------------------------------------------------------------------ probes
log "attaching probes"
sudo bpftrace "$PROBE_DIR/exec.bt" -o "$SCRATCH/exec.log" 2>"$SCRATCH/exec.err" &
sudo bpftrace "$PROBE_DIR/file.bt" -o "$SCRATCH/file.log" 2>"$SCRATCH/file.err" &
sudo bpftrace "$PROBE_DIR/net.bt"  -o "$SCRATCH/net.log"  2>"$SCRATCH/net.err" &
sleep 3

mapfile -t PROBE_PIDS < <(pgrep -f "bpftrace $PROBE_DIR/" || true)
[ "${#PROBE_PIDS[@]}" -eq 3 ] || fail "expected 3 probes, found ${#PROBE_PIDS[@]}"
log "probe pids: ${PROBE_PIDS[*]}"

# --------------------------------------------------------------------- release
touch "$SCRATCH/go" || fail "cannot release the gate"
wait "$SCOPE_JOB"
SCOPE_JOB=""
log "workload finished"

# ------------------------------------------------------------------------ stop
for p in "${PROBE_PIDS[@]}"; do sudo kill -INT "$p" 2>/dev/null; done
sleep 2
for p in "${PROBE_PIDS[@]}"; do
    sudo kill -0 "$p" 2>/dev/null && fail "probe $p survived SIGINT"
done
PROBE_PIDS=()
kill "$LISTENER_PID" 2>/dev/null
LISTENER_PID=""

# ------------------------------------------------------------------ verify+exit
[ -s "$SCRATCH/workload.log" ] || fail "workload produced no log"
grep -q 'MARK workload phase=start' "$SCRATCH/workload.log" \
    || fail "workload never reached its start marker"
grep -q 'MARK workload phase=end' "$SCRATCH/workload.log" \
    || fail "workload never reached its end marker"

log "run complete; result will be captured at $RESULT_DIR"
exit 0
