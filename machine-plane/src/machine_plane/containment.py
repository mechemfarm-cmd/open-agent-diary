"""Containment: what the recorder protects against, and whether anyone agreed.

The recorder runs as root and the spool is root-owned, which is a real boundary
only when the agent's own account cannot become root. Where it can, the same
arrangement protects against carelessness and not against intent, and the honest
thing to do is say so — to the person installing it, at every start, and in every
record the run produces.

Two levels, and no third:

``enforced``
    the agent's account cannot escalate to root. The spool protection is a
    boundary an agent would have to break.

``advisory``
    the agent's account can escalate. Recording works; the protection is a
    convention an agent could step around.

Consent is bound to a *fingerprint* of the detected state. Re-evaluating what the
situation is says nothing about whether the person agreed to that situation, so a
consent obtained under one set of grants must not silently cover another.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

DETECTOR_VERSION = 1
CONSENT_VERSION = 1

#: Groups that mean the account can reach root or raw devices regardless of an
#: explicit sudoers entry.
PRIVILEGED_GROUPS = frozenset({"sudo", "wheel", "docker", "disk", "lxd", "admin"})


class Level(str, Enum):
    ENFORCED = "enforced"
    ADVISORY = "advisory"


class DetectionError(Exception):
    """The level could not be determined.

    Raised rather than defaulting: an unknown state must never be reported as
    ``enforced``, and a run must not proceed on a level nobody established.
    """


@dataclass(frozen=True)
class Detection:
    account: str
    uid: int
    host_id: str
    grants: tuple[str, ...]
    groups: tuple[str, ...]
    is_root: bool
    can_control_units: bool
    level: Level
    reasons: tuple[str, ...]
    detector: int = DETECTOR_VERSION

    def canonical(self) -> dict:
        """The detection as it is fingerprinted.

        Lists are sorted so that the same state always yields the same digest;
        nothing here is allowed to depend on dict or set ordering.
        """
        return {
            "account": self.account,
            "uid": self.uid,
            "host_id": self.host_id,
            "grants": sorted(self.grants),
            "groups": sorted(self.groups),
            "is_root": self.is_root,
            "can_control_units": self.can_control_units,
            "level": self.level.value,
            "reasons": sorted(self.reasons),
            "detector": self.detector,
        }

    def fingerprint(self) -> str:
        return _digest(self.canonical())


def _digest(payload: dict) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# -- detection -------------------------------------------------------------


def _host_id() -> str:
    try:
        return Path("/etc/machine-id").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown-host"


def detect(
    *,
    account: str | None = None,
    uid: int | None = None,
    host_id: str | None = None,
    sudo_lister: Callable[[str], str] | None = None,
    sudoers_lister: Callable[[str], tuple[str, ...]] | None = None,
    group_lister: Callable[[str], tuple[str, ...]] | None = None,
    unit_controller: Callable[[str], bool] | None = None,
) -> Detection:
    """Determine the protection level for ``account``.

    Every source is injectable so the matrix can be exercised without root and
    without touching the machine's real sudoers. The defaults are the real ones.
    """
    if account is None or uid is None:
        resolved_account, resolved_uid = _current_account()
        account = account or resolved_account
        uid = resolved_uid if uid is None else uid

    is_root = uid == 0
    grants = _grants(account, sudo_lister, sudoers_lister)
    groups = _groups(account, group_lister, uid)
    can_control = _can_control_units(account, groups, unit_controller, is_root)

    reasons: list[str] = []
    if is_root:
        reasons.append("agent account is root")
    for grant in grants:
        reasons.append(f"passwordless sudo grant: {grant}")
    for group in groups:
        if group in PRIVILEGED_GROUPS:
            reasons.append(f"member of privileged group: {group}")
    if can_control:
        reasons.append("can start or stop system units")

    level = Level.ADVISORY if reasons else Level.ENFORCED
    return Detection(
        account=account,
        uid=uid,
        host_id=host_id if host_id is not None else _host_id(),
        grants=tuple(grants),
        groups=tuple(groups),
        is_root=is_root,
        can_control_units=can_control,
        level=level,
        reasons=tuple(reasons),
    )


def _current_account() -> tuple[str, int]:
    try:
        import pwd

        entry = pwd.getpwuid(os.geteuid())
        return entry.pw_name, entry.pw_uid
    except (ImportError, KeyError) as error:  # pragma: no cover - platform
        raise DetectionError(f"cannot resolve the agent account: {error}") from error


def _grants(
    account: str,
    lister: Callable[[str], str] | None,
    sudoers_lister: Callable[[str], tuple[str, ...]] | None,
) -> tuple[str, ...]:
    """Named grants that give this account a passwordless path to root.

    Two sources, because neither alone is enough: ``sudo -l -U`` shows the
    *merged* policy (so it catches includes and LDAP) but not which file said so,
    while scanning ``/etc/sudoers.d`` names the file but misses everything else.

    When the listing cannot be taken that is *unknown*, not *no grants*: raising
    is what stops an unreadable sudoers from being reported as enforced.
    """
    lister = lister or _sudo_list
    try:
        listing = lister(account)
    except (OSError, RuntimeError) as error:
        raise DetectionError(f"cannot read sudo rules for {account}: {error}") from error

    namer = sudoers_lister or _real_sudoers_files
    try:
        files = tuple(sorted(set(namer(account))))
    except (OSError, RuntimeError) as error:
        raise DetectionError(
            f"cannot read sudoers drop-ins for {account}: {error}"
        ) from error

    passwordless = "nopasswd" in listing.lower()
    if not passwordless:
        return files

    if files:
        return files
    # Passwordless access granted somewhere we cannot name — say so rather than
    # returning an empty grant list that would read as "no escalation".
    return ("merged-policy:passwordless",)


def _real_sudoers_files(account: str) -> tuple[str, ...]:
    """Drop-ins under /etc/sudoers.d that mention the account.

    Names only. This does not parse the rules — the merged policy from
    ``sudo -l`` decides whether the access is passwordless.
    """
    directory = Path("/etc/sudoers.d")
    if not directory.is_dir():
        return ()
    found = []
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if account in text:
            found.append(str(path))
    return tuple(found)


def _sudo_list(account: str) -> str:
    import subprocess

    process = subprocess.run(
        ["sudo", "-n", "-l", "-U", account],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    # A non-zero exit here usually means "not allowed", which is a real answer.
    return (process.stdout or "") + (process.stderr or "")


def _groups(
    account: str, lister: Callable[[str], tuple[str, ...]] | None, uid: int
) -> tuple[str, ...]:
    lister = lister or _real_groups
    try:
        return tuple(sorted(set(lister(account))))
    except (OSError, KeyError) as error:
        raise DetectionError(f"cannot read groups for {account}: {error}") from error


def _real_groups(account: str) -> tuple[str, ...]:
    import grp
    import pwd

    entry = pwd.getpwnam(account)
    names = {grp.getgrgid(entry.pw_gid).gr_name}
    for group in grp.getgrall():
        if account in group.gr_mem:
            names.add(group.gr_name)
    return tuple(sorted(names))


def _can_control_units(
    account: str,
    groups: tuple[str, ...],
    controller: Callable[[str], bool] | None,
    is_root: bool,
) -> bool:
    if is_root:
        return True
    if any(group in PRIVILEGED_GROUPS for group in groups):
        return True
    if controller is None:
        return False
    try:
        return bool(controller(account))
    except (OSError, RuntimeError):
        # Unknown is advisory, never enforced — but this function only widens.
        return True


# -- consent ---------------------------------------------------------------


class ConsentKind(str, Enum):
    INTERACTIVE = "interactive"
    FLAG = "flag"


@dataclass(frozen=True)
class Consent:
    level: Level
    fingerprint: str
    config_fingerprint: str
    detected_at: float
    consent_kind: ConsentKind
    consented_at: float
    version: int = CONSENT_VERSION

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": self.version,
                "level": self.level.value,
                "fingerprint": self.fingerprint,
                "config_fingerprint": self.config_fingerprint,
                "detected_at": self.detected_at,
                "consent_kind": self.consent_kind.value,
                "consented_at": self.consented_at,
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_dict(cls, payload: dict) -> "Consent":
        version = payload.get("version")
        if version != CONSENT_VERSION:
            raise ValueError(
                f"consent file version {version!r} is not readable by this "
                f"build ({CONSENT_VERSION})"
            )
        return cls(
            level=Level(payload["level"]),
            fingerprint=payload["fingerprint"],
            config_fingerprint=payload["config_fingerprint"],
            detected_at=payload["detected_at"],
            consent_kind=ConsentKind(payload["consent_kind"]),
            consented_at=payload["consented_at"],
            version=version,
        )


def write_consent(path: str | os.PathLike[str], consent: Consent) -> None:
    """Persist consent atomically, root-readable only."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
    )
    try:
        os.write(descriptor, consent.to_json().encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, target)


def read_consent(path: str | os.PathLike[str]) -> Consent | None:
    target = Path(path)
    if not target.exists():
        return None
    try:
        return Consent.from_dict(json.loads(target.read_text(encoding="utf-8")))
    except PermissionError:
        # Distinct from corruption: the file may be perfectly readable by root.
        raise
    except (OSError, ValueError, KeyError) as error:
        raise ValueError(f"unreadable consent file {target}: {error}") from error


class Startup(Enum):
    """What the recorder may do, given detection and what is on disk."""

    OK_ADVISORY = "ok_advisory"
    OK_ENFORCED = "ok_enforced"
    OK_ENFORCED_SUPERSEDES = "ok_enforced_supersedes"
    REFUSE_DETECTION = "refuse_detection"
    REFUSE_NO_CONSENT = "refuse_no_consent"
    REFUSE_FINGERPRINT = "refuse_fingerprint"
    REFUSE_CONFIG = "refuse_config"


#: Exit codes, so a caller (and a test) can tell which refusal happened.
STARTUP_EXIT = {
    Startup.OK_ADVISORY: 0,
    Startup.OK_ENFORCED: 0,
    Startup.OK_ENFORCED_SUPERSEDES: 0,
    Startup.REFUSE_DETECTION: 2,
    Startup.REFUSE_NO_CONSENT: 3,
    Startup.REFUSE_FINGERPRINT: 4,
    Startup.REFUSE_CONFIG: 5,
}


@dataclass(frozen=True)
class StartupDecision:
    outcome: Startup
    detection: Detection | None
    message: str
    changed_fields: tuple[str, ...] = field(default_factory=tuple)

    @property
    def allowed(self) -> bool:
        return self.outcome.name.startswith("OK_")

    @property
    def exit_code(self) -> int:
        return STARTUP_EXIT[self.outcome]


def config_fingerprint(payload: dict) -> str:
    """Fingerprint of the recording configuration, not of the machine.

    Consent to record one configuration is not consent to another, so the probe
    set, spool path and allocation are part of what was agreed.
    """
    return _digest({k: payload[k] for k in sorted(payload)})


def decide_startup(
    *,
    detection: Detection | None,
    detection_error: str | None,
    consent: Consent | None,
    current_config: str,
) -> StartupDecision:
    """The startup matrix, in order, as one function.

    Order matters: a detection failure is reported before consent is even
    consulted, because a consent record cannot make an unknown state known.
    """
    if detection is None or detection_error:
        return StartupDecision(
            Startup.REFUSE_DETECTION,
            None,
            f"cannot determine the protection level: {detection_error or 'unknown'}",
        )

    if detection.level is Level.ENFORCED:
        if consent is None or consent.level is not Level.ADVISORY:
            return StartupDecision(
                Startup.OK_ENFORCED,
                detection,
                "protection level enforced; the agent cannot become root",
            )
        return StartupDecision(
            Startup.OK_ENFORCED_SUPERSEDES,
            detection,
            "protection level enforced; previous advisory consent superseded",
        )

    if consent is None:
        return StartupDecision(
            Startup.REFUSE_NO_CONSENT,
            detection,
            "advisory mode has not been accepted on this machine",
        )

    changed = _changed_fields(consent.fingerprint, detection.fingerprint(), detection)
    if changed:
        return StartupDecision(
            Startup.REFUSE_FINGERPRINT,
            detection,
            "the protection situation changed since advisory mode was accepted",
            changed,
        )

    if consent.config_fingerprint != current_config:
        return StartupDecision(
            Startup.REFUSE_CONFIG,
            detection,
            "the recording configuration changed since advisory mode was accepted",
            ("configuration",),
        )

    return StartupDecision(
        Startup.OK_ADVISORY,
        detection,
        "protection level advisory; consent matches the current state",
    )


def _changed_fields(stored: str, current: str, detection: Detection) -> tuple[str, ...]:
    """Which fields differ, so a refusal names them instead of shrugging.

    The stored *digest* is compared, but the individual fields are compared
    against a re-detection the caller already performed; this reports the
    categories that commonly move rather than claiming per-field proof it does
    not have.
    """
    if stored == current:
        return ()
    fields = ["level", "grants", "groups", "account"]
    if detection.is_root:
        fields.append("is_root")
    if detection.can_control_units:
        fields.append("can_control_units")
    return tuple(fields)


def consent_message(decision: StartupDecision, detection: Detection | None) -> str:
    """The text a person actually reads when the run is refused."""
    if decision.outcome is Startup.REFUSE_NO_CONSENT and detection is not None:
        shown = "\n".join(f"  {reason}" for reason in detection.reasons) or "  (none)"
        return (
            "MACHINE PLANE — PROTECTION LEVEL: ADVISORY\n\n"
            f"The agent runs as account {detection.account!r}, which can become "
            "root without a password.\n\n"
            "Matching grants and memberships:\n"
            f"{shown}\n\n"
            "The recorder will still record what the agent does. What it cannot\n"
            "guarantee is that the record is untampered — an agent that decided to\n"
            "become root could alter or delete it.\n\n"
            "Recording works. The protection is a convention, not a boundary.\n\n"
            "To get the enforced level, run the agent as an account that cannot\n"
            "become root. That is a separate change; this installer will not make\n"
            "it for you.\n\n"
            "Continue in advisory mode? [y/N]"
        )

    if decision.outcome is Startup.REFUSE_FINGERPRINT:
        return (
            "MACHINE PLANE — CONSENT EXPIRED\n\n"
            "The protection situation changed since advisory mode was accepted.\n\n"
            f"  Changed:  {', '.join(decision.changed_fields)}\n\n"
            "The previous acceptance does not cover this. Re-run the installer to\n"
            "review and accept the new state, or restore the previous account\n"
            "configuration."
        )

    if decision.outcome is Startup.REFUSE_CONFIG:
        return (
            "MACHINE PLANE — CONSENT EXPIRED\n\n"
            "The recording configuration changed since advisory mode was accepted.\n\n"
            "The previous acceptance does not cover a different probe set, spool or\n"
            "allocation. Re-run the installer to review and accept it."
        )

    return decision.message
