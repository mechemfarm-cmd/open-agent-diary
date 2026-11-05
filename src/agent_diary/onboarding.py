"""Onboarding steps: one implementation, two front-ends.

Each step function is pure orchestration over state that already exists. The
human walkthrough and the agent's JSON interface call the same functions; the
only difference is whether a question is rendered for a person or returned as
data for an agent.

The contract that makes agent-driving safe is `next_action`: it returns a
machine-readable description of what to do next, and sets `needs_human` only
for decisions that genuinely belong to the user — consent scope, above all.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from agent_diary.config import config_file_path, default_paths
from agent_diary.onboarding_sources import describe, discover
from agent_diary.onboarding_state import (
    OnboardingError,
    OnboardingState,
    STEPS,
)

PROPOSED_ROOT = "~/.local/share/agent-diary"

# What each step is for, in plain language. Used by the human front-end and
# echoed in JSON so an agent can explain itself without inventing wording.
STEP_PURPOSE = {
    "init": "Choose where your diary lives, and create it.",
    "discover": "Look for history worth importing — without reading it yet.",
    "consent": "Decide which sources may be imported, and at what scope.",
    "preview": "See a real sample of what would be imported.",
    "import": "Bring the approved history in.",
    "review": "Approve which derived facts become lasting knowledge.",
    "connect": "Point your agent at the diary so it recalls from it.",
    "done": "Onboarding is complete.",
}


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def run_init(
    data_root: Path | str | None = None,
    *,
    accept_proposal: bool = False,
    force: bool = False,
) -> dict:
    """Resolve and persist the data root, then create the diary.

    Never adopts a default silently. Without an explicit choice (or an explicit
    acceptance of the proposal) this reports that a human decision is needed.
    """
    existing = config_file_path()
    if existing.exists() and not force:
        try:
            current = json.loads(existing.read_text(encoding="utf-8"))
            resolved = Path(current.get("data_root", "")).expanduser()
        except (OSError, ValueError):
            resolved = None
        if resolved is not None and str(resolved):
            return {
                "status": "already_initialized",
                "data_root": str(resolved),
                "config": str(existing),
                "next": "discover",
                "changed": False,
            }

    if data_root is None:
        proposal = Path(PROPOSED_ROOT).expanduser()
        if not accept_proposal:
            return {
                "status": "needs_human",
                "needs_human": True,
                "question": "Where should your diary live?",
                "proposed": str(proposal),
                "proposed_exists": proposal.exists(),
                "proposed_entries": (
                    len(list(proposal.iterdir())) if proposal.exists() else 0
                ),
                "answer_with": "agent-diary init --data-root <path>  (or --yes to accept the proposal)",
                "changed": False,
            }
        data_root = proposal

    resolved = Path(data_root).expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)

    config_file_path().parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "data_root": str(resolved)}
    config_file_path().write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(config_file_path(), 0o600)

    state = OnboardingState.load(resolved)
    state.data_root = str(resolved)
    if state.step == "init":
        # init is finished the moment the root exists; leaving the state at
        # 'init' would make an agent repeat a step that already succeeded.
        state.advance("discover")
    state.save()

    return {
        "status": "initialized",
        "data_root": str(resolved),
        "config": str(config_file_path()),
        "next": "discover",
        "changed": True,
    }


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


def run_discover(state: OnboardingState, *, source_paths: dict[str, str] | None = None) -> dict:
    """Populate source metadata. Never reads content."""
    state.sources = discover(paths=source_paths)
    state.save()
    return {
        "status": "discovered",
        "sources": [s.to_dict() for s in state.sources],
        "lines": [describe(s) for s in state.sources],
        "detected": [s.id for s in state.sources if s.detected],
        "needs_human": False,
        "next": "consent",
    }


# ---------------------------------------------------------------------------
# consent
# ---------------------------------------------------------------------------

# Only sources the orientation layer can represent today. Anything else is
# reported as unsupported rather than silently mapped onto a kind it isn't.
SUPPORTED_CONSENT_KINDS = {"hermes": "hermes_history"}


def run_consent(
    state: OnboardingState,
    answers: dict[str, str] | None = None,
    *,
    paths=None,
) -> dict:
    """Record the user's scope decision for each detected source.

    `answers` maps source id to one of content / metadata / denied. A source
    with no answer stays pending and the step stays put — consent is never
    inferred from silence.
    """
    from agent_diary.config import default_paths
    from agent_diary.orientation import create_run, set_consent

    answers = answers or {}
    detected = [s for s in state.sources if s.detected]
    if not detected:
        return {
            "status": "nothing_to_consent",
            "detail": "No sources were detected, so there is nothing to import.",
            "needs_human": False,
            "next": "review",
        }

    unsupported = [
        s.id for s in detected if s.id not in SUPPORTED_CONSENT_KINDS
    ]
    actionable = [s for s in detected if s.id in SUPPORTED_CONSENT_KINDS]

    answered = {sid: v for sid, v in answers.items() if sid in {s.id for s in actionable}}
    missing = [s.id for s in actionable if s.id not in answered]
    if missing:
        return {
            "status": "needs_human",
            "needs_human": True,
            "question": "Which sources may be imported, and at what scope?",
            "awaiting": missing,
            "options": ["content", "metadata", "denied"],
            "unsupported": unsupported,
            "detail": "Consent is not inferred from silence; every detected source needs an answer.",
        }

    invalid = {
        sid: v for sid, v in answered.items() if v not in {"content", "metadata", "denied"}
    }
    if invalid:
        return {
            "status": "invalid_answer",
            "detail": f"unknown scope(s): {invalid}; expected content, metadata or denied",
            "needs_human": True,
        }

    resolved_paths = paths or default_paths()

    # The run is created with the scope each source was granted; set_consent
    # then records the decision. Reusing the orientation layer means the
    # consent record is the same one every other diary command understands.
    sources = [
        {
            "source_id": s.id,
            "kind": SUPPORTED_CONSENT_KINDS[s.id],
            "locator": s.path,
            "scope": answered[s.id] if answered[s.id] != "denied" else "metadata",
        }
        for s in actionable
    ]

    if state.orientation_run_id:
        run_id = state.orientation_run_id
    else:
        run = create_run(resolved_paths, sources)
        run_id = run["run_id"]
        state.orientation_run_id = run_id

    recorded = {}
    for s in actionable:
        decision = "approved" if answered[s.id] != "denied" else "rejected"
        set_consent(resolved_paths, run_id, s.id, decision)
        s.consent = (
            "content"
            if answered[s.id] == "content"
            else "metadata"
            if answered[s.id] == "metadata"
            else "denied"
        )
        recorded[s.id] = answered[s.id]

    state.save()
    return {
        "status": "consented",
        "run_id": run_id,
        "recorded": recorded,
        "unsupported": unsupported,
        "needs_human": False,
        "next": "preview",
    }


# ---------------------------------------------------------------------------
# the agent-facing contract
# ---------------------------------------------------------------------------


def next_action(state: OnboardingState) -> dict:
    """Describe the next step in machine-readable form.

    `needs_human` is true only where a person must decide. An agent should be
    able to run everything else without asking.
    """
    step = state.step
    action: dict = {
        "step": step,
        "purpose": STEP_PURPOSE.get(step, ""),
        "completed": list(state.completed),
        "needs_human": False,
        "blockers": [],
    }

    if step == "init":
        action["command"] = "agent-diary init --data-root <path>"
        action["needs_human"] = True
        action["question"] = STEP_PURPOSE["init"]
        action["hint"] = "A wrong data root looks identical to lost memory, so this is the user's call."

    elif step == "discover":
        action["command"] = "agent-diary onboard --step discover"

    elif step == "consent":
        pending = state.pending_human_questions()
        action["command"] = "agent-diary onboard --step consent"
        if pending:
            action["needs_human"] = True
            action["question"] = "Which sources may be imported, and at what scope?"
            action["sources"] = pending
            action["options"] = ["content", "metadata", "denied"]
            action["hint"] = "An agent must never choose a scope on the user's behalf."
        else:
            action["blockers"].append("no sources remain pending a decision")

    elif step == "preview":
        action["command"] = "agent-diary onboard --step preview --confirm"

    elif step == "import":
        action["command"] = "agent-diary onboard --step import"

    elif step == "review":
        action["command"] = "agent-diary onboard --step review"
        proposed = state.candidates.get("proposed", 0)
        if proposed:
            action["needs_human"] = True
            action["question"] = f"Approve {proposed} derived fact(s) into knowledge?"
            action["candidates"] = dict(state.candidates)

    elif step == "connect":
        action["command"] = "agent-diary onboard --step connect"

    elif step == "done":
        action["command"] = None
        action["purpose"] = "Nothing left to do."

    return action


def status(state: OnboardingState) -> dict:
    """Everything an agent needs to orient itself in one call."""
    return {
        "data_root": state.data_root,
        "step": state.step,
        "order": list(STEPS),
        "completed": list(state.completed),
        "sources": [
            {
                "id": s.id,
                "detected": s.detected,
                "sessions": s.sessions,
                "consent": s.consent,
            }
            for s in state.sources
        ],
        "imported_entries": state.imported_entries,
        "candidates": dict(state.candidates),
        "agent_connected": state.agent_connected,
        "action": next_action(state),
    }
