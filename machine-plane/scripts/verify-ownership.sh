#!/usr/bin/env bash
# Acceptance checks 19-22: the ownership matrix, attempted rather than assumed.
#
# Run as root. Every check is an actual attempt that must be refused (or
# permitted) — reading the modes with `ls -l` proves what the bits say, not what
# the kernel does.
#
#   19. an account in mp-read only can read a sealed segment and cannot write,
#       truncate, rename or delete anything in the spool
#   20. the importer account cannot write the spool
#   21. the agent's account cannot read the spool at all
#   22. the importer never reads a .partial
#
# Writes a retained JSON artifact (default /var/lib/machine-plane/acceptance/) so
# the result is evidence rather than a transcript somebody has to remember.
set -uo pipefail

SPOOL="${SPOOL:-/var/lib/machine-plane/spool}"
IMPORTER="${IMPORTER:-mp-import}"
AGENT="${AGENT:?set AGENT to the account the agent runs as}"
STORE="${STORE:-/var/lib/machine-plane/store/events.ndjson}"
OUT_DIR="${OUT_DIR:-/var/lib/machine-plane/acceptance}"
JSON_OUT="${1:-$OUT_DIR/ownership-$(hostname -s)-$(date -u +%Y%m%dT%H%M%SZ).json}"

RESULTS="$(mktemp)"
trap 'rm -f "$RESULTS"' EXIT

failures=0

# record <check> <expected> <observed> <pass|fail>
record() {
    printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" >>"$RESULTS"
    if [ "$4" = "pass" ]; then
        printf '  %-52s PASS (%s)\n' "$1" "$2"
    else
        printf '  %-52s FAIL (expected %s, observed %s)\n' "$1" "$2" "$3"
        failures=$((failures + 1))
    fi
}

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }

segment="$(ls -1 "$SPOOL"/segment-*.ndjson 2>/dev/null | head -1)"
if [ -z "$segment" ]; then
    echo "no sealed segment in $SPOOL — run a capture first" >&2
    exit 2
fi

echo "=== 19/20: $IMPORTER (read-only, in mp-read) ==="

if sudo -u "$IMPORTER" head -c 200 "$segment" >/dev/null 2>&1; then
    record "read a sealed segment" "permitted" "permitted" pass
else
    record "read a sealed segment" "permitted" "denied" fail
fi

if sudo -u "$IMPORTER" sh -c 'printf x >> "$1"' _ "$segment" 2>/dev/null; then
    record "append to a sealed segment" "denied" "permitted" fail
else
    record "append to a sealed segment" "denied" "denied" pass
fi

if sudo -u "$IMPORTER" sh -c ': > "$1"' _ "$segment" 2>/dev/null; then
    record "truncate a sealed segment" "denied" "permitted" fail
else
    record "truncate a sealed segment" "denied" "denied" pass
fi

if sudo -u "$IMPORTER" mv "$segment" "$segment.moved" 2>/dev/null; then
    mv "$segment.moved" "$segment"
    record "rename a sealed segment" "denied" "permitted" fail
else
    record "rename a sealed segment" "denied" "denied" pass
fi

if sudo -u "$IMPORTER" rm -f "$segment" 2>/dev/null; then
    record "delete a sealed segment" "denied" "deleted" fail
else
    record "delete a sealed segment" "denied" "denied" pass
fi

if sudo -u "$IMPORTER" sh -c 'printf x > "$1/new-from-importer"' _ "$SPOOL" 2>/dev/null; then
    rm -f "$SPOOL/new-from-importer"
    record "create a file in the spool" "denied" "permitted" fail
else
    record "create a file in the spool" "denied" "denied" pass
fi

echo
echo "=== 21: $AGENT (the agent, not in mp-read) ==="

if sudo -u "$AGENT" head -c 200 "$segment" >/dev/null 2>&1; then
    record "read a sealed segment as the agent" "denied" "permitted" fail
else
    record "read a sealed segment as the agent" "denied" "denied" pass
fi

if sudo -u "$AGENT" ls "$SPOOL" >/dev/null 2>&1; then
    record "list the spool as the agent" "denied" "permitted" fail
else
    record "list the spool as the agent" "denied" "denied" pass
fi

echo
echo "=== 22: a .partial is never ingested ==="

partial="$SPOOL/segment-verify-000000-partial.ndjson.partial"
printf '{"spool_version":1,"recorder":"machine-plane-recorder","recorded_at":0,"event":{}}\n' >"$partial"
chgrp mp-read "$partial" 2>/dev/null || true
chmod 0640 "$partial"

before=$(wc -l <"$STORE" 2>/dev/null || echo 0)
# The property under test is that a .partial is never ingested, so the assertion
# is the store's line count. The importer's exit code is deliberately not part of
# it: a refused segment elsewhere in the spool makes a non-zero exit correct.
sudo -u "$IMPORTER" machine-plane-import --spool "$SPOOL" --store "$STORE" >/dev/null 2>&1
after=$(wc -l <"$STORE" 2>/dev/null || echo 0)
if [ "$before" = "$after" ]; then
    record "importer ignores a live .partial" "store unchanged" "unchanged" pass
else
    record "importer ignores a live .partial" "store unchanged" "grew" fail
fi
rm -f "$partial"

# -- retained artifact -------------------------------------------------------

mkdir -p "$(dirname "$JSON_OUT")" 2>/dev/null || true
python3 - "$RESULTS" "$JSON_OUT" "$SPOOL" "$IMPORTER" "$AGENT" "$segment" "$failures" <<'PY'
import json, os, pwd, sys, time

results_path, out_path, spool, importer, agent, segment, failures = sys.argv[1:8]


def identity(name):
    try:
        entry = pwd.getpwnam(name)
        return {"account": name, "uid": entry.pw_uid, "gid": entry.pw_gid}
    except KeyError:
        return {"account": name, "uid": None, "gid": None}


def mode_of(path):
    try:
        return oct(os.stat(path).st_mode & 0o7777)
    except OSError:
        return None


checks = []
with open(results_path, encoding="utf-8") as handle:
    for line in handle:
        check, expected, observed, status = line.rstrip("\n").split("\t")
        checks.append(
            {"check": check, "expected": expected, "observed": observed, "status": status}
        )

payload = {
    "artifact": "machine-plane-ownership-acceptance",
    "version": 1,
    "host": os.uname().nodename,
    "recorded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "spool": {"path": spool, "mode": mode_of(spool)},
    "segment": {"name": os.path.basename(segment), "mode": mode_of(segment)},
    "identities": {"importer": identity(importer), "agent": identity(agent)},
    "checks": checks,
    "failures": int(failures),
    "passed": int(failures) == 0,
}
with open(out_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
print(f"artifact : {out_path}")
PY

echo
if [ "$failures" -eq 0 ]; then
    echo "ownership: all checks passed"
else
    echo "ownership: $failures check(s) FAILED" >&2
fi
exit "$failures"
