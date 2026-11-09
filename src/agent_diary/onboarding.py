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

import hashlib
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


def _export_digest(export_path: str | None) -> str:
    """A short, content-derived identity for one export transcript.

    Import ids must change when a new export generation lands, otherwise the
    second import reuses the first id and the new sessions never arrive. They
    must also stay the same for identical content, so re-running import on the
    same transcript is still a no-op.
    """
    if not export_path:
        return "none"
    path = Path(export_path)
    if not path.is_file():
        return "missing"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:12]


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
# preview
# ---------------------------------------------------------------------------


def run_preview(
    state: OnboardingState,
    *,
    confirm: bool = False,
    samples: int = 3,
    paths=None,
) -> dict:
    """Show a real sample of what would be imported, then wait for confirmation.

    Nothing is written. Without ``confirm`` the step stays put, so an agent
    cannot advance past the one moment the user is asked to look at actual
    content before it lands.
    """
    from agent_diary.orientation_preview import preview_approved_source

    resolved_paths = paths or paths_for(state)
    content_sources = [s for s in state.sources if s.detected and s.content_access_granted]
    metadata_only = [
        s
        for s in state.sources
        if s.detected and s.decision == "approved" and s.requested_scope == "metadata"
    ]

    previews = []
    for source in content_sources:
        format_name = source.format_name or EXPORT_FORMAT
        previews.append(
            preview_approved_source(
                resolved_paths,
                state.orientation_run_id,
                source.id,
                format_name=format_name,
                max_messages=samples,
            )
        )

    if not content_sources and not metadata_only:
        return {
            "status": "nothing_to_preview",
            "detail": "No source was approved, so there is nothing to import.",
            "needs_human": False,
            "next": "import",
        }

    payload = {
        "status": "confirmed" if confirm else "preview_ready",
        "samples": [p.get("samples", []) for p in previews],
        "counts": [
            {
                "source_id": p["source_id"],
                "message_count": p["message_count"],
                "author_role_counts": p.get("author_role_counts"),
                "created_at_range": p.get("created_at_range"),
            }
            for p in previews
        ],
        "metadata_only": [
            {
                "source_id": s.id,
                "detail": "Metadata only: session count and date range will be recorded, not content.",
            }
            for s in metadata_only
        ],
        "needs_human": not confirm,
        "next": "import",
    }
    if metadata_only and not content_sources:
        payload["detail"] = (
            "No content was approved, so there is nothing to display. Confirming "
            "proceeds with metadata only: session counts and date ranges are recorded, "
            "no message text is imported."
        )
    if not confirm:
        payload["question"] = (
            "Import the history shown above? Re-run this step with --confirm to import."
        )
        payload["command"] = "agent-diary --json onboard --step preview --confirm"
    return payload


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


def run_import(state: OnboardingState, *, paths=None, dry_run: bool = False) -> dict:
    """Import the approved history, reporting honest counts.

    Idempotent: the import id is derived from the run and source, so a repeat
    imports nothing new and says so. A failure is recorded and the state does
    not advance — never report success for a partial import.
    """
    from agent_diary.orientation_import import import_approved_history

    resolved_paths = paths or paths_for(state)
    content_sources = [s for s in state.sources if s.detected and s.content_access_granted]
    metadata_only = [
        s
        for s in state.sources
        if s.detected and s.decision == "approved" and s.requested_scope == "metadata"
    ]

    if not content_sources:
        return {
            "status": "nothing_to_import",
            "detail": (
                "No source was approved for content. Metadata-only sources are "
                "recorded by their export metadata, not imported as entries."
            ),
            "metadata_only": [s.id for s in metadata_only],
            "imported_count": 0,
            "skipped_count": 0,
            "failures": [],
            "needs_human": False,
            "next": "review",
        }

    results = []
    failures = []
    for source in content_sources:
        try:
            outcome = import_approved_history(
                resolved_paths,
                run_id=state.orientation_run_id,
                source_id=source.id,
                format_name=source.format_name or EXPORT_FORMAT,
                import_id=(
                    f"onboarding-{state.orientation_run_id}-{source.id}"
                    f"-{_export_digest(source.exported_path)}"
                ),
                dry_run=dry_run,
            )
        except Exception as exc:  # recorded, never swallowed
            failures.append({"source_id": source.id, "error": f"{type(exc).__name__}: {exc}"})
            continue
        source.imported_entries = int(outcome.get("imported_count", 0))
        results.append(outcome)

    imported = sum(int(r.get("imported_count", 0)) for r in results)
    skipped = sum(int(r.get("skipped_count", 0)) for r in results)
    if not dry_run:
        # Cumulative across export generations. Overwriting would report 0 after
        # an idempotent re-run and make status lie about what is in the diary.
        state.imported_entries += imported
    state.save()

    payload = {
        "status": "imported" if not failures else "partial_failure",
        "run_id": state.orientation_run_id,
        "sources": results,
        "imported_count": imported,
        "skipped_count": skipped,
        "failures": failures,
        "metadata_only": [s.id for s in metadata_only],
        "needs_human": False,
        "next": "review",
    }
    if failures:
        payload["detail"] = (
            "Some sources failed to import. The step has not advanced; "
            "fix the cause and re-run import."
        )
    return payload


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


def run_review(
    state: OnboardingState,
    *,
    approve: list[str] | None = None,
    reject: list[str] | None = None,
    auto_reject_unreviewed: bool = False,
    paths=None,
) -> dict:
    """Turn derived candidates into knowledge only by explicit approval.

    Generation is deterministic and repeatable; approval is not. A candidate
    that nobody has looked at stays pending, and choosing to reject everything
    unreviewed is a legitimate outcome — an empty knowledge base is correct
    until a person says otherwise.
    """
    from agent_diary.orientation_candidates import (
        generate_candidates,
        list_candidates,
        set_candidate_status,
    )

    resolved_paths = paths or paths_for(state)
    content_sources = [s for s in state.sources if s.detected and s.content_access_granted]

    if not state.orientation_run_id or not content_sources:
        # Without a run there are no candidates. Passing None into the candidate
        # layer creates a "None.json" file for a run that does not exist.
        return {
            "status": "nothing_to_review",
            "detail": "No orientation run exists, so there are no derived facts to review.",
            "counts": {"proposed": 0, "approved": 0, "rejected": 0, "pending": 0},
            "needs_human": False,
            "next": "connect",
        }

    generated = []
    failures = []
    for source in content_sources:
        try:
            generated.append(
                generate_candidates(
                    resolved_paths, run_id=state.orientation_run_id, source_id=source.id
                )
            )
        except Exception as exc:
            failures.append({"source_id": source.id, "error": f"{type(exc).__name__}: {exc}"})

    pending = list_candidates(resolved_paths, state.orientation_run_id, status="pending")
    decided = []

    for candidate_id in approve or []:
        decided.append(
            set_candidate_status(resolved_paths, state.orientation_run_id, candidate_id, "approved")
        )
    for candidate_id in reject or []:
        decided.append(
            set_candidate_status(resolved_paths, state.orientation_run_id, candidate_id, "rejected")
        )

    if auto_reject_unreviewed:
        for item in pending:
            decided.append(
                set_candidate_status(
                    resolved_paths, state.orientation_run_id, item["candidate_id"], "rejected"
                )
            )

    all_candidates = list_candidates(resolved_paths, state.orientation_run_id)
    counts = {
        "proposed": len(all_candidates),
        "approved": sum(1 for c in all_candidates if c.get("status") == "approved"),
        "rejected": sum(1 for c in all_candidates if c.get("status") == "rejected"),
        "pending": sum(1 for c in all_candidates if c.get("status") == "pending"),
    }
    state.candidates = {
        "proposed": counts["proposed"],
        "approved": counts["approved"],
        "rejected": counts["rejected"],
    }
    state.save()

    remaining = counts["pending"]
    payload = {
        "status": "reviewed" if remaining == 0 else "needs_review",
        "run_id": state.orientation_run_id,
        "counts": counts,
        "decided": [c.get("candidate_id") for c in decided],
        "pending": [
            {
                "candidate_id": c.get("candidate_id"),
                "kind": c.get("kind"),
                "statement": c.get("statement"),
                "confidence": c.get("confidence"),
                "source_entry_ids": c.get("source_entry_ids", []),
            }
            for c in pending
            if c.get("candidate_id") not in set(approve or []) | set(reject or [])
        ][:50],
        "failures": failures,
        "needs_human": remaining > 0 and not auto_reject_unreviewed,
        "next": "connect",
    }
    if payload["needs_human"]:
        payload["question"] = (
            f"{remaining} derived fact(s) await review. Approve, reject, or decline all."
        )
        payload["command"] = (
            "agent-diary --json onboard --step review --approve <id> --reject <id> "
            "  (or --auto-reject-unreviewed to import no derived knowledge)"
        )
    return payload


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


def run_connect(state: OnboardingState, *, dry_run: bool = False) -> dict:
    """Point the detected agent runtime at this diary.

    Writing an agent's config is the most invasive step, so it is also the one
    that backs up and verifies. An already-correct entry is left untouched.
    """
    from agent_diary.onboarding_connect import connect_hermes, hermes_config_path

    config = hermes_config_path()
    if not config.parent.is_dir():
        return {
            "status": "no_agent_detected",
            "detail": (
                f"No Hermes installation found at {config.parent}. The diary is ready; "
                "configure your agent's MCP host manually using the entry below."
            ),
            "entry": {"command": "python3", "args": ["<repo>/agent-diary-mcp.py"]},
            "http_equivalent": {"url": "http://127.0.0.1:8041", "auth": "none (loopback)"},
            "needs_human": True,
            "next": "done",
        }

    try:
        out = connect_hermes(config_path=config, dry_run=dry_run)
    except Exception as exc:  # a broken config must not crash onboarding
        from agent_diary.onboarding_connect import ConnectError

        if not isinstance(exc, ConnectError):
            raise
        return {
            "status": "connect_failed",
            "detail": str(exc),
            "config_path": str(config),
            "changed": False,
            "needs_human": True,
            "next": "connect",
        }
    if out.get("status") in {"connected", "already_connected"}:
        state.agent_connected = True
        state.save()
    return out


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
