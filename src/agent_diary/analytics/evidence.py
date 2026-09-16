"""Evidence counting, shared by every producer.

Fixes two review findings at once:

* **Duplicate sources** — confidence must count independent sources, not
  evidence rows. One entry asserting the same thing twice is one witness, and
  the plan has always said "deduplicate by entry, not by mention". The backfill
  was counting rows.
* **Producer divergence** — the backfill and the preview each had their own
  counting path and could disagree. One helper, one answer.

A "source" is a ``(source_kind, source_id)`` pair. A single source is classified
as contradicting if any of its roles contradict, otherwise as supporting.
"""

from __future__ import annotations

from dataclasses import dataclass

SUPPORT_ROLES = frozenset({"establishes", "supports"})
CONTRADICT_ROLES = frozenset({"contradicts", "refutes", "disputes", "conflicts"})


@dataclass(frozen=True)
class EvidenceCount:
    """Independent sources, not rows."""

    supporting: int
    contradicting: int
    raw_rows: int
    distinct_sources: int

    @property
    def duplicate_rows(self) -> int:
        return max(0, self.raw_rows - self.distinct_sources)


def _field(row, name, default=None):
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return default


def count_evidence(rows) -> EvidenceCount:
    """Count independent supporting and contradicting sources.

    Unrecognised roles count as support, conservatively — refusing to invent a
    contradiction where the schema does not name one.
    """
    rows = list(rows)
    by_source: dict[tuple[str, str], bool] = {}  # source -> is_contradicting

    for row in rows:
        role = (_field(row, "role") or "").strip().lower()
        kind = str(_field(row, "source_kind") or "")
        sid = str(_field(row, "source_id") or "")
        key = (kind, sid)
        contradicts = role in CONTRADICT_ROLES
        # Once a source contradicts, it keeps contradicting.
        by_source[key] = by_source.get(key, False) or contradicts

    supporting = sum(1 for contradicts in by_source.values() if not contradicts)
    contradicting = sum(1 for contradicts in by_source.values() if contradicts)

    return EvidenceCount(
        supporting=supporting,
        contradicting=contradicting,
        raw_rows=len(rows),
        distinct_sources=len(by_source),
    )
