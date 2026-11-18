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

Ordered, each step failing closed. **There are two attach operations and they are
not the same thing** — the document names them separately so it cannot be read as
attaching the production probes twice for no reason.

1. **Create the scope**, measure its cgroup inode, confirm it is a positive integer
   and that the control group reported by systemd resolves to the same inode.

2. **Disposable preflight attach.** One short-lived probe process, attached only to
   prove two things: the predicate compiles for the measured value, and the argument
   arrives. It is torn down before the run and produces no evidence.

3. **Confirm the argument is required.** A probe run with no positional argument
   must fail to attach, and the test asserts that failure rather than assuming it. A
   probe that silently defaulted to "no filter" is precisely the failure this stage
   exists to prevent.

4. **Final attach.** The real probe set, one process per probe script, three
   processes, which stay attached for the window. Each child's argv is captured and
   asserted to carry the same measured integer — the runtime half of the guarantee,
   where Gate 1 is the static half.

5. **Release the workload** only after steps 1–4 pass, including the argv assertion.

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
| a probe exits after its start marker but before release | **do not release**; void the run |
| a probe exits during the window | record, close the window, void the run |

**Teardown order** is stop probes, close the measurement window, then stop the
scope. Reversing it would let the workload keep running while nothing is listening,
and any action it took in that gap would be silently absent from a capture that
looks complete.

**No fallback to host-wide capture.** If filtering cannot be established, the run
does not happen. A capture that quietly widens is worse than no capture, because it
looks like the narrow one.

## 5. Event accounting

Two populations, because one cannot serve both purposes. Lifecycle and gap records
are the recorder's own bookkeeping and carry no workload identity **by design**, so
counting them as unattributable and then requiring zero would make a correctly
functioning run impossible. The earlier draft of this section did exactly that:
each counter was locally sensible and the acceptance criterion contradicted the
definition.

**Total population**

```
records = lifecycle + action + gaps
```

The three classes are **disjoint and exhaustive**. Every record belongs to exactly
one, and each class is defined by its event types rather than by description:

- `lifecycle` — **only** `recorder_started`, `recorder_stopped`, and the attachment
  markers. Recorder bookkeeping, no process identity, deliberately.
- `action` — `exec`, `open`, `read_attempt`, `write_attempt`, `create`, `rename`,
  `unlink`, `metadata_change`, `connect`. Events a probe observed.
- `gaps` — `buffer_overflow`, `permission_gap`, `unsupported_kernel_feature`,
  `sink_unavailable`, `clock_jump`, and any record that could not be read.

**Every coverage-loss event is a gap, never a lifecycle event.** An earlier draft of
this section listed "other gap-class events" under `lifecycle` while also defining a
`gaps` population, which would have counted buffer overflows, permission gaps, sink
failures and clock jumps in both — inflating `records` and destroying the equation's
meaning.

This matters because the core schema has a broader notion: `event_kind()` classifies
all seven of those types as `EventKind.GAP`. The accounting split is a **refinement**
of that classification, not the same thing, and the importer must apply this
partition rather than `event_kind()`. That difference is exactly where a drift would
go unnoticed.

**Attribution population — action records only**

```
action_records = in_scope_actions + out_of_scope_actions + unmatched_actions
```

| Counter | Definition |
|---|---|
| `in_scope_actions` | identity present and its isolation scope equals the named scope |
| `out_of_scope_actions` | identity present, scope differs |
| `unmatched_actions` | an action record with no identity or no isolation domain |

**Acceptance applies to action records only:**

```
out_of_scope_actions == 0
unmatched_actions    == 0
```

`out_of_scope_actions > 0` is a defect and voids the run — once the filter is
claimed to be narrow, an out-of-scope action is evidence the claim failed.

`unmatched_actions > 0` is also a finding: an action carrying no identity cannot
have come through a filter keyed on a scope, so its presence means something
reached the capture by a path not understood.

**Lifecycle and gap records are exempt, and that exemption is stated.** They are
counted, reported, and never required to be zero or to be attributable.

**What "unattributed" means.** When no scope is supplied at import, attribution is
unknown. The importer reports `unattributed` and never `in_scope_actions 0` — a
measured zero and an unknown are different claims, and printing the first for the
second is a lie about evidence.

**Reported separately**, in the recorder's terminal record and the import summary:

```
records, lifecycle, action, gaps,
in_scope_actions, out_of_scope_actions, unmatched_actions,
scope_supplied (true | false)
```

**One normative rule, three representations.** The scope test must be the same
decision everywhere, but it cannot literally be the same code: `bpftrace` cannot
call a Python function, so "define it once and call it three times" was an
overclaim in the previous draft.

The rule is therefore stated normatively, and each side implements it:

> An event is **in scope** if and only if its cgroup identity is present and its
> numeric value equals the measured scope cgroup inode.

| Representation | Form |
|---|---|
| `bpftrace` (kernel predicate) | `/cgroup == $1/`, `$1` a decimal integer |
| Python (producer re-check, importer accounting) | `is_in_scope(identity, scope_inode)` |

**The seam is tested, not assumed.** Gate 1 pins the predicate text to exactly
`/cgroup == $1/`. The Python helper's truth table is asserted over the cases that
decide agreement: equal, not equal, absent cgroup, absent identity, non-numeric.
And the two representations are reconciled behaviourally by the paired runs — the
filtered run's `in_scope_actions` must equal the control run's action records that
the Python rule classifies as in scope. If the predicate and the helper disagreed,
the two counts would diverge, which is a far stronger check than either in
isolation.

**Test requirement.** A fixture containing start and stop lifecycle records, at
least one **real** gap event (`buffer_overflow` or `clock_jump`, not a generic
placeholder), and at least one action record must satisfy the acceptance
conditions *and* must classify each record exactly once.

The disjointness is asserted directly: sum the three classes, compare to the record
count, and assert no record is claimed twice. A generic gap fixture would not have
caught the double count, because the double count came from the classification
rule, not from the data.

This is the test the previous two drafts would have failed — the first demanded zero
identity from records that cannot have one, and the second placed gap events in two
classes at once.

## 6. Privacy and Gate 1 implications

The predicate is inside the verifier's grammar, so Gate 1 must understand it.

**Grammar addition.** Exactly one filter form is approved:

```
/cgroup == $1/
```

Gate 1 accepts that form and rejects every other filter — including `/cgroup != $1/`,
a filter referencing any other variable, a filter on a content-bearing field, and a
filter with no predicate where the approved grammar now requires one.

**Both halves are required, and neither substitutes for the other.** Gate 1 proves
the source contains exactly the approved predicate. Runtime verification proves the
invocation carried the measured value and that all three processes reached their
start markers. Static checking cannot see a command line; a start marker cannot see
the source. The argv assertion in §2 step 4 is where the runtime half is tested.

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

**The workload declares its expected action manifest before the run**, not after
it. A fixed number of execs, opens and one connect attempt, written down as the
expected action count and the expected range. The control run is **not** the source
of that expectation — it is full of host-wide noise, so deriving an expectation from
it would mean asserting the capture matches the noise.

The manifest is what makes a filtered capture checkable at a glance: 1,200 actions
against a declared 1,180–1,240 is either right or visibly wrong. Without it, any
count looks plausible.

**The window:** 60 seconds, filter enabled, scope named, one run.

**Acceptance for the window:**

1. predicate compiled in preflight; argument confirmed required; three probes
   attached and every child's argv carries the same measured inode;
2. `out_of_scope_actions == 0`;
3. `unmatched_actions == 0`;
4. `in_scope_actions` inside the declared manifest range;
5. the accounting equation balances: `records == lifecycle + action + gaps`, and
   `action == in_scope_actions + out_of_scope_actions + unmatched_actions`;
6. Gate 2 passes on the sealed segment;
7. the control run's host-wide total is recorded separately and labelled, for
   comparison only;
8. after the run: no `bpftrace`, no scope, nothing enabled.

A failure at 1 voids the run before it starts. A failure at 2–5 voids it after, and
the void record preserves the offending count, the first and last occurrence, the
cgroup values seen, and the capture identity — so the failure is diagnosable without
anyone importing the run as valid evidence.

**The control run** is the same workload for the same duration with the filter
disabled. It is labelled as a control, and it is kept out of the workload-evidence
store. One control run is sufficient for this stage: it measures the narrowing
mechanism on this host, and it is a baseline rather than a permanent adequacy proof.

## 8. Non-goals

- **Not** chain or checkpoints. That is the next stage, and it follows this one.
- **Not** always-on, and **not** enabling the unit.
- **Not** observer mode, and **not** gateway integration.
- **Not** multiple simultaneous scopes. One run, one scope.
- **Not** content capture. Metadata only, unchanged.
- **Not** a change to the spool format, retention, or the containment gate.
- **Not** any second host.

## 9. Open questions, and what this does not claim

**Decisions taken during review of the first draft:**

1. **Static and runtime enforcement are both required.** Gate 1 proves the source
   contains the approved predicate; runtime verification proves the invocation
   carried the measured value and that three processes reached their markers.
   Neither substitutes for the other.
2. **Any out-of-scope action voids the run.** Not annotated, not averaged over.
   The void record keeps the count, first and last occurrence, cgroup values and
   capture identity.
3. **The producer re-check stays.** It is the independent seam check, and it shares
   one `is_in_scope` definition with the predicate and the importer so the three
   cannot drift.
4. **One unfiltered control run** for this stage; not one per filtered run.
5. **The expected action count is declared before the run**, never derived from the
   control run.
6. **Lifecycle and gap records are exempt from attribution** and reported
   separately (§5). The first draft required them to be unattributed *and* zero,
   which no correct run could satisfy.
7. **The three accounting classes are disjoint and exhaustive**, defined by event
   type: lifecycle is recorder start/stop and attachment markers only; every
   coverage-loss event is a gap. The second draft listed gap-class events under
   lifecycle as well, which would have counted them twice.
8. **The scope test is one normative rule with two representations** — a bpftrace
   predicate and a Python helper — reconciled by a seam test rather than by a
   shared function name. Saying "defined once and called three times" was wrong:
   bpftrace cannot call Python.

**Still open, and worth a view at implementation time:** whether a probe that exits
during the window should void the run or merely truncate it, and how the manifest
range should be widened when the workload changes.

**What this does not claim:**

- It does not claim the filter captures everything the workload did. A process that
  leaves the scope, or one whose event is attributed to a different cgroup, is not
  captured. Narrowing makes the capture *about* the workload; it does not make it
  *complete* about the workload.
- It does not claim host-wide capture becomes unavailable. It remains possible and
  remains labelled.
- It does not claim this makes the evidence tamper-evident. That is the chain, and
  it comes after this.
