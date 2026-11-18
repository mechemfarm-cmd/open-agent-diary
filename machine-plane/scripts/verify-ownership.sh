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
set -uo pipefail

SPOOL="${SPOOL:-/var/lib/machine-plane/spool}"
IMPORTER="${IMPORTER:-mp-import}"
AGENT="${AGENT:?set AGENT to the account the agent runs as}"
STORE="${STORE:-/var/lib/machine-plane/store/events.ndjson}"

failures=0
note() { printf '  %-52s %s\n' "$1" "$2"; }
pass() { note "$1" "PASS ($2)"; }
fail() { note "$1" "FAIL ($2)"; failures=$((failures + 1)); }

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }

segment="$(ls -1 "$SPOOL"/segment-*.ndjson 2>/dev/null | head -1)"
if [ -z "$segment" ]; then
    echo "no sealed segment in $SPOOL — run a capture first" >&2
    exit 2
fi

echo "=== 19/20: $IMPORTER (read-only, in mp-read) ==="

if sudo -u "$IMPORTER" head -c 200 "$segment" >/dev/null 2>&1; then
    pass "read a sealed segment" "allowed, as intended"
else
    fail "read a sealed segment" "denied — the importer cannot read its input"
fi

if sudo -u "$IMPORTER" sh -c 'printf x >> "$1"' _ "$segment" 2>/dev/null; then
    fail "append to a sealed segment" "WROTE — the boundary is broken"
else
    pass "append to a sealed segment" "denied"
fi

if sudo -u "$IMPORTER" sh -c ': > "$1"' _ "$segment" 2>/dev/null; then
    fail "truncate a sealed segment" "TRUNCATED — the boundary is broken"
else
    pass "truncate a sealed segment" "denied"
fi

if sudo -u "$IMPORTER" mv "$segment" "$segment.moved" 2>/dev/null; then
    mv "$segment.moved" "$segment"
    fail "rename a sealed segment" "RENAMED — the boundary is broken"
else
    pass "rename a sealed segment" "denied"
fi

if sudo -u "$IMPORTER" rm -f "$segment" 2>/dev/null; then
    fail "delete a sealed segment" "DELETED — the boundary is broken"
else
    pass "delete a sealed segment" "denied"
fi

if sudo -u "$IMPORTER" sh -c 'printf x > "$1/new-from-importer"' _ "$SPOOL" 2>/dev/null; then
    rm -f "$SPOOL/new-from-importer"
    fail "create a file in the spool" "CREATED — the boundary is broken"
else
    pass "create a file in the spool" "denied"
fi

echo
echo "=== 21: $AGENT (the agent, not in mp-read) ==="

if sudo -u "$AGENT" head -c 200 "$segment" >/dev/null 2>&1; then
    fail "read a sealed segment as the agent" "ALLOWED — the agent can read the spool"
else
    pass "read a sealed segment as the agent" "denied"
fi

if sudo -u "$AGENT" ls "$SPOOL" >/dev/null 2>&1; then
    fail "list the spool as the agent" "ALLOWED — the agent can enumerate it"
else
    pass "list the spool as the agent" "denied"
fi

echo
echo "=== 22: a .partial is never ingested ==="

partial="$SPOOL/segment-verify-000000-partial.ndjson.partial"
printf '{"spool_version":1,"recorder":"machine-plane-recorder","recorded_at":0,"event":{}}\n' >"$partial"
chgrp mp-read "$partial" 2>/dev/null || true
chmod 0640 "$partial"

before=$(wc -l <"$STORE" 2>/dev/null || echo 0)
if sudo -u "$IMPORTER" machine-plane-import --spool "$SPOOL" --store "$STORE" >/dev/null 2>&1; then
    after=$(wc -l <"$STORE" 2>/dev/null || echo 0)
    if [ "$before" = "$after" ]; then
        pass "importer ignores a live .partial" "store unchanged ($after records)"
    else
        fail "importer ignores a live .partial" "store grew $before -> $after"
    fi
else
    fail "importer ignores a live .partial" "import exited non-zero"
fi
rm -f "$partial"

echo
if [ "$failures" -eq 0 ]; then
    echo "ownership: all checks passed"
else
    echo "ownership: $failures check(s) FAILED" >&2
fi
exit "$failures"
