"""Belief ranking and recall selection.

Task 5 + 6 of .hermes/plans/2026-09-15_evidential-belief-layer-plan.md

Three separate quantities, deliberately not conflated:

* **strength**   — how often the claim holds
* **confidence** — how much evidence stands behind it
* **salience**   — what usage has earned it

The distinction matters because confidence alone ranks a true, well-corroborated
and completely useless fact at the top. On the live corpus a 174-observation IP
address scored maximum confidence. Confidence is not importance.

**Salience is spend and replenish** (Task 6). Surfacing a fact SPENDS attention;
being acted on EARNS it back. A fact that has never been surfaced keeps its full
unspent value, so heavily-surfaced-but-unearned facts fall below it — rotation
falls out of the rule rather than needing a separate exploration quota.

No predicate names appear here. No MCP tool surface is added: recall is paid for
in the payload, not permanently in a tool schema.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_diary.analytics.confidence import saturating_evidence
from agent_diary.analytics.signal_policy import is_recall_eligible

# ─ salience constants ────────────────────────────────────────────────────────
#: Never let usage crush a fact: the spend is a nudge, not a verdict.
SALIENCE_FLOOR = 0.25
SALIENCE_CEILING = 1.0
#: Maximum penalty from OUTSTANDING unearned exposure. Note that unbounded
#: surfacing converges on 1 - SPEND_WEIGHT = 0.40, never on SALIENCE_FLOOR, so
#: the honest claim is that spending NUDGES ranking — it does not guarantee
#: rotation. A fact whose baseline is ~2.5x better survives any amount of
#: unearned exposure. Stated plainly because the earlier "free exploration"
#: claim was stronger than this arithmetic supports.
SPEND_WEIGHT = 0.60
SPEND_K = 3.0

DEFAULT_MIN_SCORE = 0.15
DEFAULT_LIMIT = 5
DEFAULT_CHAR_BUDGET = 900


@dataclass(frozen=True)
class Candidate:
    """One fact considered for recall."""

    fact_id: str
    statement: str
    provenance: str
    strength: float
    confidence: float
    #: Surfacing pressure not yet cleared by being acted on. None means usage is
    #: unknown, which is neutral.
    surfaced_since_credit: int | None = None
    #: Times this fact has actually been acted on. A STATISTIC, not a scoring
    #: input — acting on a fact pays its pressure down at write time instead, so
    #: past credit cannot be banked against future exposure.
    acted_on_count: int | None = None
    #: True when this belief was derived from a bulk import rather than from
    #: first-hand observation. It must be carried all the way to the rendered
    #: block — the plan forbids silently mixing provisional and observed.
    provisional: bool = True


def salience(
    *,
    surfaced_since_credit: int | None = None,
    spend_k: float = SPEND_K,
) -> float:
    """Spend and replenish. How much outstanding attention debt does this carry?

    Usage UNKNOWN (``None``) is neutral at 1.0: applying a penalty to an absent
    signal would demote everything uniformly, which is noise rather than
    salience.

    Scoring reads ONLY the outstanding unearned exposure. It deliberately does
    not read ``acted_on_count``: lifetime credit used to offset pressure, which
    let a fact that was useful once immunise itself against all future unearned
    exposure. Acting on a fact now pays the debt down at write time instead, so
    past credit cannot be banked.

    This is not a ratio. ``acted_on / surfaced`` explodes at small denominators,
    so one use out of one surfacing would look perfect; a saturating curve on the
    outstanding count avoids that.
    """
    if surfaced_since_credit is None:
        return 1.0
    if surfaced_since_credit < 0:
        raise ValueError("surfaced_since_credit must not be negative")

    spent = saturating_evidence(surfaced_since_credit, spend_k)
    value = SALIENCE_CEILING - SPEND_WEIGHT * spent
    return max(SALIENCE_FLOOR, min(SALIENCE_CEILING, value))


def score(candidate: Candidate) -> float:
    """Overall recall priority. Zero for classes not eligible for recall."""
    if not is_recall_eligible(candidate.provenance):
        return 0.0
    s = salience(surfaced_since_credit=candidate.surfaced_since_credit)
    return candidate.strength * candidate.confidence * s


def rank(
    candidates: list[Candidate],
    *,
    limit: int = DEFAULT_LIMIT,
    min_score: float = DEFAULT_MIN_SCORE,
) -> list[tuple[Candidate, float]]:
    """Select the highest-priority eligible candidates.

    Returns ``(candidate, score)`` pairs, best first. Ineligible classes score
    zero and fall out here rather than being filtered upstream — one rule, one
    place.
    """
    scored = [(c, score(c)) for c in candidates]
    scored = [(c, s) for c, s in scored if s > 0.0 and s >= min_score]
    scored.sort(key=lambda pair: (-pair[1], pair[0].fact_id))
    return scored[:limit]


def render_selection(
    selected: list[tuple[Candidate, float]],
    *,
    char_budget: int = DEFAULT_CHAR_BUDGET,
) -> tuple[str, list[str]]:
    """Render the block AND report which facts actually made it in.

    The distinction matters because surfacing SPENDS attention. A fact that was
    selected but cut by the character budget was never shown, so it must not be
    charged: billing it would penalise precisely the facts that never got their
    turn, which is the opposite of what spending is for.
    """
    if not selected:
        return "", []
    lines: list[str] = []
    shown: list[str] = []
    used = 0
    for cand, s in selected:
        mark = " [provisional]" if cand.provisional else ""
        line = f"- {cand.statement} ({s:.2f}){mark}"
        if used + len(line) + 1 > char_budget:
            # SKIP, do not stop. Breaking here meant one oversized statement at
            # the top of the list produced a permanently empty block: it never
            # rendered, so it was never charged, so it was never demoted, so it
            # stayed at the top and blocked every shorter fact behind it for
            # good. Skipping lets the budget go to candidates that can use it.
            continue
        lines.append(line)
        shown.append(cand.fact_id)
        used += len(line) + 1
    return "\n".join(lines), shown


def render_block(
    selected: list[tuple[Candidate, float]],
    *,
    char_budget: int = DEFAULT_CHAR_BUDGET,
) -> str:
    """Render a compact recall block within a hard character budget.

    The budget is enforced, not advisory: recall is paid on every turn, so an
    uncapped block would eat context permanently.
    """
    return render_selection(selected, char_budget=char_budget)[0]