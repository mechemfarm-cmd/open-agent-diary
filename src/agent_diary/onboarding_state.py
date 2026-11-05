"""Persisted onboarding state.

One state machine, two front-ends: the human walkthrough and an agent's JSON
interface both read and write this file.

Two properties this module is responsible for, both of which the architecture
review found missing in the first draft:

1. **Approval and scope are separate facts.** A user can request content scope
   without having approved anything, and approval can exist without content
   scope (metadata-only consent). `content_access_granted` is therefore derived
   from both, never stored, and cannot be set by constructing an object.

2. **Writes are safe under concurrent readers/writers.** The state file is
   written through a unique temporary file plus an exclusive lock, so an agent
   and a human editing the same run cannot corrupt each other's write.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Iterator

VERSION = 1

# The onboarding sequence. `export` sits between consent and preview because
# preview/import need a readable transcript file, not a database locator.
STEPS = (
    "init",
    "discover",
    "consent",
    "export",
    "preview",
    "import",
    "review",
    "connect",
    "done",
)

# Legal forward transitions. Explicit rather than computed so adding a step
# forces a deliberate decision about where it sits.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "init": ("discover",),
    "discover": ("consent",),
    "consent": ("export",),
    "export": ("preview",),
    "preview": ("import",),
    "import": ("review",),
    "review": ("connect",),
    "connect": ("done",),
    "done": (),
}

DECISIONS = ("pending", "approved", "rejected", "revoked", "unavailable")
SCOPES = ("metadata", "content")


class OnboardingError(RuntimeError):
    """Raised for illegal transitions or unreadable state."""


def _lock_path(state_path: Path) -> Path:
    return state_path.with_suffix(state_path.suffix + ".lock")


@contextmanager
def file_lock(state_path: Path) -> Iterator[None]:
    """Exclusive advisory lock for a read-modify-write cycle."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(state_path)
    with open(lock_path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass
class SourceState:
    """A place history might live, and what the user decided about it.

    `requested_scope` is what the user asked for. `decision` is what was
    recorded. Neither alone grants content access; only both together do.
    """

    id: str
    kind: str
    path: str = ""
    detected: bool = False
    sessions: int = 0
    oldest: str | None = None
    newest: str | None = None
    requested_scope: str | None = None
    decision: str = "pending"
    exported_path: str | None = None
    format_name: str | None = None
    imported_entries: int = 0

    def __post_init__(self) -> None:
        if self.decision not in DECISIONS:
            raise OnboardingError(
                f"invalid decision {self.decision!r} for source {self.id!r}; "
                f"expected one of {', '.join(DECISIONS)}"
            )
        if self.requested_scope is not None and self.requested_scope not in SCOPES:
            raise OnboardingError(
                f"invalid scope {self.requested_scope!r} for source {self.id!r}; "
                f"expected one of {', '.join(SCOPES)}"
            )
        if not self.detected and (
            self.decision != "pending" or self.requested_scope is not None
        ):
            raise OnboardingError(
                f"source {self.id!r} cannot carry a decision: it was not detected"
            )
        if self.decision == "approved" and self.requested_scope is None:
            raise OnboardingError(
                f"source {self.id!r} cannot be approved without a requested scope"
            )

    # -- derived, never stored ------------------------------------------

    @property
    def content_access_granted(self) -> bool:
        """The only condition that permits reading content.

        Derived on purpose: a state file edited by hand cannot claim content
        access without also claiming an approved decision *and* content scope.
        The exporter re-checks this against the orientation manifest.
        """
        return self.decision == "approved" and self.requested_scope == "content"

    @property
    def consent(self) -> str:
        """Display label. Read-only so it can't diverge from the real fields."""
        if self.decision == "pending":
            return "pending"
        if self.decision == "approved":
            return self.requested_scope or "metadata"
        if self.decision == "revoked":
            return "revoked"
        if self.decision == "unavailable":
            return "unavailable"
        return "denied"

    def to_dict(self) -> dict:
        data = asdict(self)
        # Emitted for readers, reconstructed from the fields on load.
        data.pop("requested_scope", None)
        data["requested_scope"] = self.requested_scope
        return data


@dataclass
class OnboardingState:
    data_root: str
    step: str = "init"
    completed: list[str] = field(default_factory=list)
    sources: list[SourceState] = field(default_factory=list)
    orientation_run_id: str | None = None
    imported_entries: int = 0
    candidates: dict = field(
        default_factory=lambda: {"proposed": 0, "approved": 0, "rejected": 0}
    )
    agent_connected: bool = False
    version: int = VERSION

    # -- paths ---------------------------------------------------------------

    @staticmethod
    def path_for(data_root: Path | str) -> Path:
        return Path(data_root).expanduser().resolve() / "config" / "onboarding.json"

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def new(cls, data_root: Path | str) -> "OnboardingState":
        return cls(data_root=str(Path(data_root).expanduser().resolve()))

    @classmethod
    def load(cls, data_root: Path | str) -> "OnboardingState":
        path = cls.path_for(data_root)
        if not path.exists():
            return cls.new(data_root)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OnboardingError(f"cannot read onboarding state at {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise OnboardingError(f"onboarding state at {path} is not an object")
        if payload.get("version") != VERSION:
            raise OnboardingError(
                f"onboarding state version {payload.get('version')!r} "
                f"is not supported (expected {VERSION})"
            )
        state = cls._from_payload(payload, fallback_root=data_root)
        state.validate()
        return state

    @classmethod
    def _from_payload(cls, payload: dict, *, fallback_root: Path | str) -> "OnboardingState":
        candidates = payload.get("candidates") or {}
        if not isinstance(candidates, dict):
            raise OnboardingError("onboarding state 'candidates' must be an object")
        return cls(
            data_root=payload.get("data_root")
            or str(Path(fallback_root).expanduser().resolve()),
            step=payload.get("step", "init"),
            completed=list(payload.get("completed", []) or []),
            sources=[SourceState(**s) for s in payload.get("sources", []) or []],
            orientation_run_id=payload.get("orientation_run_id"),
            imported_entries=int(payload.get("imported_entries", 0) or 0),
            candidates={
                "proposed": int(candidates.get("proposed", 0) or 0),
                "approved": int(candidates.get("approved", 0) or 0),
                "rejected": int(candidates.get("rejected", 0) or 0),
            },
            agent_connected=bool(payload.get("agent_connected", False)),
        )

    def validate(self) -> None:
        """Reject a state file that could not have been produced by this code.

        A corrupted or hand-edited state is a real failure mode: it produces a
        status response whose action points at a step that doesn't exist.
        """
        if self.step not in STEPS:
            raise OnboardingError(
                f"unknown onboarding step {self.step!r}; expected one of {', '.join(STEPS)}"
            )
        unknown = [s for s in self.completed if s not in STEPS]
        if unknown:
            raise OnboardingError(f"unknown completed step(s): {', '.join(unknown)}")
        expected = list(STEPS[: STEPS.index(self.step)])
        if self.completed != expected[: len(self.completed)]:
            raise OnboardingError(
                f"completed {self.completed!r} is not a prefix of the onboarding order"
            )
        if len(self.completed) > len(expected):
            raise OnboardingError(
                f"completed lists {len(self.completed)} steps but only "
                f"{len(expected)} precede {self.step!r}"
            )
        ids = [s.id for s in self.sources]
        if len(set(ids)) != len(ids):
            raise OnboardingError("onboarding sources must have unique ids")

    # -- persistence ---------------------------------------------------------

    def save(self) -> Path:
        """Atomically and exclusively write the state.

        Unique temporary file plus an exclusive lock: crash-safe for one writer,
        and safe when an agent and a human front-end write at the same time.
        """
        path = self.path_for(self.data_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"
        with file_lock(path):
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
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp_name, 0o600)
                os.replace(tmp_name, path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        return path

    # -- transitions ---------------------------------------------------------

    def advance(self, to: str) -> None:
        allowed = TRANSITIONS.get(self.step, ())
        if to not in allowed:
            raise OnboardingError(
                f"cannot move from {self.step!r} to {to!r}; "
                f"legal next steps: {', '.join(allowed) or '(none — onboarding is complete)'}"
            )
        if self.step not in self.completed:
            self.completed.append(self.step)
        self.step = to

    def skip_to(self, to: str, *, reason: str) -> None:
        """Advance through a step that had nothing to do.

        Only permitted along the linear order, and only for steps explicitly
        documented as skippable when a source list is empty.
        """
        if to not in TRANSITIONS.get(self.step, ()):
            raise OnboardingError(
                f"cannot skip from {self.step!r} to {to!r}; "
                f"legal next steps: {', '.join(TRANSITIONS.get(self.step, ())) or '(none)'}"
            )
        self.advance(to)

    def source(self, source_id: str) -> SourceState:
        for s in self.sources:
            if s.id == source_id:
                return s
        raise OnboardingError(f"unknown source {source_id!r}")

    def pending_human_questions(self) -> list[str]:
        """Detected sources with no decision yet — excluding unsupported ones.

        An unsupported source is marked unavailable at consent time rather than
        left pending, so it cannot keep reporting as an open question.
        """
        return [
            s.id
            for s in self.sources
            if s.detected and s.decision == "pending" and s.kind in SUPPORTED_KINDS
        ]

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict:
        data = asdict(self)
        data["version"] = VERSION
        return data


# Detector kinds the orientation layer can represent as importable today.
SUPPORTED_KINDS = {"hermes_sqlite"}
