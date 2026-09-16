"""Computed strength and confidence for knowledge-graph facts.

Task 2 of .hermes/plans/2026-09-15_evidential-belief-layer-plan.md

Replaces the coarse ``high`` / ``medium`` / ``low`` text label currently stored on
``graph_facts.confidence`` with a value derived from evidence.

Two numbers, deliberately kept separate:

* **strength**   — how often the claim holds  (supporting / supporting + contradicting)
* **confidence** — how much evidence stands behind that number, and how fresh it is

Volatility controls how fast confidence decays. It is a *parameter*, not a table
keyed to predicate names: a deployment is expected to measure churn and pass it
in. That is what keeps this portable.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_diary.analytics.signal_policy import (
    AGENT_DERIVED,
    CURATED,
    HARVESTED,
    INFERRED,
    OBSERVED,
    UNATTRIBUTED,
    UNKNOWN,
)

#: No fact may be more certain than its provenance class permits. This is what
#: guarantees an ephemeral, agent-narrated or derived fact can never outrank
#: something the human actually said.
CLASS_CEILING: dict[str, float] = {
    CURATED: 1.0,
    HARVESTED: 0.80,
    OBSERVED: 0.60,
    AGENT_DERIVED: 0.50,
    INFERRED: 0.50,
    UNATTRIBUTED: 0.35,
    UNKNOWN: 0.25,
}

#: Half-life in days, per volatility class. Defaults only — measure per
#: deployment. Not keyed to predicate names, by design.
VOLATILITY_HALF_LIFE_DAYS: dict[str, float] = {
    "stable": 3650.0,   # years — preferences, decisions, relationships
    "slow": 365.0,      # about a year
    "medium": 90.0,
    "fast": 7.0,        # days — addresses, versions, running state
}

DEFAULT_VOLATILITY = "medium"
DEFAULT_K = 3.0
DEFAULT_CONTRADICTION_PENALTY = 0.5


@dataclass(frozen=True)
class Belief:
    """A computed, explainable belief about one fact."""

    strength: float
    confidence: float
    provenance: str

    def as_dict(self) -> dict[str, float | str]:
        return {
            "strength": self.strength,
            "confidence": self.confidence,
            "provenance": self.provenance,
        }


def saturating_evidence(n_supporting: float, k: float = DEFAULT_K) -> float:
    """Diminishing returns on corroboration. Never reaches 1.0 for finite n."""
    if n_supporting <= 0:
        return 0.0
    return n_supporting / (n_supporting + k)


def decay(age_days: float, volatility: str = DEFAULT_VOLATILITY) -> float:
    """Exponential decay by half-life. Fresh evidence does not decay."""
    if age_days <= 0:
        return 1.0
    half_life = VOLATILITY_HALF_LIFE_DAYS.get(volatility)
    if half_life is None:
        half_life = VOLATILITY_HALF_LIFE_DAYS[DEFAULT_VOLATILITY]
    if half_life <= 0:
        return 0.0
    return 0.5 ** (age_days / half_life)


#: Non-use decay: a fact nobody ever recalls and nothing ever corroborates fades.
#: This is salience by usage rather than by taxonomy — it needs no predicate
#: names and it works the same in any domain.
NON_USE_GRACE_DAYS = 30.0
NON_USE_HALF_LIFE_DAYS = 365.0


def non_use_decay(
    days_since_last_use: float | None,
    *,
    grace_days: float = NON_USE_GRACE_DAYS,
    half_life_days: float = NON_USE_HALF_LIFE_DAYS,
) -> float:
    """Fade a fact that is never used.

    ``None`` means usage is UNKNOWN — which is the current state of the corpus,
    since recall tracking does not exist yet. Unknown usage returns a NEUTRAL
    factor of 1.0 and is never punitive: applying a penalty to an absent signal
    would decay every fact uniformly, which is noise, not salience.
    """
    if days_since_last_use is None:
        return 1.0
    if days_since_last_use < 0:
        raise ValueError("days_since_last_use must not be negative")
    if days_since_last_use <= grace_days:
        return 1.0
    if half_life_days <= 0:
        return 0.0
    return 0.5 ** ((days_since_last_use - grace_days) / half_life_days)


def compute_belief(
    *,
    n_supporting: int,
    n_contradicting: int = 0,
    age_days: float = 0.0,
    volatility: str = DEFAULT_VOLATILITY,
    provenance: str = HARVESTED,
    days_since_last_use: float | None = None,
    k: float = DEFAULT_K,
    contradiction_penalty: float = DEFAULT_CONTRADICTION_PENALTY,
) -> Belief:
    """Compute strength and confidence for one fact.

    ``days_since_last_use`` is optional and defaults to ``None`` (usage unknown),
    which is neutral. Recall tracking does not exist yet, so in practice it is
    always neutral today — see ``non_use_decay``.

    Raises ValueError on negative inputs — silent coercion here would hide
    upstream bugs in evidence counting.
    """
    if n_supporting < 0 or n_contradicting < 0:
        raise ValueError("evidence counts must not be negative")
    if age_days < 0:
        raise ValueError("age_days must not be negative")

    total = n_supporting + n_contradicting
    strength = (n_supporting / total) if total > 0 else 0.0

    evidence = saturating_evidence(n_supporting, k)
    contradiction_ratio = (n_contradicting / total) if total > 0 else 0.0

    raw_confidence = (
        evidence
        * decay(age_days, volatility)
        * non_use_decay(days_since_last_use)
        * (1.0 - contradiction_penalty * contradiction_ratio)
    )

    ceiling = CLASS_CEILING.get(provenance, CLASS_CEILING[UNKNOWN])
    confidence = max(0.0, min(raw_confidence, ceiling))

    return Belief(
        strength=round(strength, 6),
        confidence=round(confidence, 6),
        provenance=provenance,
    )
