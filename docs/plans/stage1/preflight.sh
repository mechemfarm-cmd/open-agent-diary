#!/usr/bin/env bash
# preflight.sh — disposable check that bpftrace's `cgroup` builtin really does
# equal the cgroup directory's inode on this host.
#
# Why this exists: the attribution measurement depends entirely on that equality.
# It is asserted in the design docs, but a design doc is not a measurement. If the
# value bpftrace emits does not match the inode recorded for the same process,
# the join key is wrong and the whole run would be meaningless — so this is
# checked first, separately, and a mismatch voids the run rather than falling
# back to process names or timestamps.
#
# Cost: about ten seconds, one transient scope, one short-lived attachment.
# It writes only to a temporary directory that it removes on exit.

set -euo pipefail

PF_ID="mp1-preflight-$(date -u +%Y%m%dT%H%M%SZ)"
PF_DIR="$(mktemp -d /var/tmp/mp1-preflight.XXXXXX)"
SCOPE_PID=""

log() { printf '%s\n' "$*" >&2; }

cleanup() {
    local rc=$?
    set +e
    [ -n "$SCOPE_PID" ] && kill "$SCOPE_PID" 2>/dev/null
    systemctl --user stop "$PF_ID.scope" 2>/dev/null
    systemctl --user reset-failed "$PF_ID.scope" 2>/dev/null
    rm -rf "$PF_DIR"
    exit "$rc"
}
trap cleanup EXIT INT TERM

# The scope records its own cgroup inode, then waits, then makes exactly one
# observable syscall. The wait is what lets the probe attach before the trigger.
#
# The trigger must run in THIS shell process, not a child: the probe filters on
# the recorded PID, and an external command like `cat` would fork and open the
# file under a different PID, so the filter could never match. A redirection with
# exec is performed by bash itself.
systemd-run --user --scope --collect --unit="$PF_ID" --setenv=PF_DIR="$PF_DIR" --expand-environment=yes -- \
    bash -c 'printf "%s\n" "$$" >"$PF_DIR/pid";
             stat -c "%i" "/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup)" >"$PF_DIR/inode";
             sleep 6;
             exec 3</dev/null;
             sleep 1' &
SCOPE_PID=$!

sleep 1
[ -s "$PF_DIR/pid" ] || { log "PREFLIGHT FAILED: scope did not write its pid"; exit 2; }
[ -s "$PF_DIR/inode" ] || { log "PREFLIGHT FAILED: scope did not write its cgroup inode"; exit 2; }

TARGET_PID="$(cat "$PF_DIR/pid")"
RECORDED="$(cat "$PF_DIR/inode")"
log "PREFLIGHT target pid=$TARGET_PID recorded_cgroup_id=$RECORDED"

# Attach before the trigger fires at t+6s. -d was used at review time to prove
# this one-liner compiles; here it is actually attached, briefly.
sudo timeout 9 bpftrace -e '
tracepoint:syscalls:sys_enter_openat /pid == $1/ {
    printf("%llu\n", cgroup);
    exit();
}' "$TARGET_PID" >"$PF_DIR/observed" 2>"$PF_DIR/observed.err" || true

OBSERVED="$(tail -1 "$PF_DIR/observed" 2>/dev/null || true)"

if [ -z "$OBSERVED" ]; then
    log "PREFLIGHT FAILED: probe emitted nothing"
    sed 's/^/  /' "$PF_DIR/observed.err" >&2
    exit 3
fi

if [ "$OBSERVED" != "$RECORDED" ]; then
    log "PREFLIGHT FAILED: mismatch — recorded=$RECORDED observed=$OBSERVED"
    log "The cgroup join key does not hold on this host. The run must be void."
    exit 4
fi

log "PREFLIGHT OK: bpftrace cgroup=$OBSERVED matches recorded inode=$RECORDED"
exit 0
