#!/bin/sh
# Stage 3 bounded workload: a named, repeated, inspectable action set.
#
# Deliberately small and deterministic, because the point of the run is to prove
# the recorder path — scope, join key, attach, seal — not to measure the machine.
# It produces execs, file opens and a connect attempt, which are the three
# classes the probes observe.
set -u

for _ in $(seq 1 120); do
    /bin/true
    /bin/cat /etc/hostname >/dev/null 2>&1
done

# One connect that is expected to fail: the probe records the attempt, and the
# outcome is a declared gap rather than a guess.
python3 - <<'PY' 2>/dev/null || true
import socket

sock = socket.socket()
sock.settimeout(0.5)
try:
    sock.connect(("127.0.0.1", 9))
except OSError:
    pass
sock.close()
PY
