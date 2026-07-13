from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from agent_diary.analytics.common import collect_source_rows, entry_row_to_body

OPEN_HINTS = (
    "todo",
    "follow up",
    "follow-up",
    "pending",
    "unresolved",
    "need to",
    "must",
    "action item",
    "next step",
)

CLOSED_HINTS = (
    "done",
    "completed",
    "resolved",
    "closed",
    "fixed",
    "cancelled",
    "canceled",
)

SHOULD_CONTEXT_HINTS = (
    "should send",
    "should follow",
    "should update",
    "should confirm",
    "should check",
    "should schedule",
)

QUESTION_ACTION_HINTS = (
    "follow up",
    "follow-up",
    "next",
    "pending",
    "plan",
    "decision",
    "deadline",
    "when",
    "check",
    "confirm",
    "verify",
    "let me know",
)

NON_ACTIONABLE_HINTS = (
    "how are you",
    "what do you think",
    "nice to have",
)

RESOLUTION_RESULT_HINTS = (
    "is back",
    "works now",
    "working now",
    "confirmed",
    "checked",
)

NON_DISTINCTIVE_TOKENS = {
    "check", "confirm", "verify", "whether", "follow", "next", "pending", "plan",
    "decision", "deadline", "when", "still", "works", "work", "know", "tell", "update",
    "great", "please", "could", "would",
}


@dataclass
class Candidate:
    entry_id: str
    created_at: str
    text: str


def _extract_candidates(entry_id: str, created_at: str, content: str) -> list[Candidate]:
    snippets: list[Candidate] = []
    content_lower = content.lower()
    has_open_in_entry = any(h in content_lower for h in OPEN_HINTS) or any(h in content_lower for h in SHOULD_CONTEXT_HINTS) or ("?" in content and any(h in content_lower for h in QUESTION_ACTION_HINTS))
    has_closed_in_entry = any(h in content_lower for h in CLOSED_HINTS)
    if has_open_in_entry and has_closed_in_entry:
        # Conservative precision choice for v1: mixed open/closed language in the same
        # entry is treated as ambiguous and skipped.
        return snippets

    # Keep extraction simple and explicit: split by lines and sentence-ish punctuation.
    parts = re.split(r"[\n\r]+|(?<=[.!?])\s+", content)
    cleaned_parts: list[str] = []
    for raw in parts:
        text = " ".join(raw.split()).strip()
        if len(text) < 10:
            continue
        cleaned_parts.append(text)

    for idx, text in enumerate(cleaned_parts):
        lowered = text.lower()
        has_open_hint = any(h in lowered for h in OPEN_HINTS)
        has_should_context = any(h in lowered for h in SHOULD_CONTEXT_HINTS)
        has_question_action_context = ("?" in text) and any(h in lowered for h in QUESTION_ACTION_HINTS)
        has_open = has_open_hint or has_should_context or has_question_action_context
        has_closed = any(h in lowered for h in CLOSED_HINTS)
        is_non_actionable = any(h in lowered for h in NON_ACTIONABLE_HINTS)
        if has_open and not has_closed and not is_non_actionable:
            if has_question_action_context and _is_question_addressed_later(text, cleaned_parts[idx + 1 :]):
                continue
            snippets.append(Candidate(entry_id=entry_id, created_at=created_at, text=text[:220]))
    return snippets


def _is_question_addressed_later(question_text: str, later_parts: list[str]) -> bool:
    q_tokens = {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9-]{4,}", question_text)
        if token.lower() not in NON_DISTINCTIVE_TOKENS
    }
    if not q_tokens:
        return False
    for part in later_parts:
        lowered = part.lower()
        has_resolution_signal = any(h in lowered for h in CLOSED_HINTS) or any(h in lowered for h in RESOLUTION_RESULT_HINTS)
        if not has_resolution_signal:
            continue
        part_tokens = {token.lower() for token in re.findall(r"[A-Za-z0-9-]{4,}", part)}
        if q_tokens.intersection(part_tokens):
            return True
    return False


def _loop_key(text: str) -> str:
    # Normalize obvious volatile tokens to reduce key churn within bounded windows.
    base = re.sub(r"\b\d+\b", " ", text.lower())
    base = re.sub(r"\b(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", " ", base)
    base = re.sub(r"[^a-z0-9 ]+", " ", base)
    base = re.sub(r"\s+", " ", base).strip()
    return base[:80]


def build_open_loops_payload(
    *,
    source_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    groups: dict[str, list[Candidate]] = {}
    for row in source_entries:
        body = entry_row_to_body(row)
        entry_id = str(body["entry_id"])
        created_at = str(body["created_at"])
        content = str(body.get("content", ""))
        for cand in _extract_candidates(entry_id, created_at, content):
            key = _loop_key(cand.text)
            groups.setdefault(key, []).append(cand)

    loops: list[dict[str, Any]] = []
    for key, candidates in groups.items():
        ordered = sorted(candidates, key=lambda c: c.created_at)
        supporting_ids = sorted({c.entry_id for c in ordered})
        loop_hash = hashlib.sha1(f"{key}|{'|'.join(supporting_ids)}".encode("utf-8")).hexdigest()[:12]
        mentions = len(ordered)
        if mentions >= 3:
            strength = "high"
            confidence = 0.85
        elif mentions >= 2:
            strength = "medium"
            confidence = 0.70
        else:
            strength = "low"
            confidence = 0.55

        title = ordered[0].text[:72].rstrip(" .!?")
        loops.append(
            {
                "loop_id": f"loop_{loop_hash}",
                "title": title,
                "status": "open",
                "summary": f"Unresolved concern detected from {mentions} mention(s) across source entries.",
                "supporting_entry_ids": supporting_ids,
                "evidence_snippets": [
                    {"entry_id": c.entry_id, "quote": c.text}
                    for c in ordered[:3]
                ],
                "signals": {"strength": strength, "confidence": confidence},
                "first_seen_at": ordered[0].created_at,
                "last_seen_at": ordered[-1].created_at,
            }
        )

    loops.sort(key=lambda l: (l["last_seen_at"], l["loop_id"]), reverse=True)
    return {"loops": loops}
