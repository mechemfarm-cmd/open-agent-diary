# Machine plane — Stage 0 probes (Emily and Art)

Read-only. Nothing installed, no configuration changed. Every number came from
tools already present on the hosts.

Supersedes `machine-plane-stage0-emily.md`, which covered Emily alone and
reported the file-activity finding before it had been repeated.

## Environment

| Check | Emily | Art |
| --- | --- | --- |
| Kernel | 6.8.0-149-generic | 7.0.0-43-generic |
| Container | no | no |
| cgroup v2 | mounted | mounted |
| BTF | present | present |
| Kernel lockdown | `[none]` | `[none]` |
| `unprivileged_bpf_disabled` | 2 | 2 |
| `perf_event_paranoid` | 4 | 4 |
| bpftrace | installed | installed |
| strace | installed | installed |
| tetragon / falco / sysdig / auditd | none | none |
| Passwordless sudo | yes | yes |

**Conclusion:** both hosts are viable for a **privileged** eBPF probe today, with
zero installation. bpftrace already exists on both and the kernels have
everything CO-RE needs, so a sensor package would be an upgrade rather than a
prerequisite.

This is a statement about privileged feasibility, not about unprivileged attach.
Both hosts report `unprivileged_bpf_disabled=2` and `perf_event_paranoid=4`,
which is what those settings mean: a producer running as an ordinary process
cannot attach, and the probes below were run with sudo. Whether the recorder runs
privileged is therefore a Stage 0 question the probe does not answer, and the
privilege separation the design depends on is only achievable if it does.

## Exec activity

| Host | 8-second `sys_enter_execve` trace |
| --- | --- |
| Emily | 0 events on an idle gateway |
| Art | 0 events on an idle gateway |

An agent host waiting for work spawns almost nothing. Encouraging for
consequence-based filtering.

## File activity — and why the first reading was incomplete

| Host | 8-second `sys_enter_openat` trace |
| --- | --- |
| Emily | ~1,600 events |
| Art | 1 event |

Emily's breakdown (second measurement, reproducing the first):

```
hermes-change-w   1109
syncthing          281
systemd-udevd      118
(udev-worker)       72
hermes-lease-wa     16
upowerd             14
asyncio_3           13
```

The first reading measured Emily only and concluded that "the agent framework's
own infrastructure dominates file activity". That was too strong. Art runs the
same framework and is silent. Repeating the measurement on Emily confirmed the
load is persistent rather than a transient — the same watcher, ~1,100 opens in
8 seconds again — but the honest conclusion is narrower:

**The background profile is host-specific and workload-specific.** It depends on
which watchers are live, not on the agent framework as such. The magnitude
between two hosts running the same software differs by three orders of
magnitude.

Consequences for the design, which stand:

- A recorder scoped to the agent's process tree would exclude all of the above,
  which is the point of scoping.
- If scoping is ever loosened, the filter has to exclude infrastructure **by
  policy**, because there is no threshold that works on both hosts. A rule tuned
  to Emily's 1,600 events would discard everything Art ever does.
- Any per-host threshold is therefore wrong by construction. This is an argument
  for exclusion rules, not for rate limiting.

## Exec noise — one command, many events

A bounded trace on Emily while running `/usr/bin/env echo hi` recorded **16
execve events for that one command**: `env` walks `$PATH` and calls `execve` on
each candidate until one succeeds, so **15 were failed probes against binaries
that do not exist**.

Roughly half of all exec events in that session were failed lookups from one
command.

Consequence: **counting syscalls is not counting actions.** A recorder logging
every `execve` reports one command as a dozen effects, almost none of which
happened. The filter must key on outcome — whether the exec actually took — not
on the syscall appearing.

That consequence is now carried in the core rather than left to each producer.
`exec_outcome()` in `machine_plane.events` classifies an `EXEC` event as an
attempt or a completed transition, and an `EXEC` with no `result` is an attempt
by definition. A producer that emits syscall entries therefore produces attempts,
which is correct and useful; one that wants to claim a process actually started
has to record the result that proves it. Without this, a future producer could
emit every syscall entry as `EXEC` and still satisfy the schema, and the finding
above would quietly stop being enforced anywhere.

## Identity

Process names at the tracepoint are truncated to 15 characters
(`hermes-change-w`, `hermes-lease-wa`, `ThreadPoolForeg`). Two processes can
share a truncated name, and a process can change its own name.

This confirms the design's choice of `boot_id + pid + start_time` plus an
ancestry chain over any name-based identity. It is not a theoretical concern;
it was visible in the first trace.

## What this still does not answer

These are capability and noise measurements, not a sensor evaluation. Still
open, and still requiring a decision about installing something:

- whether AgentSight or Tetragon actually traces the agent's process tree
  end-to-end, with its own correlation rather than bpftrace's raw events;
- drop rates under sustained load, and storage/CPU cost over a full workday;
- behaviour for Python, Node, statically linked binaries, sudo and background
  jobs;
- whether a sensor's output can be exported without adopting its content-bearing
  schema.

Both hosts clear the capability bar. The sensor choice is now about fit, not
feasibility.
