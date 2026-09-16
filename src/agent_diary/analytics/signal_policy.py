"""Provenance classification for knowledge-graph facts.

Task 2 of .hermes/plans/2026-09-15_evidential-belief-layer-plan.md

TWO independent axes, because one is not enough:

* **source axis (HOW)** — ``source_kind``: raw_entry, user_assertion, work_trace,
  overlay. Where the evidence record came from.
* **attribution axis (WHO)** — ``author_role``: human, agent, mixed. Who actually
  said it.

``raw_entry`` alone is a CONTAINER, not an origin. A raw conversation entry can
hold the human's words, the agent's words, a pasted command output, or a
summarised observation. Classifying every ``raw_entry`` fact as recall-eligible
knowledge let machine-derived claims be retrieved as though the user had said
them — the blocker found in external review.

So a ``raw_entry`` fact is only knowledge when its entry is attributable to the
human.

Predicate names must never appear here. Neither axis consults what a fact is
*about*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

# ── provenance classes, most recall-worthy first ──────────────────────────────
CURATED = "curated"              # a human deliberately asserted it
HARVESTED = "harvested"          # conversation, attributable to the human
AGENT_DERIVED = "agent_derived"  # conversation, but the agent's words
OBSERVED = "observed"            # from execution or a probe
UNATTRIBUTED = "unattributed"    # conversation, speaker cannot be determined
INFERRED = "inferred"            # derived by the system itself
UNKNOWN = "unknown"

CLASSES = (CURATED, HARVESTED, AGENT_DERIVED, OBSERVED, UNATTRIBUTED, INFERRED, UNKNOWN)

#: Precedence when evidence of several kinds supports one fact.
#:
#: * a deliberate human assertion trumps everything
#: * a human-attributable conversation fact outranks the agent's narration of the
#:   same fact — if the human said it, it is knowledge regardless of the agent
#:   also mentioning it. (This ordering REVERSED when the attribution axis was
#:   added: ``observed`` used to mean "re-derivable" and outranked narration,
#:   but the agent-derived class is a different thing from a probe result.)
#: * unattributed sits below a real source: we have a fact but not a speaker
#: * unrecognised kinds fall to the most restrictive class
CLASS_PRECEDENCE: tuple[str, ...] = (
    CURATED, HARVESTED, AGENT_DERIVED, OBSERVED, UNATTRIBUTED, INFERRED, UNKNOWN,
)

#: Only human-attributable knowledge is eligible for default recall.
RECALL_ELIGIBLE: frozenset[str] = frozenset({CURATED, HARVESTED})

# ── attribution ───────────────────────────────────────────────────────────────
HUMAN = "human"
AGENT = "agent"
MIXED = "mixed"
UNATTRIBUTED_AUTHOR = "unattributed"

#: ``author_role`` in the live corpus uses four labels for two roles, plus
#: "mixed" for entries containing both speakers.
ATTRIBUTION_BY_AUTHOR_ROLE: dict[str, str] = {
    "human": HUMAN,
    "user": HUMAN,
    "agent": AGENT,
    "assistant": AGENT,
    "mixed": MIXED,
}

#: Source kinds whose meaning depends on WHO spoke.
ATTRIBUTION_SENSITIVE_SOURCE_KINDS = frozenset({"raw_entry"})

#: Default mapping from the ``source_kind`` vocabulary already in the codebase
#: (see ``_source_exists`` in service/handlers.py and the CLI defaults).
DEFAULT_SOURCE_KIND_CLASS: dict[str, str] = {
    "user_assertion": CURATED,
    "raw_entry": HARVESTED,     # refined by attribution below
    "work_trace": OBSERVED,
    "overlay": INFERRED,
}


@dataclass(frozen=True)
class SignalPolicy:
    """Configurable mapping from evidence to provenance class."""

    source_kind_class: Mapping[str, str] = field(
        default_factory=lambda: dict(DEFAULT_SOURCE_KIND_CLASS)
    )
    attribution_by_author_role: Mapping[str, str] = field(
        default_factory=lambda: dict(ATTRIBUTION_BY_AUTHOR_ROLE)
    )
    attribution_sensitive: frozenset[str] = field(
        default_factory=lambda: frozenset(ATTRIBUTION_SENSITIVE_SOURCE_KINDS)
    )

    def attribution_for(self, author_role: str | None) -> str:
        """Who spoke? Unknown authors are unattributed, never assumed human."""
        role = (author_role or "").strip().lower()
        if not role:
            return UNATTRIBUTED_AUTHOR
        return self.attribution_by_author_role.get(role, UNATTRIBUTED_AUTHOR)

    def class_for(self, source_kind: str | None, author_role: str | None = None) -> str:
        """Classify one evidence row. The only public entry point."""
        kind = (source_kind or "").strip().lower()
        if not kind:
            return UNKNOWN
        base = self.source_kind_class.get(kind)
        if base is None:
            return UNKNOWN
        if kind not in self.attribution_sensitive:
            return base

        attribution = self.attribution_for(author_role)
        if attribution == HUMAN:
            return HARVESTED
        if attribution == AGENT:
            return AGENT_DERIVED
        # mixed, or nothing to go on. Conservative: we cannot say the user said it.
        return UNATTRIBUTED

    def classify(self, evidence: Iterable) -> str:
        """Classify a fact from its evidence rows.

        Each item is either a ``source_kind`` string, or a
        ``(source_kind, author_role)`` pair. Strings alone are still accepted so
        callers with no attribution available keep working — they simply land in
        ``unattributed`` rather than being assumed human.
        """
        found: set[str] = set()
        for item in evidence:
            if isinstance(item, str):
                found.add(self.class_for(item, None))
                continue
            parts = tuple(item)
            kind = parts[0] if len(parts) > 0 else None
            author = parts[1] if len(parts) > 1 else None
            found.add(self.class_for(kind, author))
        found.discard("")
        if not found:
            return UNKNOWN
        for candidate in CLASS_PRECEDENCE:
            if candidate in found:
                return candidate
        return UNKNOWN


DEFAULT_POLICY = SignalPolicy()


def classify_fact(evidence, policy: SignalPolicy | None = None) -> str:
    """Convenience wrapper. Note: no predicate or statement argument."""
    return (policy or DEFAULT_POLICY).classify(evidence)


def is_recall_eligible(provenance_class: str) -> bool:
    """Whether a fact of this class may be surfaced in default recall."""
    return provenance_class in RECALL_ELIGIBLE
