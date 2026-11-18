# Machine plane — Probe filtering and workload attribution

**Status:** proposal for review. Nothing here has been built or run.
**Predecessors:** Stage 3 approved as bounded recorder plumbing at `486fd2e`;
merged to `main` at `5c56236`.
**Target host:** the reference host.
**Author:** the implementer. Written for review, then the operator's authorization.

---

## 0. Plain language

The recorder currently watches the whole machine and records everything it sees.
That is why a 15-second capture of a workload doing a few hundred operations
produced 62,720 records, of which 1,261 belonged to the workload. The rest is the
machine going about its business.

This proposal narrows the sensor to one scope: the box the workload runs in. After
it, a capture of that workload should contain approximately the workload and
almost nothing else.

The important part is not the narrowing. It is that **the narrowing is proven**,
and that a capture which fails to narrow is refused rather than silently reported
as if it had worked.

## 1. The join key, and the filter mechanism

**The key is the cgroup inode.** Verified directly on this host: a workload inside
a scope reports `cgroup=385715` from `bpftrace`, and the scope's directory inode is
`385715`. They are the same number, and the approved supervisor already measures it
before attaching.

**The mechanism is a bpftrace predicate with a positional argument.**

Each `EV`-emitting block gains exactly one predicate:

```
tracepoint:sched:sched_process_exec
/cgroup == $1/
{
    printf("EV exec_started pid=%d tid=%d cgroup=%llu ts=%llu path=%s\n", ...);
}
```

and the recorder passes the measured inode as the single argument:

```
bpftrace /usr/lib/machine-plane/probes/exec.bt 385715
```

Verified on this host: `bpftrace -e 'BEGIN { printf("%d\n", $1); exit(); }' 12345`
prints `12345`, and `/cgroup == <value>/` compiles as a predicate. So the probe
*source* stays fixed and reviewed; only the argument varies per run. No generated
scripts, and no template that Gate 1 would have to reason about.

**Why `$1` and not the alternatives.**

- *Generating a script per run* makes every run's artifact a new, unreviewed file
  that Gate 1 has to verify as an instance of a template. More moving parts for the
  same effect.
- *Filtering in the producer only* works, but pays the whole cost before discarding
  the result: the kernel writes, the recorder reads, the spool is never touched.
  It also means every out-of-scope event is read by the recorder, which is a larger
  privacy surface for the same outcome.
- *`BEGIN`/`END` blocks are not filtered*, deliberately: their markers are how
  attachment readiness is signalled, and an attachment signal that depends on the
  filter working would be circular.

## 2. Attach and preflight

Ordered, each step failing closed:

1. **Create the scope**, measure its cgroup inode, confirm it is a positive integer
   and that `/sys/fs/cgroup` + the reported control group resolves to it.
2. **Verify the predicate compiles** for the measured value before any workload
   exists: attach the probe set with the real argument and require a start marker.
3. **Confirm the argument was received**: a probe run without a positional argument
   must fail to attach. Tested as a precondition, not assumed — a probe that
   silently defaulted to "no filter" is the failure this stage exists to prevent.
4. **Attach the real probe set**, one process per probe script, require exactly
   three markers.
5. **Release the workload** only after steps 1–4 pass.

## 3. Release-gate ordering

The existing attach gate is extended, not replaced:

```
scope created -> cgroup measured -> filter verified -> probes attached (3 markers)
   -> filter liveness checked -> workload released -> window clock starts
```

The window clock starts at release, as it does now. What changes is that release
now waits for filter verification as well as attachment, so a run can never begin
with probes listening but unfiltered.

## 4. Fail-closed behaviour

| Condition | Behaviour |
|---|---|
| cgroup inode cannot be measured | refuse; no scope, no attach |
| predicate fails to compile for the value | refuse; run voided, exit non-zero |
| any probe fails to attach | refuse; run voided |
| a probe attaches without receiving the argument | **refuse** — this is the silent-widening case |
| out-of-scope events appear in the capture | run continues, recorded as a defect, report marked void |
| filter verification times out | refuse; run voided |

**No fallback to host-wide capture.** If filtering cannot be established, the run
does not happen. A capture that quietly widens is worse than no capture, because it
looks like the narrow one.

## 5. Event accounting

Four counters, separately named, replacing today's matched/unmatched pair:

| Counter | Definition |
|---|---|
| `in_scope` | identity present and its isolation scope equals the named scope |
| `out_of_scope` | identity present, scope differs — **must be 0 when the filter works** |
| `unmatched` | no identity, or no isolation domain — cannot be attributed either way |
| `total` | all records read, including lifecycle |

`in_scope + out_of_scope + unmatched == total`, checked at import and in the
recorder's terminal record.

**Two consequences, both of which are the point.**

- `out_of_scope > 0` is a **defect**, not noise. With the filter attached and
  verified it should be impossible, so any non-zero value means the filter did not
  do what it claimed. The importer reports it and the run is marked void.
- **"No scope supplied" is reported as `unattributed`, never as zero matches.** A
  capture imported without a scope name has an unknown attribution, not a measured
  absence of matching events. Those are different claims and were previously
  conflated by printing `matched 0`.

A **control run** — the same workload, same window, filter disabled and clearly
labelled as such — is required once, so the narrowing is measured rather than
asserted: it establishes the host-wide volume that filtering is claimed to remove.

## 6. Privacy and Gate 1 implications

The predicate is inside the verifier's grammar, so Gate 1 must understand it.

**Grammar addition.** Exactly one filter form is approved:

```
/cgroup == $1/
```

Gate 1 accepts that form and rejects every other filter — including `/cgroup != $1/`,
a filter referencing any other variable, a filter on a content-bearing field, and a
filter with no predicate where the approved grammar now requires one.

**Why the verifier must require it, not merely allow it.** A probe with the
predicate omitted still attaches and still passes today's Gate 1. That is the same
bypass shape as the last three: the guard checks what is present and never asks
what is missing. The grammar must therefore state that the predicate is required on
every `EV` block, and fail a probe without it.

**The approved dereference is unchanged** but must be re-verified in the filtered
form: `tracepoint:sched:sched_process_exec` → `str(args->filename)` stays the only
pointer read, and adding a predicate must not alter that verdict.

**Producer-side re-check.** The producer re-tests each event's cgroup against the
scope map and counts `out_of_scope` rather than trusting the kernel predicate. This
is the seam check: it costs one comparison and turns a silent filter failure into a
reported number.

## 7. One bounded workload, one validation window

Supervisor mode only, one named workload, one window, as Stage 3.

**The workload** must produce a known, countable action set — a fixed number of
execs, opens and one connect attempt — so the expected record count is predictable
within a range, and a capture that is wildly outside it is visible without
interpretation. The Stage 3 workload is close to this already; it needs a stated
expected count rather than a script.

**The window:** 60 seconds, filter enabled, scope named, one run.

**Acceptance for the window:**

1. filter compiled and verified before release; three probes attached;
2. `out_of_scope == 0` and `unmatched == 0`;
3. `in_scope` within the stated expected range for the workload;
4. Gate 2 passes on the sealed segment;
5. the control run's host-wide total, recorded separately for comparison;
6. the importer reports the four counters and reconciles them;
7. after the run: no `bpftrace`, no scope, nothing enabled.

A failure at 1 voids the run before it starts. A failure at 2 voids it after.

## 8. Non-goals

- **Not** chain or checkpoints. That is the next stage, and it follows this one.
- **Not** always-on, and **not** enabling the unit.
- **Not** observer mode, and **not** gateway integration.
- **Not** multiple simultaneous scopes. One run, one scope.
- **Not** content capture. Metadata only, unchanged.
- **Not** a change to the spool format, retention, or the containment gate.
- **Not** any second host.

## 9. Open questions, and what this does not claim

**Questions for review:**

1. Is requiring the predicate on every `EV` block the right enforcement, or should
   the recorder refuse to attach a probe whose filter cannot be verified at
   runtime — or both?
2. Should `out_of_scope > 0` void the run outright, or record a defect and let the
   operator decide? I lean towards voiding: a filter that partially works is not a
   filter, and the numbers downstream cannot be trusted.
3. Is one control run sufficient to establish the narrowing, or should every
   filtered run be paired with one?
4. The expected-count range for the workload: derive it from a control run, or
   declare it up front and treat a mismatch as a finding?

**What this does not claim:**

- It does not claim the filter captures everything the workload did. A process that
  leaves the scope, or one whose event is attributed to a different cgroup, is not
  captured. Narrowing makes the capture *about* the workload; it does not make it
  *complete* about the workload.
- It does not claim host-wide capture becomes unavailable. It remains possible and
  remains labelled.
- It does not claim this makes the evidence tamper-evident. That is the chain, and
  it comes after this.
