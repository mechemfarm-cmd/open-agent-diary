"""Persisted onboarding state.

One state machine, two front-ends: the human walkthrough and an agent's JSON
interface both read and write this file. Steps are validated so an out-of-order
jump fails loudly instead of quietly corrupting a run, and every write is
atomic so a crash can never leave a half-written state behind.

State lives inside the data root, not in a global location: the onboarding run
belongs to a particular diary, and moving the data root should move its state.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path

VERSION = 1

STEPS = ("init", "discover", "consent", "preview", "import", "review", "connect", "done")

# Legal forward transitions. Kept explicit rather than computed so that adding
# a step forces a deliberate decision about where it sits in the order.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "init": ("discover",),
    "discover": ("consent",),
    "consent": ("preview",),
    "preview": ("import",),
    "import": ("review",),
    "review": ("connect",),
    "connect": ("done",),
    "done": (),
}

CONSENT_CHOICES = ("pending", "metadata", "content", "denied", "imported")


class OnboardingError(RuntimeError):
    """Raised for illegal transitions or unreadable state."""


@dataclass
class SourceState:
    """A place history might live. Detection is metadata only."""

    id: str
    kind: str
    path: str = ""
    detected: bool = False
    sessions: int = 0
    oldest: str | None = None
    newest: str | None = None
    consent: str = "pending"

    def __post_init__(self) -> None:
        if self.consent not in CONSENT_CHOICES:
            raise OnboardingError(
                f"invalid consent {self.consent!r} for source {self.id!r}; "
                f"expected one of {', '.join(CONSENT_CHOICES)}"
            )
        if self.consent in {"metadata", "content"} and not self.detected:
            raise OnboardingError(
                f"source {self.id!r} cannot be consented: it was not detected"
            )


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
        sources = [SourceState(**s) for s in payload.get("sources", [])]
        return cls(
            data_root=payload.get("data_root") or str(Path(data_root).expanduser().resolve()),
            step=payload.get("step", "init"),
            completed=list(payload.get("completed", [])),
            sources=sources,
            orientation_run_id=payload.get("orientation_run_id"),
            imported_entries=int(payload.get("imported_entries", 0)),
            candidates=payload.get("candidates")
            or {"proposed": 0, "approved": 0, "rejected": 0},
            agent_connected=bool(payload.get("agent_connected", False)),
        )

    def save(self) -> Path:
        """Write atomically: a crash must never truncate the state."""
        path = self.path_for(self.data_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
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

    def source(self, source_id: str) -> SourceState:
        for s in self.sources:
            if s.id == source_id:
                return s
        raise OnboardingError(f"unknown source {source_id!r}")

    def pending_human_questions(self) -> list[str]:
        """Sources still awaiting a scope decision."""
        return [s.id for s in self.sources if s.detected and s.consent == "pending"]

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict:
        data = asdict(self)
        data["version"] = VERSION
        return data
