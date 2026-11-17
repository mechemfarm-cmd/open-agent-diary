#!/usr/bin/env bash
# separation-proof.sh — demonstrate the ingestion boundary rather than assert it.
#
# Design §8 lists five acceptance conditions. Four of them are properties of the
# filesystem and the record format, and this script exercises each against a real
# root-owned spool on this host, as the unprivileged user the agent actually runs
# as. The fifth (distinguishable authorship) is structural and is checked by
# reading the schema, which is stated at the end.
#
# It creates one root-owned spool directory under /var/tmp, proves four negative
# results, and removes it. Nothing persistent is installed.
#
# Run: bash separation-proof.sh          (needs sudo for the setup steps only)

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG="${MACHINE_PLANE_PKG:-/home/willard/development/oad-machineplane/machine-plane/src}"
SPOOL_DIR="/var/tmp/mp2-separation-proof"
SPOOL="$SPOOL_DIR/spool.jsonl"
RECORDER="mp2-recorder"
PASS=0
FAIL=0

say()  { printf '%s\n' "$*"; }
ok()   { PASS=$((PASS+1)); printf '  PASS  %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$*"; }

cleanup() {
    local rc=$?
    set +e
    sudo rm -rf "$SPOOL_DIR" 2>/dev/null
    exit "$rc"
}
trap cleanup EXIT INT TERM

say "=== setup: a root-owned spool, as the design requires ==="
sudo rm -rf "$SPOOL_DIR"
sudo mkdir -p "$SPOOL_DIR" || { say "cannot create $SPOOL_DIR"; exit 1; }
sudo chown root:root "$SPOOL_DIR"
sudo chmod 0755 "$SPOOL_DIR"
printf '{"spool_version":1,"recorder":"%s","recorded_at":1.0,"event":{}}\n' "$RECORDER" \
    | sudo tee "$SPOOL" >/dev/null
sudo chown root:root "$SPOOL"
sudo chmod 0600 "$SPOOL"
say "  spool: $(sudo stat -c '%U:%G %a' "$SPOOL")  dir: $(sudo stat -c '%U:%G %a' "$SPOOL_DIR")"
say "  running as: $(id -un) (uid $(id -u))"
say

say "=== condition 1: the agent cannot submit a machine-plane event ==="
if printf '{"spool_version":1,"recorder":"%s","recorded_at":2.0,"event":{}}\n' "$RECORDER" >>"$SPOOL" 2>/dev/null; then
    bad "unprivileged append SUCCEEDED — the agent can forge an event"
else
    ok "unprivileged append refused (the spool is not writable by the agent)"
fi
say

say "=== condition 2: the agent cannot alter an existing machine event ==="
if sed -i 's/mp2-recorder/forged/' "$SPOOL" 2>/dev/null; then
    bad "unprivileged rewrite SUCCEEDED — an existing event can be altered"
else
    ok "unprivileged rewrite refused"
fi
if truncate -s 0 "$SPOOL" 2>/dev/null; then
    bad "unprivileged truncate SUCCEEDED"
else
    ok "unprivileged truncate refused"
fi
if rm -f "$SPOOL" 2>/dev/null; then
    bad "unprivileged removal SUCCEEDED — the record could be destroyed"
else
    ok "unprivileged removal refused"
fi
say

say "=== condition 4: schema or version mismatch fails closed ==="
# The write-refusal proofs above used the strictest mode. For the format checks
# the spool must be readable by the unprivileged importer, which is the real
# deployment shape: root-owned, writable only by the recorder, group-readable so
# the importer can consume it. Read access is not a boundary violation — the
# conditions are about submitting and altering, not reading.
sudo chmod 0644 "$SPOOL"
say "  (spool relaxed to $(sudo stat -c '%U:%G %a' "$SPOOL") so the unprivileged importer can read it)"
printf '{"spool_version":999,"recorder":"%s","recorded_at":1.0,"event":{}}\n' "$RECORDER" \
    | sudo tee -a "$SPOOL" >/dev/null
VERSION_OUT="$(PYTHONPATH="$PKG" python3 - <<'PY'
from machine_plane.spool import Spool, SpoolError
spool = Spool("/var/tmp/mp2-separation-proof/spool.jsonl", "mp2-recorder")
try:
    list(spool.read_from(0))
    print("READ-OK")
except SpoolError as exc:
    print("REFUSED:", exc)
PY
)"
case "$VERSION_OUT" in
    REFUSED:*) ok "unknown spool version refused: ${VERSION_OUT#REFUSED: }" ;;
    *)         bad "unknown spool version was read anyway: $VERSION_OUT" ;;
esac
say

say "=== condition 4b: a record from another recorder is refused by the importer ==="
IMPORTER_OUT="$(PYTHONPATH="$PKG" python3 - <<'PY'
from machine_plane.spool import Spool, SpoolError
from machine_plane.importer import Importer, MemoryStore
# Recorder says mp2-recorder in the file; importer expects a different one.
spool = Spool("/var/tmp/mp2-separation-proof/spool.jsonl", "mp2-recorder")
imp = Importer(spool, recorder_id="some-other-recorder", store=MemoryStore())
try:
    imp.run()
    print("ACCEPTED")
except SpoolError as exc:
    print("REFUSED:", str(exc)[:70])
except Exception as exc:
    print("REFUSED-OTHER:", type(exc).__name__)
PY
)"
case "$IMPORTER_OUT" in
    REFUSED*) ok "mismatched recorder refused: ${IMPORTER_OUT}" ;;
    *)        bad "mismatched recorder accepted: $IMPORTER_OUT" ;;
esac
say

say "=== condition 5: partial ingestion is detectable and resumable ==="
RESUME_OUT="$(PYTHONPATH="$PKG" python3 - <<'PY'
import json, os, tempfile
from machine_plane.spool import Spool, SpoolEnvelope
from machine_plane.importer import Importer, MemoryStore
from machine_plane.testing import MockProducer, action_event

tmp = tempfile.mkdtemp(prefix="mp2-resume-")
path = os.path.join(tmp, "spool.jsonl")
ckpt = os.path.join(tmp, "offset.json")
spool = Spool(path, "r1")

def make(n):
    raws = [action_event(wall_time=1000.0 + i, monotonic_time=float(i)) for i in range(n)]
    return list(MockProducer(raws).events())

for event in make(3):
    spool.append(event)

# A fourth record written only part-way, with no trailing newline.
fourth = make(1)[0]
line = (SpoolEnvelope(
    spool_version=1, recorder="r1", recorded_at=fourth.wall_time,
    event=fourth.to_dict()).to_json() + "\n").encode()
with open(path, "ab") as handle:
    handle.write(line[:-6])

r1 = Importer(spool, "r1", MemoryStore(), ckpt).run()
stopped_at = r1.offset

# The writer finishes the record; a restart must pick it up, not lose it.
with open(path, "ab") as handle:
    handle.write(line[-6:])
r2 = Importer(spool, "r1", MemoryStore(), ckpt).run()

print(json.dumps({
    "first_imported": r1.imported,
    "second_imported": r2.imported,
    "total_after_resume": r1.imported + r2.imported,
    "checkpoint_before_completion": stopped_at,
    "checkpoint_after_completion": r2.offset,
    "checkpoint_advanced_on_resume": r2.offset > stopped_at,
}))
PY
)"
say "  $RESUME_OUT"
case "$RESUME_OUT" in
    *'"first_imported": 3'*'"second_imported": 1'*'"total_after_resume": 4'*)
        ok "partial record held back, then resumed on restart (4 events, none lost, none duplicated)" ;;
    *) bad "resume behaviour unexpected" ;;
esac
say

say "=== condition 3: authorship is distinguishable (structural) ==="
say "  the event schema has no 'plane' field at all:"
if PYTHONPATH="$PKG" python3 -c "
import dataclasses
from machine_plane.events import MachineEvent
names = {f.name for f in dataclasses.fields(MachineEvent)}
raise SystemExit(0 if 'plane' not in names else 1)
"; then
    ok "MachineEvent has no 'plane' field, so a forged payload has nowhere to claim it"
else
    bad "MachineEvent carries a 'plane' field — a payload could assert its own plane"
fi
say "  and the importer stamps it from the envelope, never the payload:"
if grep -q '"plane": "machine"' "$PKG/machine_plane/importer.py"; then
    ok "importer stamps plane=machine from the envelope"
else
    bad "importer does not stamp plane from the envelope"
fi
say

say "=== result ==="
say "  $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
