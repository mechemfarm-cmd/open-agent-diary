# Agent Diary — Machine Plane

**Status:** design sketch. Nothing is built. Written to be argued with.

**One-line idea:** Agent Diary records what an agent *says* it did. This adds an
independent record of what the machine *actually did*, and joins the two.

---

## 1. The idea in plain language

Right now Agent Diary keeps a work trace: the agent called this tool, ran that
command, edited this file. That is the agent's own account of itself.

This proposal adds a second account, taken from the operating system rather than
from the agent. The kernel does not care what the agent intended; it only records
what actually happened — this process executed, this file was opened for writing,
this connection was made.

Two accounts of the same events. When they agree, you have a history you can
trust. When they disagree, you have the most useful signal in agent safety:
**the agent's account and the machine's account do not match.**

The mismatch is the product. Everything else is plumbing.

## 2. Mental image

A flight recorder for a machine that runs agents.

Every consequential action gets one case number. The agent's side says "I am
about to write to this file, because you asked me to." The machine's side says
"process 4821 opened that file for writing at 14:03." The recorder binds them
into a single record: who asked, what was authorised, what actually happened,
and whether those agree.

## 3. What already exists — do not rebuild these

The technology is largely solved. The gap is packaging, comprehension, and
privilege. Anything built here should be a consumer of these, not a replacement.

**Agent plane (Layer 1) — already ours.** Agent Diary work traces: tool calls,
terminal commands, file edits, already being written daily.

**Machine plane sensors (Layer 2):**
- `eunomia-bpf/agentsight` — open source, arXiv:2508.02736. Hooks eBPF at two
  boundaries: an *intent stream* (uprobes on `SSL_read`/`SSL_write`, capturing
  decrypted model traffic without agent cooperation or a proxy) and an *action
  stream* (tracepoints for process and file activity). This is the closest
  existing work to the correlation idea here.
- Tetragon (Cilium), Falco (CNCF), Tracee (Aqua), auditd, Sysdig.

**Integrity (Layer 3):** immudb (Merkle-backed, clients verify server responses),
Trillian (transparency logs), hash chaining, signed checkpoints.

**Kill switch prior art:** OWASP AISVS C09-06 (shutdown and graceful degradation),
CrowdStrike Falcon Guardian (endpoint agent termination, November 2026), and
Stanford Law's March 2026 argument that *kill switches don't work if the agent
writes the policy*.

**Therefore:** the deliverable is not a sensor. It is a record format, a
correlation rule, a privilege model, and a view a non-technical person can read.

## 4. Architecture

Three producers, one record format, one view.

```
  agent plane        ──┐
  (work traces)        │
                       ├──►  correlated record  ──►  hash-chained log  ──►  dashboard
  machine plane      ──┤          (one file per    (append-only,        (timeline +
  (eBPF sensor)        │           run/session)     off-box checkpoint)  divergence flags
                       │
  approval plane     ──┘
  (consent decisions)
```

The machine plane is a **separate, optional producer** that writes into Agent
Diary. It is not a limb of Agent Diary itself — see §8.

## 5. The record

One event, one row. Fields chosen so a human can read it and a machine can
verify it:

- `event_id` — stable, content-derived
- `run_id` — the correlation key; one per agent run/session
- `plane` — `agent` | `machine` | `approval`
- `actor` — which agent, which process, which user
- `intent` — what the agent said it was doing (agent plane only; empty otherwise)
- `action` — exec / open-write / unlink / connect / chmod / mount
- `target` — process name or **file path** (never file contents)
- `result` — ok / denied / failed
- `prev_hash`, `hash` — the chain link

## 6. Correlation — the hard part

Nothing joins these layers for free. A kernel event says `pid 4821 wrote /etc/x`;
an agent trace says `write_file(path="/etc/x")`. Proposed join:

1. **run_id** propagated from the agent into the environment of everything it
   spawns, so every child process inherits it.
2. **pid + process ancestry** to tie a kernel event back to that run.
3. **time window** as a tiebreaker, never as the primary key.

Where the join fails, the record says `unmatched` rather than guessing. An
honest gap is more useful than a confident wrong answer.

## 7. Privacy model — the differentiator

Every comparable tool is built for enterprise compliance and logs everything by
default. Agent Diary's users run it on the machine where they live. So:

- **Metadata only.** The machine plane records shapes, not contents: process
  names, file paths, syscall types, timestamps. Never file bytes, never command
  arguments, never payloads.
- **Filenames, not contents.** A path tells you what the agent touched. Contents
  are what would leak a life.
- **Content is opt-in per source**, exactly as the existing consent model works,
  and revocation stays terminal.
- **Exclusions by default:** credentials, keys, browser data, caches.

This is the thing nobody else has: watch everything the agent does, without
recording the person's life. It is what makes "record everything" defensible.

## 8. Privilege model — the load-bearing decision

If the agent and the recorder run with the same privileges, none of this is
evidence. A prompt-injected agent can stop the recorder, edit the log, or ignore
the kill switch. CrowdStrike solves that with a kernel driver and enterprise
policy; a personal install cannot.

The only honest answer at personal scale is **privilege separation**:

- the agent runs unprivileged, as an ordinary user
- the recorder runs above it, in its own domain
- the agent has no path to stop, edit, or signal the recorder

Until that holds, "records everything" is a claim, not a guarantee. This is worth
more design effort than the dashboard.

## 9. Kill switch

Layered, and out-of-band. If the agent can reach the switch, it is not a switch.

1. **Pause** — the agent stops taking new work; state preserved.
2. **Freeze** — its process tree is stopped.
3. **Isolate** — network access revoked.
4. **Stop** — the run ends.

Requirements: reachable by a human on a channel the agent does not control; the
agent cannot write its own policy; the path is exercised often enough to know it
works. An untested kill switch is a decoration.

## 10. Lightness budget

"Light as possible" and "records everything" are in direct tension. Full syscall
capture is gigabytes an hour. The resolution is filtering by **consequence**, not
volume:

- scope to the agent's process tree (cgroup or ancestry)
- capture only consequential syscalls: exec, open-for-write, unlink, connect,
  chmod, mount
- no payload capture, no content
- ring buffer to userspace, no full-fidelity mode by default

Target: thousands of events a day, not millions. If the recorder is heavy enough
that people turn it off, it has failed.

## 11. Dashboard

One page, readable by someone who is not a sysadmin:

- **Timeline** — what the agent did, in order, in plain sentences
- **Divergence** — actions the machine saw that the agent never claimed, and
  claims the machine never saw. This is the headline.
- **Authority** — which actions had human approval, which did not
- **Switch** — stop, with the state of the last check

No log viewer. Nobody reads logs. The value is in the ranked exceptions.

## 12. Non-goals

- Not a general EDR or intrusion detection system.
- Not cross-platform in v0. Linux only, where eBPF and auditd exist. macOS
  (Endpoint Security framework) and Windows (ETW) are separate, later efforts.
- Not a content archive. Never store file contents.
- Not a compliance product.
- Not a replacement for AgentSight, Tetragon, or Falco — a consumer of them.

## 13. Staged plan

**Stage 0 — evaluate, do not build.** One afternoon against real agent activity:
does AgentSight already give intent-to-effect correlation on this machine? If
yes, the engine exists and this project is a front end. That answer decides
everything downstream.

**Stage 1 — record format and correlation.** Define the record. Prove the join on
a single host with one agent. No UI, no kill switch.

**Stage 2 — privilege separation.** Agent unprivileged, recorder above it. Prove
the agent cannot reach the recorder.

**Stage 3 — dashboard and switch.** Only once the record is trustworthy.

## 14. Open questions

- Is the machine plane part of Agent Diary, or a companion package that writes
  into it? (Current lean: companion, to keep Agent Diary's zero-dependency,
  local-first contract intact.)
- What is the retention policy for machine events? They accumulate faster than
  entries do.
- Does a divergence raise an alarm, or only appear in the timeline?
- On a single-machine install, is privilege separation achievable without asking
  the user to reason about Unix users and groups?
- If the agent runs in a container or VM, does the machine plane belong inside it
  or outside it?
