"""Onboarding steps: one implementation, two front-ends.

Each step is orchestration over state that already exists. The human
walkthrough and the agent's JSON interface call the same functions; the only
difference is whether a question is rendered for a person or returned as data.

Invariants, all of which the architecture review asked for:

- Every step resolves paths from ``state.data_root``. No step calls bare
  ``default_paths()``, because ambient resolution can point at a different
  diary than the one the user chose.
- ``needs_human`` is true only for decisions that belong to the user.
- Steps are idempotent: running a completed step again returns the existing
  result instead of re-deciding.
- Steps with nothing to do report a no-op and advance along the linear order,
  so the sequence stays predictable.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from agent_diary.config import default_paths
from agent_diary.onboarding_sources import describe, discover
from agent_diary.onboarding_state import (
    OnboardingError,
    OnboardingState,
    STEPS,
    SUPPORTED_KINDS,
)

PROPOSED_ROOT = "~/.local/share/agent-diary"

STEP_PURPOSE = {
    "init": "Choose where your diary lives, and create it.",
    "discover": "Look for history worth importing — without reading it yet.",
    "consent": "Decide which sources may be imported, and at what scope.",
    "export": "Write the approved history to a transcript file the diary can read.",
    "preview": "See a real sample of what would be imported.",
    "import": "Bring the approved history in.",
    "review": "Approve which derived facts become lasting knowledge.",
    "connect": "Point your agent at the diary so it recalls from it.",
    "done": "Onboarding is complete.",
}

# Orientation kinds, for sources we can actually represent.
ORIENTATION_KIND = {"hermes": "hermes_history"}

# The adapter format the exporter produces.
EXPORT_FORMAT = "generic-message-jsonl"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def paths_for(state: OnboardingState):
    """The only correct way for a step to reach storage."""
    return default_paths(data_root=Path(state.data_root))


def _atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Write via unique temp file plus replace, so a crash can't truncate."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    tmp_name = handle.name
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_config(path: Path) -> dict | None:
    """Read the config file. Malformed or empty means 'not configured'."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _configured_root(payload: dict | None) -> str | None:
    if not payload:
        return None
    value = payload.get("data_root")
    if not isinstance(value, str) or not value.strip():
        return None
    return value


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def run_init(
    data_root: Path | str | None = None,
    *,
    accept_proposal: bool = False,
    force: bool = False,
) -> dict:
    """Resolve and persist the data root, create the diary, report its health.

    Never adopts a default silently: without an explicit choice (or an explicit
    acceptance of the proposal) this reports that a human decision is needed.
    """
    from agent_diary.config import config_file_path

    config_path = config_file_path()

    if config_path.exists() and not force:
        existing = _configured_root(_read_config(config_path))
        if existing:
            resolved = Path(existing).expanduser()
            return {
                "status": "already_initialized",
                "data_root": str(resolved),
                "config": str(config_path),
                "next": "discover",
                "changed": False,
            }
        # An empty or malformed config is not "initialized" — fall through and
        # let the user choose, rather than silently adopting Path(".").

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

    _atomic_write_text(
        config_path,
        json.dumps({"version": 1, "data_root": str(resolved)}, indent=2, sort_keys=True)
        + "\n",
    )

    state = OnboardingState.load(resolved)
    state.data_root = str(resolved)
    if state.step == "init":
        state.advance("discover")
    state.save()

    scaffold = _scaffold(paths_for(state))

    return {
        "status": "initialized",
        "data_root": str(resolved),
        "config": str(config_path),
        "scaffold": scaffold,
        "doctor": _run_doctor(paths_for(state)),
        "next": "discover",
        "changed": True,
    }


def _scaffold(paths) -> dict:
    """Create the diary's internal layout so a fresh install is usable.

    A root directory alone is not a diary: without these, the first real write
    fails and `doctor` reports eight missing paths.
    """
    from agent_diary.index.sqlite_index import bootstrap_sqlite

    directories = [
        paths.entries_dir,
        paths.work_trace_dir,
        paths.overlays_dir,
        paths.artifacts_dir,
        paths.imports_dir,
        paths.index_dir,
        paths.config_dir,
        paths.archive_dir,
    ]
    created = []
    for directory in directories:
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            created.append(str(directory))
    bootstrap_sqlite(paths.sqlite_path)
    return {
        "directories_created": len(created),
        "database": str(paths.sqlite_path),
        "database_created": paths.sqlite_path.exists(),
    }


def _run_doctor(paths) -> dict:
    """Health check after the root exists. Failure is reported, not raised."""
    try:
        from agent_diary.service.doctor import run_doctor

        report = run_doctor(paths)
    except Exception as exc:  # doctor must never break init
        return {"status": "unavailable", "detail": str(exc)}
    summary = report.get("summary", {}) if isinstance(report, dict) else {}
    return {
        "status": "ok" if not summary.get("error_count") else "issues",
        "error_count": summary.get("error_count", 0),
        "warning_count": summary.get("warning_count", 0),
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

    Idempotent: if the run already exists and every actionable source has a
    decision, this returns the existing outcome instead of re-deciding.
    """
    from agent_diary.orientation import create_run, load_run, set_consent

    answers = answers or {}
    resolved_paths = paths or paths_for(state)

    detected = [s for s in state.sources if s.detected]
    actionable = [s for s in detected if s.kind in SUPPORTED_KINDS]
    unsupported = [s.id for s in detected if s.kind not in SUPPORTED_KINDS]

    # Unsupported sources are explicitly unavailable, not silently pending.
    for s in detected:
        if s.kind not in SUPPORTED_KINDS and s.decision == "pending":
            s.decision = "unavailable"

    if not actionable:
        state.save()
        return {
            "status": "nothing_to_consent",
            "detail": "No importable sources were detected, so there is nothing to consent.",
            "unsupported": unsupported,
            "needs_human": False,
            "no_op_steps": ["export", "preview", "import", "review"],
            "next": "export",
        }

    # Idempotence: already decided means report, don't re-decide.
    already = [s for s in actionable if s.decision != "pending"]
    if state.orientation_run_id and len(already) == len(actionable):
        return {
            "status": "already_consented",
            "run_id": state.orientation_run_id,
            "recorded": {s.id: s.requested_scope or "denied" for s in actionable},
            "unsupported": unsupported,
            "needs_human": False,
            "next": "export",
        }

    answered = {sid: v for sid, v in answers.items() if sid in {s.id for s in actionable}}
    missing = [s.id for s in actionable if s.id not in answered]
    if missing:
        state.save()
        return {
            "status": "needs_human",
            "needs_human": True,
            "question": "Which sources may be imported, and at what scope?",
            "awaiting": missing,
            "options": ["content", "metadata", "denied"],
            "unsupported": unsupported,
            "detail": "Consent is not inferred from silence; every importable source needs an answer.",
        }

    invalid = {
        sid: v for sid, v in answered.items() if v not in {"content", "metadata", "denied"}
    }
    if invalid:
        state.save()
        return {
            "status": "invalid_answer",
            "detail": f"unknown scope(s): {invalid}; expected content, metadata or denied",
            "needs_human": True,
            "next": "consent",
        }

    sources = [
        {
            "source_id": s.id,
            "kind": ORIENTATION_KIND[s.id],
            "locator": s.path,
            "scope": answered[s.id] if answered[s.id] != "denied" else "metadata",
        }
        for s in actionable
    ]

    if state.orientation_run_id:
        run_id = state.orientation_run_id
        try:
            load_run(resolved_paths, run_id)
        except FileNotFoundError:
            created = create_run(resolved_paths, sources)
            run_id = created["run_id"]
            state.orientation_run_id = run_id
    else:
        created = create_run(resolved_paths, sources)
        run_id = created["run_id"]
        state.orientation_run_id = run_id

    recorded = {}
    for s in actionable:
        decision = "approved" if answered[s.id] != "denied" else "rejected"
        try:
            set_consent(resolved_paths, run_id, s.id, decision)
        except ValueError:
            # Already decided in the manifest — reconcile rather than fail.
            run = load_run(resolved_paths, run_id)
            entry = next(
                (x for x in run.get("sources", []) if x.get("source_id") == s.id), None
            )
            if entry is None:
                raise
            decision = str(entry.get("consent", decision))
        s.requested_scope = None if answered[s.id] == "denied" else answered[s.id]
        s.decision = decision
        recorded[s.id] = answered[s.id]

    state.save()
    return {
        "status": "consented",
        "run_id": run_id,
        "recorded": recorded,
        "unsupported": unsupported,
        "needs_human": False,
        "next": "export",
    }


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def run_export(state: OnboardingState, *, paths=None) -> dict:
    """Materialise approved content into a readable transcript.

    Content-scoped sources are exported; metadata-scoped ones have nothing to
    export by definition, so the step is a no-op for them rather than an error.
    The export module re-checks consent against the orientation run, so this
    cannot be driven past a decision the user did not make.
    """
    from agent_diary.onboarding_export import export_hermes_history

    resolved_paths = paths or paths_for(state)
    run_id = state.orientation_run_id

    content_sources = [
        s for s in state.sources if s.detected and s.content_access_granted
    ]
    metadata_only = [
        s
        for s in state.sources
        if s.detected and s.decision == "approved" and s.requested_scope == "metadata"
    ]

    if not content_sources:
        return {
            "status": "nothing_to_export",
            "detail": (
                "No source was approved for content, so there is nothing to export. "
                "Metadata-only sources are imported without a transcript."
            ),
            "metadata_only": [s.id for s in metadata_only],
            "needs_human": False,
            "next": "preview",
        }

    if not run_id:
        raise OnboardingError(
            "consent recorded content access but no orientation run exists; "
            "re-run the consent step"
        )

    results = []
    for source in content_sources:
        outcome = export_hermes_history(resolved_paths, run_id, source.id)
        source.exported_path = outcome.get("exported_path")
        source.format_name = outcome.get("format_name")
        results.append(outcome)

    exported = [r for r in results if r.get("status") == "exported"]
    state.save()

    return {
        "status": "exported" if exported else "nothing_new",
        "run_id": run_id,
        "sources": results,
        "messages_exported": sum(r.get("messages_exported", 0) for r in results),
        "sessions_exported": sum(r.get("sessions_exported", 0) for r in results),
        "metadata_only": [s.id for s in metadata_only],
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
        action["command"] = "agent-diary --json onboard --step discover"

    elif step == "consent":
        pending = state.pending_human_questions()
        action["command"] = "agent-diary --json onboard --step consent --answer '<json>'"
        if pending:
            action["needs_human"] = True
            action["question"] = "Which sources may be imported, and at what scope?"
            action["sources"] = pending
            action["options"] = ["content", "metadata", "denied"]
            action["hint"] = "An agent must never choose a scope on the user's behalf."
        else:
            action["blockers"].append("no sources remain pending a decision")

    elif step == "export":
        action["command"] = "agent-diary --json onboard --step export"
        if not any(s.content_access_granted for s in state.sources if s.detected):
            action["blockers"].append(
                "no source has content scope, so export has nothing to do"
            )

    elif step == "preview":
        action["command"] = "agent-diary --json onboard --step preview --confirm"

    elif step == "import":
        action["command"] = "agent-diary --json onboard --step import"

    elif step == "review":
        action["command"] = "agent-diary --json onboard --step review"
        proposed = state.candidates.get("proposed", 0)
        if proposed:
            action["needs_human"] = True
            action["question"] = f"Approve {proposed} derived fact(s) into knowledge?"
            action["candidates"] = dict(state.candidates)

    elif step == "connect":
        action["command"] = "agent-diary --json onboard --step connect"

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
                "decision": s.decision,
                "requested_scope": s.requested_scope,
                "content_access_granted": s.content_access_granted,
            }
            for s in state.sources
        ],
        "imported_entries": state.imported_entries,
        "candidates": dict(state.candidates),
        "agent_connected": state.agent_connected,
        "action": next_action(state),
    }
