# Agent Diary Onboarding Implementation Plan

**Status:** implemented on branch `feature/onboarding`. All steps are built:
`init`, `discover`, `consent`, `export`, `preview`, `import`, `review`,
`connect`, plus the resumable state machine, source registry, agent-facing
`next_action` contract, CLI surface, end-to-end tests and documentation.
Deviations from this plan are recorded in the commits that made them.

**Reviewed:** the branch at `0eb4ac8` was reviewed architecturally before
preview/import work continues; all findings were fixed in `b73c67f` and the
boundary cases they identified now have tests in
`tests/test_onboarding_boundaries.py`.

**Goal:** A new user — or their agent acting on their behalf — can go from a fresh clone to a preloaded, consent-gated diary that Hermes recalls from, without reading source code.

**Architecture:** Onboarding is *orchestration and persisted state* layered over the orientation commands that already exist and already have tests. One state machine on disk, two front-ends: a human walkthrough and an agent-oriented JSON interface. Every step is idempotent and resumable, and both front-ends drive the identical state.

**Tech stack:** Python 3.11, existing `argparse` CLI (`src/agent_diary/cli/main.py`, 1489 lines, 71 subcommands), existing orientation modules, pytest (29 modules, 472 tests).

---

## The governing principle

The user's constraint, verbatim:

> "the user points their agent at the download, then the agent walks the user through whatever they need to do... definitely it should be possible manually as well but they go hand in hand"

So: **agent-drivable and human-drivable are the same product, not two products.**

That means every step must satisfy all four:

1. **Idempotent** — running it twice is safe.
2. **Resumable** — state on disk, not in a process's memory.
3. **Non-interactive path** — every prompt has a flag equivalent.
4. **Machine-readable status** — an agent can always ask "where am I, what's next, what needs a human?"

A human walkthrough that shells out to the same functions the JSON path uses. Never a second implementation.

---

## Current state

### What already exists (do not rebuild)

```text
consent pipeline    orientation-create, -consent, -inventory, -preview, -import
derived layer       orientation-candidates-generate, -candidate-review, -candidate-list
knowledge layer     orientation-knowledge-list, -search, -revoke
evaluation          orientation-context-pack, -context-evaluate, -eval-run
import              import-entries-jsonl, import-session-jsonl, import-telegram-direct,
                    import-openclaw-session, import-session-and-analyze
reference adapter   scripts/hermes-to-diary.sh  (reads ~/.hermes/state.db)
agent contract      AGENTS.md
docs                getting-started, user-guide-v1, import-pipelines, agent-integration
```

The privacy model is already correct and must not be weakened: metadata-only inventory, explicit consent, preview before import, review before knowledge.

### What is missing

| Gap | Consequence today |
|---|---|
| No entry point | 71 subcommands, no indication which four matter |
| Data root is env-or-`project_root/data` | `config.py:25-26` — no persistent user config; service must set `AGENTDIARY_DATA` by hand |
| No persisted onboarding state | An agent cannot ask "what's next?" |
| No source registry | Discovery logic lives in shell scripts with hardcoded paths |
| Documentation assumes a developer | "adapt paths to your environment" is a wall |

### Assumption to verify early

`~/.hermes/state.db` must be read **read-only** and only for metadata during discovery. Never open it for write. If the file is missing or locked, discovery reports "not found" rather than failing.

---

## Design

### State file

```jsonc
// ~/.local/state/agent-diary/onboarding.json
{
  "version": 1,
  "data_root": "/home/user/.local/share/agent-diary",
  "step": "consent",              // init|discover|consent|preview|import|review|connect|done
  "completed": ["init", "discover"],
  "sources": [
    {"id": "hermes", "kind": "hermes_sqlite", "path": "~/.hermes/state.db",
     "detected": true, "sessions": 412, "oldest": "...", "newest": "...",
     "consent": "pending"}         // pending|metadata|content|denied|imported
  ],
  "orientation_run_id": "…",
  "imported_entries": 0,
  "candidates": {"proposed": 0, "approved": 0, "rejected": 0},
  "agent_connected": false
}
```

### Front-ends

```bash
agent-diary onboard                     # human: explains each step, prompts
agent-diary onboard --json              # agent: current step, next action, what needs a human
agent-diary onboard --step consent --answer '{"hermes":"content"}'   # execute one step
agent-diary onboard --step discover --json
agent-diary status --json               # lightweight "where am I"
```

`--json` output shape (stable, versioned):

```json
{"step": "consent", "completed": ["init","discover"],
 "action": {"command": "onboard --step consent", "needs_human": true,
            "question": "Which sources may be imported, and at what scope?",
            "options": [{"id":"hermes","choices":["metadata","content","denied"]}]},
 "blockers": []}
```

`needs_human: true` is the only thing that should ever cause an agent to stop and ask.

### Step map — each step wraps existing commands

| Step | Does | Wraps |
|---|---|---|
| init | choose + persist data root, run doctor | `doctor` |
| discover | metadata-only inventory of detectable sources | new: registry + `orientation-inventory` |
| consent | record scopes into a manifest | `orientation-create`, `orientation-consent` |
| preview | show sample, confirm | `orientation-preview` |
| import | import approved sources | `orientation-import` |
| review | propose candidates, review each | `orientation-candidates-generate`, `-candidate-review` |
| connect | write agent config / MCP entry | new |

No new import machinery. No new consent machinery. No new knowledge machinery.

---

## Tasks

### Task 1: Persistent config with explicit resolution order

**Objective:** stop the data-root footgun permanently.

**Files:**
- Modify: `src/agent_diary/config.py:20-35`
- Create: `tests/test_config_resolution.py`

**Step 1:** Write failing tests for four resolution cases, in order of precedence:

```python
def test_explicit_flag_beats_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTDIARY_DATA", str(tmp_path / "env"))
    assert resolve_data_root(explicit=tmp_path / "flag") == (tmp_path / "flag").resolve()

def test_env_beats_config_file(tmp_path, monkeypatch): ...
def test_config_file_beats_default(tmp_path, monkeypatch): ...
def test_default_is_project_data_when_nothing_set(tmp_path, monkeypatch): ...
```

**Step 2:** Run `pytest tests/test_config_resolution.py -v` — expect 4 failures.

**Step 3:** Implement `resolve_data_root(explicit=None)` with order: flag → `AGENTDIARY_DATA` → `~/.config/agent-diary/config.json` → `project_root/data`. Config file read via `json.loads`, missing/corrupt file treated as absent with a warning, never a crash.

**Step 4:** `pytest tests/test_config_resolution.py -v` — expect 4 passed. Then full suite: `pytest -q` — expect 472+ passed.

**Step 5:** Commit: `feat: resolve data root from flag, env, config file, then default`

---

### Task 2: `agent-diary init`

**Objective:** one command that makes a diary usable.

**Files:**
- Create: `src/agent_diary/onboarding_state.py`
- Create: `src/agent_diary/cli/onboarding.py`
- Modify: `src/agent_diary/cli/main.py` (register the new parser module)
- Create: `tests/test_onboarding_init.py`

**Behavior:**
- `init` with no args: proposes `~/.local/share/agent-diary`, shows what's already there and its size, and **requires explicit confirmation or a supplied path**. It never silently adopts a default — a wrong data root is indistinguishable from lost memory.
- `init --data-root PATH`: explicit, no prompt.
- `init --yes`: accepts the proposed path non-interactively (for agents), and is the flag an agent uses once the user has confirmed the location.
- `init --json`: emit `{"data_root":..., "doctor":"healthy", "next":"discover"}`; without `--yes` or `--data-root` it returns `needs_human: true` with the proposed path rather than writing anything.
- Refuses to overwrite an existing config without `--force`; running twice is a no-op reporting "already initialized".

**Tests:** config written with 0600; second run is a no-op; `--json` is valid JSON; `doctor` failure surfaces as a non-zero exit with the report attached.

**Commit:** `feat: add agent-diary init with persisted data root`

---

### Task 3: Source registry (metadata only)

**Objective:** know where history might live, without reading it.

**Files:**
- Create: `src/agent_diary/onboarding_sources.py`
- Create: `tests/test_onboarding_sources.py`

**Registry entries — detection only:**

```python
SOURCES = [
  Source("hermes",   "hermes_sqlite", "~/.hermes/state.db"),
  Source("openclaw", "openclaw_store", "~/.openclaw"),
  Source("telegram", "telegram_export", "<user-supplied>"),
  Source("jsonl",    "canonical_jsonl", "<user-supplied>"),
]
```

**Hard rules, enforced by tests:**
- Hermes detection runs `SELECT COUNT(*), MIN(started_at), MAX(started_at) FROM sessions` **on a read-only connection** (`file:...?mode=ro`).
- No table besides `sessions` is queried during detection.
- No message content is read during detection. A test asserts the detection function's SQL contains no `messages` table reference.
- Missing database → `detected: false`, never an exception.

**Commit:** `feat: add metadata-only source registry for onboarding`

---

### Task 4: Onboarding state machine

**Objective:** resumable state, safe to run concurrently.

**Files:**
- Modify: `src/agent_diary/onboarding_state.py`
- Create: `tests/test_onboarding_state.py`

**Behavior:** load/save with atomic replace (`tmp` + `os.replace`), `0600`, schema `version`. Step transitions validated against a legal-transition map; an illegal jump raises rather than silently corrupting.

**Tests:** atomic write survives a simulated crash between tmp-write and replace; illegal transition (`init` → `import`) raises; unknown step name is rejected with the valid list.

**Commit:** `feat: add resumable onboarding state with validated transitions`

---

### Task 5: `discover` step

**Objective:** tell the user (or agent) what was found, in metadata only.

**Files:**
- Modify: `src/agent_diary/cli/onboarding.py`
- Create: `tests/test_onboarding_discover.py`

**Output (human):**

```text
Found 3 sources.

  hermes     412 sessions   2026-03-02 → 2026-11-05   (~/​.hermes/state.db)
  telegram   not found
  jsonl      not configured

Nothing has been read from these yet.
```

**Output (`--json`):** the `sources` array from the state file, plus `"action": {"needs_human": false}`.

**Tests:** fixtures with a synthetic sqlite DB; a missing DB; a zero-session DB. Assert no message table is touched.

**Commit:** `feat: add onboarding discover step`

---

### Task 6: `consent` step

**Objective:** the human decision, recorded immutably.

**Files:**
- Modify: `src/agent_diary/cli/onboarding.py`
- Create: `tests/test_onboarding_consent.py`

**Behavior:**
- Human mode prompts per detected source: `content / metadata-only / skip`, defaulting to **skip** (safe default, explicit opt-in).
- `--answer '{"hermes":"content"}'` performs the same choice non-interactively.
- Writes the manifest via existing `orientation-create` + `orientation-consent`. Does not invent a parallel consent store.
- `--json` reports `needs_human: true` when any source is `pending`.

**Tests:** default is skip; a denied source never reaches preview; the manifest round-trips.

**Commit:** `feat: add onboarding consent step over the orientation manifest`

---

### Task 8a: `export` step (discovered during implementation — sits between consent and preview)

**Why this exists:** `preview_approved_source` and `import_approved_history` both require the run's `locator` to be a **readable export file**, not a database. Consent records the decision against the Hermes DB path, so something must materialise a transcript before preview can run.

**Hermes source shape (verified against `~/.hermes/state.db`):**

```text
messages:  id, session_id, role, content, timestamp, tool_name, tool_calls, ...
sessions:  id, source, title, started_at, ended_at, message_count, ...
```

**Output format:** `generic-message-jsonl` — the only supported adapter format that accepts a flat message stream with no vendor envelope. One JSON object per line, normalised by `_normalize_message`, which requires:

```text
message_id    (aliases accepted by _require_message_id)
created_at    (aliases accepted by _require_created_at)
role          (aliases accepted by _require_role)
content       (aliases accepted by _require_content)
```

**Behaviour:**
1. Refuses to run unless the source consent is `approved` with `content_access_granted: true` — the export is the first moment content is read, so the check must be here as well as in preview.
2. Opens `~/.hermes/state.db` **read-only** (`mode=ro`).
3. Selects messages for sessions not already exported, ordered by timestamp, using a marker file per session for idempotence (same approach as `scripts/hermes-to-diary.sh`).
4. Writes `<data_root>/imports/hermes-export-<timestamp>.jsonl`.
5. Calls `adapt_session_export(format_name="generic-message-jsonl")` to validate and canonicalise, proving the file is readable before the locator is trusted.
6. Updates the orientation run's source locator to the export path, and records `format_name` in the run metadata for preview/import to use.

**Tests:** export refuses without consent; refuses with `metadata` scope; produces a file the adapter accepts; running twice exports only new sessions; a session deleted from Hermes does not break a re-run.

**Commit:** `feat: add onboarding export step from Hermes database to canonical JSONL`

---

### Task 8b: `preview` step

**Objective:** show real sample content before anything is committed.

**Files:**
- Modify: `src/agent_diary/cli/onboarding.py`
- Create: `tests/test_onboarding_preview.py`

**Behavior:** wraps `preview_approved_source` with the export's `format_name`; prints the first 3 items and a count. For scope `metadata`, states plainly that only metadata will be imported. `--confirm` advances; without it, the step stays put.

**Commit:** `feat: add onboarding preview step`

---

### Task 8: `import` step

**Objective:** idempotent import with honest counts.

**Files:**
- Modify: `src/agent_diary/cli/onboarding.py`
- Create: `tests/test_onboarding_import.py`

**Behavior:** wraps `orientation-import`. Reports imported vs skipped. Re-running imports nothing new and says so. Partial failure leaves the step incomplete with the failure recorded — never advances on error.

**Commit:** `feat: add onboarding import step with idempotent counts`

---

### Task 9: `review` step

**Objective:** derived facts only become knowledge by human approval.

**Files:**
- Modify: `src/agent_diary/cli/onboarding.py`
- Create: `tests/test_onboarding_review.py`

**Behavior:** `orientation-candidates-generate` then a review loop over `-candidate-review`. Human mode: show each candidate with its source link, accept/reject. `--json` lists pending candidates with `needs_human: true`. `--auto-reject-unreviewed` completes the step leaving nothing approved — a legitimate choice.

**Commit:** `feat: add onboarding review step for derived knowledge`

---

### Task 10: `connect` step

**Objective:** point the agent at the diary so recall starts working.

**Files:**
- Modify: `src/agent_diary/cli/onboarding.py`
- Create: `docs/mcp.md` (extend, don't rewrite)
- Create: `tests/test_onboarding_connect.py`

**Behavior:** writes an MCP entry (and prints the equivalent HTTP config) for the detected agent runtime, backing up any existing config first. Idempotent: if the entry already matches, do nothing. Verifies by calling the diary's own endpoint and reporting the real status.

**Commit:** `feat: add onboarding connect step for agent integration`

---

### Task 11: Human walkthrough front-end

**Objective:** the same state machine, with explanation.

**Files:**
- Modify: `src/agent_diary/cli/onboarding.py`

Plain-language explanation per step, top-to-bottom flow, and at the end a summary: what's stored, where, what was declined, how to change it. Every prompt displays the non-interactive equivalent so a user can automate later:

```text
Import scope for hermes? [content / metadata / skip] (default: skip)
  non-interactive equivalent: agent-diary onboard --step consent --answer '{"hermes":"content"}'
```

**Commit:** `feat: add human walkthrough for onboarding`

---

### Task 12: Agent contract

**Objective:** an agent reading AGENTS.md knows exactly what to do.

**Files:**
- Modify: `AGENTS.md`
- Create: `docs/onboarding.md`

Add to `AGENTS.md`:

```markdown
## Onboarding a new installation

Run `agent-diary onboard --json` and follow `action.command`.
Stop and ask the user ONLY when `action.needs_human` is true.
Never choose a consent scope on the user's behalf.
Never read source history outside the identified sources.
```

**Commit:** `docs: document onboarding contract for agents and humans`

---

### Task 13: End-to-end test

**Objective:** prove the promise, not the parts.

**Files:**
- Create: `tests/test_onboarding_end_to_end.py`

Run the entire flow against a synthetic fixture: init → discover → consent → preview → import → review → connect, asserting at each step that `--json` reports a legal next action, and finally that a search returns the imported content. Then run the flow twice and assert the second run imports zero new entries.

**Commit:** `test: end-to-end onboarding from empty install to recall`

---

## Files likely to change

```text
modify   src/agent_diary/config.py                    data-root resolution
modify   src/agent_diary/cli/main.py                  register onboarding parser
modify   AGENTS.md                                    agent contract
create   src/agent_diary/onboarding_state.py
create   src/agent_diary/onboarding_sources.py
create   src/agent_diary/onboarding.py                step implementations
create   src/agent_diary/cli/onboarding.py            CLI surface
create   docs/onboarding.md
create   tests/test_config_resolution.py
create   tests/test_onboarding_state.py
create   tests/test_onboarding_sources.py
create   tests/test_onboarding_init.py
create   tests/test_onboarding_discover.py
create   tests/test_onboarding_consent.py
create   tests/test_onboarding_preview.py
create   tests/test_onboarding_import.py
create   tests/test_onboarding_review.py
create   tests/test_onboarding_connect.py
create   tests/test_onboarding_end_to_end.py
```

---

## Validation

```bash
pytest -q tests/test_config_resolution.py tests/test_onboarding_*.py
pytest -q                                   # full suite, currently 472 passing
```

Manual acceptance, on a scratch directory:

```bash
agent-diary init --data-root /tmp/diary-test
agent-diary onboard --json          # legal, resumable, needs_human where appropriate
agent-diary onboard                # same flow, explained
agent-diary status --json
```

---

## Risks and open questions

1. **Scope creep into a wizard.** The orientation machinery already exists; onboarding must stay orchestration. Any task that starts re-implementing consent or import is wrong.

2. **Discovery breadth.** Start with Hermes only, shipped and proven, then add the rest behind the same interface. A registry with four half-working detectors is worse than one that works.

3. **Hermes database coupling.** We're reading another product's private storage. Read-only, metadata-only, and degrade to "not found" rather than failing. If Hermes changes its schema, onboarding must not break — detection should treat an unknown schema as absent.

4. **`needs_human` discipline.** If agents get a path that lets them pick consent scope themselves, the consent model is decorative. Worth an explicit test asserting the JSON path refuses to set a scope without an answer payload.

5. **Decided — `connect` writes automatically.** The step writes the agent config itself, backing up any existing file first, and reports exactly what it changed. It verifies by calling the diary's own endpoint so success is demonstrated, not asserted. `--print-only` exists for anyone who wants to see the change before it lands, but the default is to do it.

6. **Decided — `init` always makes the user choose the data root.** No silent default. The step proposes a sensible path and shows its size and existing contents, but the user must confirm or supply one. A wrong data root looks exactly like lost memory, and that ambiguity is worth one extra prompt.
