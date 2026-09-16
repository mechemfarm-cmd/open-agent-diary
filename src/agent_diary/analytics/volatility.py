"""Measured volatility — churn, not a predicate table.

Task 4 of .hermes/plans/2026-09-15_evidential-belief-layer-plan.md

The plan requires volatility to be *measured* per deployment rather than declared
as a list of volatile predicate names. This module does that, and it deliberately
supports two sources of measurement, because they are not equivalent:

* **retrospective** — inferred from timestamps already present in imported
  history. Available immediately, but second-hand: it describes what happened in
  the conversations, not what we watched change.
* **observed** — measured by us, first-hand, as a value changes between
  observations after the system is live.

A system that has only retrospective churn is producing *provisional* signals.
Once observed churn exists for a subject/predicate, it takes precedence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

#: Changes per year, mapped to a volatility class. These are DEFAULTS, measured
#: against real data and expected to be tuned per deployment. They are thresholds
#: on a measured quantity, not a list of predicate names.
CHURN_PER_YEAR_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (0.5, "stable"),
    (4.0, "slow"),
    (20.0, "medium"),
)
DEFAULT_UNMEASURED_VOLATILITY = "medium"

RETROSPECTIVE = "retrospective"
OBSERVED = "observed"


@dataclass(frozen=True)
class Churn:
    """Measured churn for one (subject, predicate) pair."""

    observations: int
    distinct_values: int
    changes: int
    span_days: float
    churn_per_year: float
    volatility: str
    source: str
    provisional: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "observations": self.observations,
            "distinct_values": self.distinct_values,
            "changes": self.changes,
            "span_days": round(self.span_days, 2),
            "churn_per_year": round(self.churn_per_year, 3),
            "volatility": self.volatility,
            "source": self.source,
            "provisional": self.provisional,
        }


def classify_churn(churn_per_year: float, thresholds=CHURN_PER_YEAR_THRESHOLDS) -> str:
    """Map a measured churn rate to a volatility class."""
    for limit, name in thresholds:
        if churn_per_year < limit:
            return name
    return "fast"


#: Values seen in real evidence rows that are not timestamps. `source_timestamp`
#: contains empty strings and the literal text "unknown" in the live corpus.
_JUNK_TIMESTAMPS = {"", "unknown", "none", "null", "n/a", "na", "-", "0"}


def parse_ts(value) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _JUNK_TIMESTAMPS:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def measure_churn(
    observations: list[tuple[datetime | None, str]],
    *,
    source: str = RETROSPECTIVE,
    min_span_days: float = 30.0,
) -> Churn:
    """Measure churn from a time-ordered series of (timestamp, value) observations.

    A single observation cannot evidence stability — one sighting of anything is
    indistinguishable from one sighting of something volatile. Those return the
    unmeasured default rather than a confident "stable".

    ``min_span_days`` guards the *rate*: extrapolating changes-per-year from a
    three-day window produced absurd values (1,200/yr) on real data, because a
    short burst of observation says nothing about annual behaviour. Below the
    span threshold the pair is reported as unmeasured.
    """
    clean = [(ts, str(v)) for ts, v in observations]
    if not clean:
        raise ValueError("measure_churn needs at least one observation")

    stamped = sorted(
        (ts, v) for ts, v in clean if ts is not None
    )
    distinct = len({v for _, v in clean})

    # Order by timestamp BEFORE counting changes. Counting in input order while
    # taking the span from sorted timestamps was a footgun: an unsorted caller
    # got a churn rate computed from a different sequence than its span, and the
    # preview does not order its rows. Untimestamped observations still
    # participate in change counting and are placed last; they never affect span.
    _EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
    ordered = sorted(clean, key=lambda p: (1, _EPOCH) if p[0] is None else (0, p[0]))

    changes = 0
    for i in range(1, len(ordered)):
        if ordered[i][1] != ordered[i - 1][1]:
            changes += 1

    if len(stamped) >= 2:
        span_days = (stamped[-1][0] - stamped[0][0]).total_seconds() / 86400.0
    else:
        span_days = 0.0

    # Not enough evidence to say anything about volatility. Say so.
    if len(clean) < 2 or span_days < min_span_days:
        return Churn(
            observations=len(clean),
            distinct_values=distinct,
            changes=changes,
            span_days=span_days,
            churn_per_year=0.0,
            volatility=DEFAULT_UNMEASURED_VOLATILITY,
            source=source,
            provisional=True,
        )

    span_years = max(span_days / 365.0, 1e-6)
    churn_per_year = changes / span_years

    return Churn(
        observations=len(clean),
        distinct_values=distinct,
        changes=changes,
        span_days=span_days,
        churn_per_year=churn_per_year,
        volatility=classify_churn(churn_per_year),
        source=source,
        provisional=(source == RETROSPECTIVE),
    )
