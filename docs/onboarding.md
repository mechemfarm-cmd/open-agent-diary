# Onboarding

`agent-diary onboard` takes a new installation from an empty diary to one that
holds your own history and is wired into your agent. It is resumable, so you can
stop and come back; it is consent-gated, so nothing is read until you say so;
and it has a JSON interface, so your agent can drive it for you.

Every step exists twice — a person can run it, and an agent can run the same
step non-interactively. There is one implementation and one state file. Nothing
in this document describes a separate "agent mode".

## The sequence

```
init → discover → consent → export → preview → import → review → connect → done
```

| Step | What it does | Needs a human? |
| --- | --- | --- |
| `init` | Chooses where the diary lives and scaffolds it. | **Yes** — a wrong root looks identical to lost memory. |
| `discover` | Finds candidate sources and reports their size and date range. Reads no content. | No |
| `consent` | Records, per source, `content`, `metadata`, or `denied`. | **Yes** — an agent must never choose this. |
| `export` | Writes approved content to a readable transcript. First moment content is read. | No |
| `preview` | Shows real samples and counts. Requires `--confirm`. | **Yes** — this is the last look before import. |
| `import` | Writes entries. Idempotent. | No |
| `review` | Approves derived facts into knowledge. | **Yes** — empty knowledge is a valid answer. |
| `connect` | Writes your agent's MCP entry, with a backup. | No |

`needs_human: true` is the only thing that should stop an agent. Everything else
it can run itself.

## What is read, and when

Nothing is read during `discover` — it opens the Hermes database read-only and
asks for a session count and a date range.

Content is first read by `export`, and only for sources where the recorded
decision is `approved` **and** the scope is `content`. That is checked again at
export time against the orientation run, not merely trusted from the onboarding
state, so editing the state file by hand does not grant access.

`export` opens the source database `mode=ro` and never writes to it.

Not imported: secret files, `.env`, SSH keys, browser data, caches. There is no
whole-disk scan; sources are named explicitly.

## Driving it from an agent

Ask for status first. It returns the current step, what is done, every source's
decision, and the exact next command:

```bash
agent-diary --json onboard --status
```

The response contains `action.command`, which is copy-pasteable as emitted
(including `--json`) and includes `--step` explicitly. Run it verbatim.

Example, granting content scope for the Hermes source:

```bash
agent-diary --json onboard --step consent --answer '{"hermes":"content"}'
```

Declining all derived knowledge at the review step — a legitimate choice, and
the one that leaves the knowledge base empty on purpose:

```bash
agent-diary --json onboard --step review --auto-reject-unreviewed
```

Approving specific candidates:

```bash
agent-diary --json onboard --step review --approve cand_abc --approve cand_def
```

### What an agent must not do

- Choose a data root for the user. Report the proposal and ask.
- Grant `content` scope on the user's behalf. Ask which sources, and at what scope.
- Approve derived knowledge the user has not seen.
- Advance past `preview` without `--confirm`.

The first three are enforced by the steps themselves where enforcement is
possible; the fourth is enforced by `--confirm` being a separate flag.

## Manual walkthrough

Same commands, one at a time, reading the human-readable output:

```bash
agent-diary init --data-root ~/.local/share/agent-diary
agent-diary onboard --status
agent-diary onboard --step discover
agent-diary onboard --step consent --answer '{"hermes":"content"}'
agent-diary onboard --step export
agent-diary onboard --step preview          # look at the samples
agent-diary onboard --step preview --confirm
agent-diary onboard --step import
agent-diary onboard --step review           # then --approve <id> or --auto-reject-unreviewed
agent-diary onboard --step connect
```

Every prompt shows its non-interactive equivalent, so anything you do by hand
can be scripted later.

## Where things live

- Config: `~/.config/agent-diary/config.json` (`data_root`)
- Onboarding state: `<data_root>/config/onboarding.json`
- Orientation runs: `<data_root>/config/orientation_runs/`
- Exported transcripts: `<data_root>/imports/hermes-export-*.jsonl`
- Export markers (idempotence): `<data_root>/imports/.export-markers/`

## Resuming and re-running

`--status` always reports the truth; nothing advances on a failed step.

Re-running a completed step is safe:

- `export` takes only sessions not already marked, and a session deleted from
  the source does not break it.
- `import` derives its import id from the run and source, so a repeat imports
  nothing and says so.
- `connect` writes nothing if the entry already matches.

## If something goes wrong

- **`init` reports `needs_human`** — that is correct; pass `--data-root`, or
  `--yes` to accept the proposed location.
- **Doctor reports errors after `init`** — that should not happen; `init`
  scaffolds directories and the database. Report it.
- **`connect` says `existing_entry_unrecognized`** — the config has an
  `agent-diary` entry in a shape this step does not edit. Fix it by hand using
  the `entry` in the response; the step refuses rather than inserting a
  duplicate key.
- **Export finds no new sessions** — `nothing_new` means everything already
  exported; the locator still points at the previous transcript.
