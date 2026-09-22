from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Iterable

from agent_diary.analytics.semantic_sources import EvidenceRef, SemanticCandidate, collect_semantic_candidates
from agent_diary.config import Paths

ALLOWED_PURPOSES = {"current_status", "decision_rationale", "next_action"}


@dataclass(frozen=True)
class SemanticEvaluationCase:
    case_id: str
    query: str
    purpose: str
    expected_elements: list[str]
    required_source_refs: list[str]
    known_traps: list[str]


@dataclass(frozen=True)
class SemanticComparisonMetrics:
    case_id: str
    baseline_required_element_coverage: float
    required_element_coverage: float
    source_reference_coverage: float
    stale_conflict_handled: bool
    unsupported_claim_count: int
    baseline_output_size: int
    situation_output_size: int
    baseline_failure_categories: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "baseline_required_element_coverage": self.baseline_required_element_coverage,
            "required_element_coverage": self.required_element_coverage,
            "source_reference_coverage": self.source_reference_coverage,
            "stale_conflict_handled": self.stale_conflict_handled,
            "unsupported_claim_count": self.unsupported_claim_count,
            "baseline_output_size": self.baseline_output_size,
            "situation_output_size": self.situation_output_size,
            "baseline_failure_categories": self.baseline_failure_categories,
        }


def compare_semantic_retrieval(
    case: SemanticEvaluationCase,
    *,
    baseline_text: str,
    situation_text: str,
    situation_source_refs: list[str],
    unsupported_claims: list[str] | None = None,
) -> SemanticComparisonMetrics:
    """Compare current retrieval text with a source-linked situation preview.

    This helper is deterministic and read-only; it scores literal fixture labels
    against supplied outputs so evaluation artifacts can be generated without
    touching live recall state.
    """
    baseline_coverage = _coverage(case.expected_elements, baseline_text)
    situation_coverage = _coverage(case.expected_elements, situation_text)
    required_refs = [ref for ref in case.required_source_refs if ref.lower() != "none"]
    ref_coverage = _coverage(required_refs, "\n".join([situation_text, *situation_source_refs])) if required_refs else 1.0
    lower_situation = situation_text.lower()
    stale_conflict_handled = not case.known_traps or any(
        marker in lower_situation for marker in ("superseded", "historical", "conflict", "stale", "resolved", "planned", "inferred", "empty")
    )
    categories = _failure_categories(case, baseline_text=baseline_text, baseline_coverage=baseline_coverage)
    return SemanticComparisonMetrics(
        case_id=case.case_id,
        baseline_required_element_coverage=baseline_coverage,
        required_element_coverage=situation_coverage,
        source_reference_coverage=ref_coverage,
        stale_conflict_handled=stale_conflict_handled,
        unsupported_claim_count=len(unsupported_claims or []),
        baseline_output_size=len(baseline_text),
        situation_output_size=len(situation_text),
        baseline_failure_categories=categories,
    )


def load_semantic_evaluation_cases(path: Path | str) -> list[SemanticEvaluationCase]:
    fixture_path = Path(path)
    seen: set[str] = set()
    cases: list[SemanticEvaluationCase] = []
    for line_no, raw in enumerate(fixture_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON on line {line_no}: {exc}") from exc
        if not isinstance(body, dict):
            raise ValueError(f"case on line {line_no} must be an object")
        case_id = _required_str(body, "case_id", line_no)
        if case_id in seen:
            raise ValueError(f"duplicate semantic evaluation case_id: {case_id}")
        seen.add(case_id)
        purpose = _required_str(body, "purpose", line_no)
        if purpose not in ALLOWED_PURPOSES:
            raise ValueError(f"unsupported purpose on line {line_no}: {purpose}")
        cases.append(
            SemanticEvaluationCase(
                case_id=case_id,
                query=_required_str(body, "query", line_no),
                purpose=purpose,
                expected_elements=_required_str_list(body, "expected_elements", line_no),
                required_source_refs=_required_str_list(body, "required_source_refs", line_no),
                known_traps=_required_str_list(body, "known_traps", line_no, allow_empty=True),
            )
        )
    return cases


def _required_str(body: dict[str, Any], key: str, line_no: int) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"case on line {line_no} requires non-empty string field {key!r}")
    return value.strip()


def _required_str_list(body: dict[str, Any], key: str, line_no: int, *, allow_empty: bool = False) -> list[str]:
    value = body.get(key)
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ValueError(f"case on line {line_no} requires list field {key!r}")
    out = [str(item).strip() for item in value if str(item).strip()]
    if not out and not allow_empty:
        raise ValueError(f"case on line {line_no} requires non-empty list field {key!r}")
    return out


@dataclass(frozen=True)
class SituationItem:
    source_id: str
    source_kind: str
    text: str
    timestamp: str
    status: str
    source_refs: list[EvidenceRef]
    confidence: str | None = None
    provisional: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "text": self.text,
            "timestamp": self.timestamp,
            "status": self.status,
            "source_refs": [ref.to_dict() for ref in self.source_refs],
            "confidence": self.confidence,
            "provisional": self.provisional,
        }


@dataclass(frozen=True)
class Conflict:
    text: str
    candidate_ids: list[str]
    source_refs: list[EvidenceRef]

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "candidate_ids": self.candidate_ids,
            "source_refs": [ref.to_dict() for ref in self.source_refs],
        }


@dataclass(frozen=True)
class InferenceNote:
    kind: str
    text: str
    source_refs: list[EvidenceRef] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "source_refs": [ref.to_dict() for ref in self.source_refs],
        }


@dataclass(frozen=True)
class Situation:
    topic: str
    purpose: str
    episode: dict[str, Any]
    current_state: list[SituationItem] = field(default_factory=list)
    history: list[SituationItem] = field(default_factory=list)
    decisions: list[SituationItem] = field(default_factory=list)
    evidence: list[SituationItem] = field(default_factory=list)
    open_questions: list[SituationItem] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    inference_notes: list[InferenceNote] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "purpose": self.purpose,
            "episode": self.episode,
            "current_state": [item.to_dict() for item in self.current_state],
            "history": [item.to_dict() for item in self.history],
            "decisions": [item.to_dict() for item in self.decisions],
            "evidence": [item.to_dict() for item in self.evidence],
            "open_questions": [item.to_dict() for item in self.open_questions],
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "inference_notes": [note.to_dict() for note in self.inference_notes],
        }


def compile_situation(
    paths: Paths,
    *,
    topic: str,
    purpose: str,
    limit: int = 20,
    char_budget: int = 4000,
) -> Situation:
    candidates = collect_semantic_candidates(paths, topic=topic, limit=limit)
    return assemble_situation(
        topic=topic,
        purpose=purpose,
        candidates=candidates,
        item_limit=limit,
        char_budget=char_budget,
    )


def assemble_situation(
    *,
    topic: str,
    purpose: str,
    candidates: Iterable[SemanticCandidate],
    item_limit: int = 20,
    char_budget: int = 4000,
) -> Situation:
    if purpose not in ALLOWED_PURPOSES:
        raise ValueError(f"unsupported semantic-memory purpose: {purpose}")

    ordered = sorted(list(candidates), key=lambda c: (c.timestamp, c.source_kind, c.source_id), reverse=True)
    budgeted = ordered[: max(0, item_limit)]
    per_item_chars = max(24, min(280, int(char_budget))) if char_budget else 280

    if not budgeted:
        return Situation(
            topic=topic,
            purpose=purpose,
            episode={"anchor": topic, "status": "empty", "candidate_count": 0},
            inference_notes=[InferenceNote(kind="empty", text=f"No source candidates found for {topic!r}.")],
        )

    current: list[SituationItem] = []
    history: list[SituationItem] = []
    decisions: list[SituationItem] = []
    evidence: list[SituationItem] = []
    open_questions: list[SituationItem] = []

    for cand in budgeted:
        item = _item_from_candidate(cand, char_limit=per_item_chars)
        evidence.append(item)
        roles = _candidate_role_tokens(cand)
        if _is_decision_like(roles):
            decisions.append(item)
            continue
        if _is_open_loop_like(cand, roles):
            open_questions.append(item)
            continue
        if cand.status == "current" and not cand.superseded_by:
            current.append(item)
        else:
            status = "superseded" if cand.superseded_by else cand.status
            history.append(_item_from_candidate(cand, char_limit=per_item_chars, status=status))

    conflicts = _detect_conflicts(budgeted)
    notes = [
        InferenceNote(
            kind="grouping",
            text="Candidates were grouped deterministically by topic text, shared subject, timestamps, and explicit source links; this is an inferred episode boundary, not a raw fact.",
            source_refs=_dedupe_refs(ref for cand in budgeted for ref in cand.evidence_refs)[:5],
        )
    ]
    return Situation(
        topic=topic,
        purpose=purpose,
        episode={
            "anchor": topic,
            "status": "compiled",
            "candidate_count": len(budgeted),
            "subjects": sorted({cand.subject for cand in budgeted}),
        },
        current_state=current[:item_limit],
        history=history[:item_limit],
        decisions=decisions[:item_limit],
        evidence=evidence[:item_limit],
        open_questions=open_questions[:item_limit],
        conflicts=conflicts[:item_limit],
        inference_notes=notes,
    )


def render_situation_view(situation: Situation, *, purpose: str | None = None, char_budget: int = 4000) -> str:
    selected = purpose or situation.purpose
    if selected not in ALLOWED_PURPOSES:
        raise ValueError(f"unsupported semantic-memory purpose: {selected}")
    if selected == "current_status":
        sections = [
            ("Current state", situation.current_state),
            ("Conflicts", situation.conflicts),
            ("Recent evidence", situation.evidence),
            ("History", situation.history),
        ]
    elif selected == "decision_rationale":
        sections = [
            ("Decisions and rationale", situation.decisions),
            ("Supporting evidence", situation.evidence),
            ("Conflicts", situation.conflicts),
        ]
    else:
        sections = [
            ("Open questions and next actions", situation.open_questions),
            ("Blockers/conflicts", situation.conflicts),
            ("Current state", situation.current_state),
        ]

    lines = [
        f"Semantic situation preview — {selected}",
        f"Topic: {situation.topic}",
        "Read-only derived view; raw entries remain authoritative.",
    ]
    for title, items in sections:
        lines.append("")
        lines.append(f"{title}:")
        if not items:
            lines.append("- none found")
            continue
        for item in items:
            if isinstance(item, Conflict):
                refs = _format_refs(item.source_refs)
                lines.append(f"- CONFLICT: {item.text} [{refs}]")
            else:
                refs = _format_refs(item.source_refs)
                label = item.status
                if item.confidence:
                    label += f", confidence: {item.confidence}"
                if item.provisional:
                    label += ", provisional"
                lines.append(f"- ({label}) {item.text} [{refs}]")
    if situation.inference_notes:
        lines.append("")
        lines.append("Inference notes:")
        for note in situation.inference_notes:
            lines.append(f"- {note.text}")
    rendered = "\n".join(lines)
    if len(rendered) <= char_budget:
        return rendered
    return rendered[: max(0, char_budget - 1)].rstrip() + "…"


def _item_from_candidate(cand: SemanticCandidate, *, char_limit: int, status: str | None = None) -> SituationItem:
    return SituationItem(
        source_id=cand.source_id,
        source_kind=cand.source_kind,
        text=_clip(cand.text, char_limit),
        timestamp=cand.timestamp,
        status=status or cand.status,
        source_refs=cand.evidence_refs or [EvidenceRef(cand.source_kind, cand.source_id, cand.timestamp, "source")],
        confidence=cand.confidence,
        provisional=cand.provisional,
    )


def _detect_conflicts(candidates: list[SemanticCandidate]) -> list[Conflict]:
    buckets: dict[tuple[str, str], list[SemanticCandidate]] = {}
    for cand in candidates:
        if cand.status != "current" or not cand.predicate:
            continue
        key = (cand.subject.lower(), cand.predicate.upper())
        buckets.setdefault(key, []).append(cand)
    conflicts: list[Conflict] = []
    for (subject, predicate), group in buckets.items():
        values = {str(c.object_value or c.text) for c in group}
        if len(values) <= 1:
            continue
        source_refs = _dedupe_refs(ref for cand in group for ref in cand.evidence_refs)
        conflicts.append(
            Conflict(
                text=f"{subject} has conflicting current {predicate} values: {', '.join(sorted(values))}",
                candidate_ids=sorted(c.source_id for c in group),
                source_refs=source_refs,
            )
        )
    conflicts.sort(key=lambda c: c.text)
    return conflicts


def _dedupe_refs(refs: Iterable[EvidenceRef]) -> list[EvidenceRef]:
    seen: set[tuple[str, str]] = set()
    out: list[EvidenceRef] = []
    for ref in refs:
        key = (ref.source_kind, ref.source_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    return out


def _coverage(required: list[str], text: str) -> float:
    if not required:
        return 1.0
    lowered = text.lower()
    hits = sum(1 for item in required if _phrase_matches(item, lowered))
    return hits / len(required)


def _phrase_matches(phrase: str, lowered_text: str) -> bool:
    terms = [term for term in str(phrase).lower().replace(":", " ").split() if len(term) > 2]
    if not terms:
        return False
    return all(term in lowered_text for term in terms)


def _failure_categories(case: SemanticEvaluationCase, *, baseline_text: str, baseline_coverage: float) -> list[str]:
    categories: list[str] = []
    traps = " ".join(case.known_traps).lower()
    lower_baseline = baseline_text.lower()
    if baseline_coverage < 1.0:
        categories.append("retrieval failure")
    if any(token in traps for token in ("stale", "superseded", "resolved", "planned")) and not any(token in lower_baseline for token in ("superseded", "historical", "stale", "resolved", "planned")):
        categories.append("temporal/change failure")
    if "conflict" in traps and "conflict" not in lower_baseline:
        categories.append("missing relationship")
    if case.purpose == "decision_rationale" and baseline_coverage < 1.0:
        categories.append("missing decision rationale")
    if case.purpose == "next_action" and baseline_coverage < 1.0:
        categories.append("poor context composition")
    if "inferred" in traps and "inferred" not in lower_baseline:
        categories.append("missing episode structure")
    return categories


def _candidate_role_tokens(cand: SemanticCandidate) -> set[str]:
    tokens: set[str] = set()
    metadata = cand.metadata if isinstance(cand.metadata, dict) else {}
    for key in ("semantic_role", "role", "kind", "category", "event_type"):
        value = metadata.get(key)
        if isinstance(value, str):
            tokens.update(_split_role_tokens(value))
    semantic_value = metadata.get("semantic")
    semantic = semantic_value if isinstance(semantic_value, dict) else {}
    for key in ("role", "kind", "category"):
        value = semantic.get(key)
        if isinstance(value, str):
            tokens.update(_split_role_tokens(value))
    for ref in cand.evidence_refs:
        if ref.role:
            tokens.update(_split_role_tokens(ref.role))
    if cand.author_role:
        tokens.update(_split_role_tokens(cand.author_role))
    return tokens


def _split_role_tokens(value: str) -> set[str]:
    return {part for part in str(value).lower().replace("-", "_").replace("/", "_").split("_") if part}


def _is_decision_like(tokens: set[str]) -> bool:
    return bool(tokens & {"decision", "decides", "decided", "choice", "chosen", "selected", "selection", "rationale", "reason"})


def _is_open_loop_like(cand: SemanticCandidate, tokens: set[str]) -> bool:
    if tokens & {"question", "ask", "review", "todo", "task", "action", "followup", "blocker", "blocked", "unresolved", "open"}:
        return True
    text = cand.text.strip()
    return text.endswith("?")


def _format_refs(refs: list[EvidenceRef]) -> str:
    if not refs:
        return "source:unknown"
    return ", ".join(f"{ref.source_kind}:{ref.source_id}" for ref in refs[:3])


def _clip(text: str, limit: int) -> str:
    compact = " ".join(str(text).split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"
