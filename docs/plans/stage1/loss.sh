#!/usr/bin/env bash
# loss.sh — Stage 1 loss session. Separate from the coverage session entirely.
#
# The question is not "did loss happen" but "was loss REPORTED". A tracer that
# overflows silently is the failure mode the whole design exists to prevent, so
# the signal is bpftrace's own lost-event count on stderr, checked against the
# window in which the workload was actually running.
#
# Separate capture id, separate scratch, separate scope. Induced loss must never
# be confused with attribution quality in the coverage run.

set -uo pipefail

SHEET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROBE_DIR="$SHEET_DIR/probes"
DURATION=60
RB_PAGES=2          # default is 64 per CPU on bpftrace 0.20.2 — 32x smaller here
TARGET_RATE=5000    # opens per second

CAPTURE_ID="mp1-loss-$(date -u +%Y%m%dT%H%M%SZ)-$(head -c3 /dev/urandom | od -An -tx1 | tr -d ' \n')"
SCOPE="mp-stage1-loss-${CAPTURE_ID}"
SCRATCH="/var/tmp/machine-plane-stage1/${CAPTURE_ID}"
RESULT_DIR="/home/willard/reviews/stage1-loss-${CAPTURE_ID}"

SCOPE_JOB=""
PROBE_PID=""
MOUNTED=0
DONE=0

log() { printf '%s\n' "$*" >&2; }
fail() { log "FATAL: $*"; exit 1; }

cleanup() {
    local rc=$?
    set +e
    [ "$DONE" -eq 1 ] && return 0
    [ -n "$PROBE_PID" ] && sudo kill -INT "$PROBE_PID" 2>/dev/null
    sleep 1
    [ -n "$PROBE_PID" ] && sudo kill -TERM "$PROBE_PID" 2>/dev/null
    PROBE_PID=""
    systemctl --user stop "$SCOPE.scope" 2>/dev/null
    systemctl --user reset-failed "$SCOPE.scope" 2>/dev/null
    [ -n "$SCOPE_JOB" ] && wait "$SCOPE_JOB" 2>/dev/null
    if [ "$MOUNTED" -eq 1 ]; then
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

log "loss session: capture_id=$CAPTURE_ID"

pgrep -x bpftrace >/dev/null && fail "a bpftrace process is already running"
systemctl --user list-units --all 'mp-stage1-*' --no-legend 2>/dev/null | grep -q . \
    && fail "a stale mp-stage1 scope exists"
[ -e "$SCRATCH" ] && fail "scratch already exists: $SCRATCH"
avail_mb="$(free -m | awk 'NR==2 {print $7}')"
[ "${avail_mb:-0}" -ge 2000 ] || fail "only ${avail_mb:-0} MB available; need 2000"

sudo mkdir -p "$SCRATCH" || fail "cannot create $SCRATCH"
sudo mount -t tmpfs -o size=256M,mode=0700 tmpfs "$SCRATCH" || fail "tmpfs mount failed"
MOUNTED=1
sudo chown "$(id -u):$(id -g)" "$SCRATCH" || fail "cannot chown scratch"

# The workload: a paced openat loop, so the rate is known rather than whatever
# the machine happens to manage. It waits behind a gate so the probe is attached
# before any of it runs.
cat >"$SCRATCH/churn.py" <<'PY'
import os, time
path = os.path.join(os.environ["SCRATCH"], "churn.txt")
gate = os.path.join(os.environ["SCRATCH"], "go")
for _ in range(300):
    if os.path.exists(gate):
        break
    time.sleep(0.1)
else:
    raise SystemExit("gate timeout")
n = 0
with open(os.path.join(os.environ["SCRATCH"], "rate.log"), "w") as rate:
    started = time.time()
    while True:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        os.close(fd)
        n += 1
        if n % 50 == 0:
            time.sleep(0.01)      # 50 opens per 10 ms ~= 5000/s
            if n % 5000 == 0:
                rate.write("%f %d\n" % (time.time(), n))
                rate.flush()
PY

log "starting scope $SCOPE"
systemd-run --user --scope --collect --unit="$SCOPE" \
    --setenv=SCRATCH="$SCRATCH" --expand-environment=yes \
    --property=MemoryMax=768M --property=CPUQuota=200% \
    -- bash -c 'timeout '"$((DURATION + 5))"' python3 "$SCRATCH/churn.py"' &
SCOPE_JOB=$!

for _ in $(seq 1 100); do [ -d "$SCRATCH" ] && break; sleep 0.1; done

log "attaching file probe with BPFTRACE_PERF_RB_PAGES=$RB_PAGES"
sudo env BPFTRACE_PERF_RB_PAGES="$RB_PAGES" \
    bpftrace "$PROBE_DIR/file.bt" -o "$SCRATCH/loss.log" 2>"$SCRATCH/loss.err" &
sleep 3
mapfile -t PIDS < <(pgrep -f "bpftrace $PROBE_DIR/file.bt" || true)
[ "${#PIDS[@]}" -eq 1 ] || fail "expected 1 probe, found ${#PIDS[@]}"
PROBE_PID="${PIDS[0]}"
log "probe pid: $PROBE_PID"

touch "$SCRATCH/go" || fail "cannot release the gate"
log "running for ${DURATION}s"
timeout "$DURATION" tail -f /dev/null 2>/dev/null || true

sudo kill -INT "$PROBE_PID" 2>/dev/null
sleep 2
sudo kill -0 "$PROBE_PID" 2>/dev/null && fail "probe survived SIGINT"
PROBE_PID=""

systemctl --user stop "$SCOPE.scope" 2>/dev/null
systemctl --user reset-failed "$SCOPE.scope" 2>/dev/null
wait "$SCOPE_JOB" 2>/dev/null
SCOPE_JOB=""

[ -s "$SCRATCH/loss.err" ] || fail "probe produced no stderr"

log "loss session complete; stderr captured at $RESULT_DIR"
exit 0
