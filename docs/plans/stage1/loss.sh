#!/usr/bin/env bash
# loss.sh — Stage 1 loss session. Separate from the coverage session entirely.
#
# The question is not "did loss happen" but "was loss REPORTED". A tracer that
# overflows silently is the failure mode the whole design exists to prevent, so
# the signal is bpftrace's own lost-event count on stderr.
#
# Three things this script refuses to do:
#   - run past a probe that died early, treating a dead probe as a quiet one;
#   - count a session where the churn workload never started;
#   - call any non-empty stderr "loss".
#
# SCOPE OF THE MEASUREMENT: the probe is deliberately UNFILTERED. It measures
# openat pressure across the whole host, not only inside the churn scope. Emily's
# own gateway and watchers contribute to the load. The result is therefore
# host-wide buffer pressure, and must be reported that way — not as loss
# attributable solely to the churn workload.

set -uo pipefail

SHEET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROBE_DIR="$SHEET_DIR/probes"
DURATION=60
RB_PAGES=2          # default is 64 per CPU on bpftrace 0.20.2 — 32x smaller here

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

# ------------------------------------------------------------------ workload
# Paced openat loop. It announces itself after the gate opens, and appends a
# running count, so the session can prove it actually did work.
cat >"$SCRATCH/churn.py" <<'PY'
import os, time
scratch = os.environ["SCRATCH"]
path = os.path.join(scratch, "churn.txt")
gate = os.path.join(scratch, "go")
for _ in range(300):
    if os.path.exists(gate):
        break
    time.sleep(0.1)
else:
    raise SystemExit("gate timeout")

# Proof that the workload reached the gate, distinct from "the file exists".
with open(os.path.join(scratch, "churn.ready"), "w") as ready:
    ready.write("ready %.6f\n" % time.time())

n = 0
started = time.time()
with open(os.path.join(scratch, "rate.log"), "w") as rate:
    while True:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        os.close(fd)
        n += 1
        if n % 50 == 0:
            time.sleep(0.01)      # 50 opens per 10 ms ~= 5000/s
        if n % 1000 == 0:
            rate.write("%.6f %d\n" % (time.time() - started, n))
            rate.flush()
PY

log "starting scope $SCOPE"
systemd-run --user --scope --collect --unit="$SCOPE" \
    --setenv=SCRATCH="$SCRATCH" --expand-environment=yes \
    --property=MemoryMax=768M --property=CPUQuota=200% \
    -- bash -c 'timeout '"$((DURATION + 10))"' python3 "$SCRATCH/churn.py"' &
SCOPE_JOB=$!

# --------------------------------------------------------------------- probe
log "attaching file probe with BPFTRACE_PERF_RB_PAGES=$RB_PAGES (unfiltered: host-wide)"
sudo env BPFTRACE_PERF_RB_PAGES="$RB_PAGES" \
    bpftrace "$PROBE_DIR/file.bt" -o "$SCRATCH/loss.log" 2>"$SCRATCH/loss.err" &
sleep 3
mapfile -t PIDS < <(pgrep -f "bpftrace $PROBE_DIR/file.bt" || true)
[ "${#PIDS[@]}" -eq 1 ] || fail "expected 1 probe, found ${#PIDS[@]}"
PROBE_PID="${PIDS[0]}"
log "probe pid: $PROBE_PID"

touch "$SCRATCH/go" || fail "cannot release the gate"

# ------------------------------------------------------------------- window
# Poll rather than sleep once, so a probe that dies at second 5 is noticed at
# second 5 rather than at second 60.
STARTED="$(date +%s)"
PROBE_EXITED_EARLY=0
EXITED_AT=""
ELAPSED=0
while :; do
    ELAPSED=$(( $(date +%s) - STARTED ))
    if [ "$ELAPSED" -ge "$DURATION" ]; then break; fi
    if ! sudo kill -0 "$PROBE_PID" 2>/dev/null; then
        PROBE_EXITED_EARLY=1
        EXITED_AT="$ELAPSED"
        log "probe exited early at ${ELAPSED}s"
        break
    fi
    sleep 1
done

sudo kill -INT "$PROBE_PID" 2>/dev/null
sleep 2
if sudo kill -0 "$PROBE_PID" 2>/dev/null; then fail "probe survived SIGINT"; fi
PROBE_PID=""

systemctl --user stop "$SCOPE.scope" 2>/dev/null
systemctl --user reset-failed "$SCOPE.scope" 2>/dev/null
wait "$SCOPE_JOB" 2>/dev/null
SCOPE_JOB=""

# ------------------------------------------------------------------ classify
CHURN_READY=0
[ -s "$SCRATCH/churn.ready" ] && CHURN_READY=1

TOTAL_OPS=0
if [ -s "$SCRATCH/rate.log" ]; then
    TOTAL_OPS="$(awk 'END {print ($2 == "" ? 0 : $2)}' "$SCRATCH/rate.log")"
fi

# bpftrace reports loss in its own words on stderr. Classify rather than assume:
# "non-empty stderr" is not evidence of loss, and an error is not loss either.
LOSS_CLASS="no_loss_reported"
LOSS_COUNT=""
LOSS_LINE=""
if [ -s "$SCRATCH/loss.err" ]; then
    LOSS_LINE="$(grep -iE 'lost' "$SCRATCH/loss.err" | head -1 || true)"
    if [ -n "$LOSS_LINE" ]; then
        LOSS_COUNT="$(printf '%s' "$LOSS_LINE" | grep -oE '[0-9]+' | head -1 || true)"
        if [ -n "$LOSS_COUNT" ]; then
            LOSS_CLASS="loss_reported"
        else
            # Mentions loss but not in a form this script recognises. Saying
            # "no loss" here would be inventing a result, so say what happened.
            LOSS_CLASS="loss_mentioned_unparsed"
        fi
    elif grep -qiE 'error|failed|invalid' "$SCRATCH/loss.err"; then
        LOSS_CLASS="probe_error"
    fi
fi

cat >"$SCRATCH/session.json" <<JSON
{
  "capture_id": "$CAPTURE_ID",
  "kind": "loss",
  "scoped_to": "host-wide — probe is unfiltered; Emily's own activity contributes",
  "rb_pages": $RB_PAGES,
  "planned_duration_s": $DURATION,
  "observed_duration_s": $ELAPSED,
  "probe_exited_early": $([ "$PROBE_EXITED_EARLY" -eq 1 ] && echo true || echo false),
  "probe_exited_at_s": $([ -n "$EXITED_AT" ] && echo "$EXITED_AT" || echo null),
  "churn_ready": $([ "$CHURN_READY" -eq 1 ] && echo true || echo false),
  "churn_total_ops": $TOTAL_OPS,
  "loss_class": "$LOSS_CLASS",
  "loss_count": $([ -n "$LOSS_COUNT" ] && echo "$LOSS_COUNT" || echo null)
}
JSON

log "session.json:"
sed 's/^/  /' "$SCRATCH/session.json" >&2

# ------------------------------------------------------------------- verdict
[ "$CHURN_READY" -eq 1 ] || fail "churn workload never reached the gate — nothing was measured"
[ "$TOTAL_OPS" -gt 0 ] || fail "churn workload recorded zero operations — nothing was measured"
[ "$PROBE_EXITED_EARLY" -eq 0 ] || fail "probe exited early at ${EXITED_AT}s; the 60s window was not observed"

log "loss session complete; result at $RESULT_DIR"
exit 0
