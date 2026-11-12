"""Redaction, applied by the producer before anything is written.

The metadata-only guarantee is only as good as *where* filtering happens. If a
platform event carries command lines, they must be discarded before the spool,
not filtered on read: a later filter is too late, because the content has
already been written to disk.

So this module is deliberately usable without importing the rest of the package,
and the producer pipeline calls it before normalisation is committed to storage.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from enum import Enum

#: Detail keys that carry content rather than shape. These are dropped
#: unconditionally in metadata-only mode. Several sensors expose them naturally
#: (Sysmon in particular), so this is an explicit stripping step rather than an
#: assumption about what a producer happens to collect.
FORBIDDEN_CONTENT_KEYS = frozenset(
    {
        "command_line",
        "cmdline",
        "argv",
        "args",
        "arguments",
        "content",
        "contents",
        "payload",
        "body",
        "prompt",
        "prompts",
        "response",
        "completion",
        "headers",
        "cookies",
        "authorization",
        "title",
        "text",
        "excerpt",
    }
)

#: Path fragments that are excluded by default, whatever the configured roots
#: say. A path is not automatically private, but these are private by
#: construction.
DEFAULT_EXCLUDED_FRAGMENTS = (
    "/.ssh/",
    "/.gnupg/",
    "/.aws/",
    "/.config/gh/",
    "/.config/gcloud/",
    "/.kube/",
    "/.docker/config.json",
    "/.netrc",
    "/.git-credentials",
    "/.password-store/",
    "/keyrings/",
    "/.mozilla/",
    "/.config/google-chrome/",
    "/.config/chromium/",
    "/Library/Application Support/Google/Chrome/",
    "/Library/Keychains/",
    "/AppData/Local/Google/Chrome/",
    "/AppData/Roaming/Mozilla/",
)


class PathDisplay(str, Enum):
    """How much of a path survives redaction."""

    FULL = "full"
    BASENAME = "basename"
    HASHED = "hashed"
    OMITTED = "omitted"


def coarsen_destination(target: str) -> str:
    """Reduce a network destination to host and port.

    Paths and query strings in a URL routinely carry tokens and identifiers.
    The metadata-only guarantee has to survive a producer that hands us a full
    URL, so we cut it down rather than trusting the sensor.

    Correlation applies this same function to an agent's declared target, so
    the two accounts are compared after identical treatment rather than one
    redacted against one raw.
    """
    if "://" in target:
        scheme, _, remainder = target.partition("://")
        authority = remainder.split("/", 1)[0]
        return f"{scheme}://{authority}"
    return target.split("/", 1)[0]


@dataclass(frozen=True)
class RedactionResult:
    value: str | None
    redacted: bool
    reason: str | None = None


@dataclass
class PrivacyPolicy:
    """The producer-side redaction policy.

    ``allowed_roots`` are the only places a full path may survive. Everything
    outside them is reduced according to ``path_display``. Credential and
    browser paths are excluded regardless.
    """

    allowed_roots: tuple[str, ...] = ()
    path_display: PathDisplay = PathDisplay.BASENAME
    hash_salt: bytes = b"agent-diary-machine-plane"
    excluded_fragments: tuple[str, ...] = DEFAULT_EXCLUDED_FRAGMENTS

    def _is_excluded(self, path: str) -> bool:
        lowered = path.lower()
        return any(fragment.lower() in lowered for fragment in self.excluded_fragments)

    def _within_allowed_root(self, path: str) -> bool:
        if not self.allowed_roots:
            return False
        resolved = os.path.abspath(os.path.expanduser(path))
        for root in self.allowed_roots:
            root_abs = os.path.abspath(os.path.expanduser(root))
            if resolved == root_abs or resolved.startswith(root_abs.rstrip("/") + "/"):
                return True
        return False

    def redact_path(self, path: str | None) -> RedactionResult:
        """Reduce a path to what the policy allows."""
        if path is None:
            return RedactionResult(None, False)

        if self._is_excluded(path):
            return RedactionResult(None, True, "excluded_path")

        within = self._within_allowed_root(path)

        if self.path_display is PathDisplay.OMITTED:
            return RedactionResult(None, True, "omitted")

        if self.path_display is PathDisplay.FULL:
            if within:
                return RedactionResult(path, False)
            return RedactionResult(None, True, "outside_allowed_roots")

        if self.path_display is PathDisplay.BASENAME:
            # A basename survives only for paths inside the allowed roots;
            # outside them, even the name can name a life.
            if within or not self.allowed_roots:
                return RedactionResult(os.path.basename(path), True, "basename_only")
            return RedactionResult(None, True, "outside_allowed_roots")

        if self.path_display is PathDisplay.HASHED:
            digest = hashlib.sha256(self.hash_salt + path.encode("utf-8")).hexdigest()
            return RedactionResult(digest[:32], True, "hashed")

        return RedactionResult(None, True, "omitted")

    def sanitize_details(self, details: dict) -> tuple[dict, tuple[str, ...]]:
        """Drop content-bearing keys. Returns the cleaned dict and what was cut."""
        cleaned: dict = {}
        removed: list[str] = []
        for key, value in details.items():
            if key.lower() in FORBIDDEN_CONTENT_KEYS:
                removed.append(key)
                continue
            if isinstance(value, dict):
                nested, nested_removed = self.sanitize_details(value)
                cleaned[key] = nested
                removed.extend(f"{key}.{item}" for item in nested_removed)
            else:
                cleaned[key] = value
        return cleaned, tuple(removed)
