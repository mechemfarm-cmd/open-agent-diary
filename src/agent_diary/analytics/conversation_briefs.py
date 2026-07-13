from __future__ import annotations

import re
from typing import Any


def _split_turns(content: str) -> list[tuple[str, str]]:
    turns: list[tuple[str, str]] = []
    current_speaker = ""
    current_lines: list[str] = []
    for line in content.replace("\r\n", "\n").split("\n"):
        match = re.match(r"^([^:\n]{1,48}):\s*(.*)$", line)
        if match:
            if current_speaker or current_lines:
                turns.append((current_speaker, "\n".join(current_lines).strip()))
            current_speaker = match.group(1).strip()
            current_lines = [match.group(2)]
            continue
        current_lines.append(line)
    if current_speaker or current_lines:
        turns.append((current_speaker, "\n".join(current_lines).strip()))
    return [(speaker, body) for speaker, body in turns if body]


def _clean_text(text: str) -> str:
    return " ".join(str(text).split()).strip()


def _sentenceish(text: str, limit: int = 220) -> str:
    compact = _clean_text(text)
    if len(compact) <= limit:
        return compact
    clipped = compact[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{clipped}…"


def _speaker_label(speaker: str) -> str:
    lowered = _clean_text(speaker).lower()
    if lowered in {"assistant", "agent", "assistant", "codex", "bot"}:
        return "Assistant"
    if lowered in {"user", "human", "user", "sampleuser", "sampleuser"}:
        return "User"
    return _clean_text(speaker) or "Speaker"


def build_conversation_brief_text(entry: dict[str, Any]) -> str:
    content = str(entry.get("content", ""))
    turns = _split_turns(content)
    if not turns:
        return _sentenceish(content, limit=260)

    first_turn = turns[0]
    last_turn = turns[-1]
    middle_turn = turns[1] if len(turns) >= 3 else None
    question_turn = next((turn for turn in turns if "?" in turn[1]), None)

    opener = _sentenceish(first_turn[1], limit=140)
    closer = _sentenceish(last_turn[1], limit=140)

    if question_turn and question_turn != first_turn:
        focus = _sentenceish(question_turn[1], limit=120)
        return (
            f"{_speaker_label(first_turn[0])} opens with {opener} "
            f"The main ask becomes {focus} "
            f"It closes with {_speaker_label(last_turn[0])}: {closer}"
        )

    if middle_turn:
        response = _sentenceish(middle_turn[1], limit=120)
        return (
            f"{_speaker_label(first_turn[0])} starts with {opener} "
            f"{_speaker_label(middle_turn[0])} responds: {response} "
            f"It closes with {_speaker_label(last_turn[0])}: {closer}"
        )

    if len(turns) == 1:
        return f"{_speaker_label(first_turn[0])}: {opener}"

    return (
        f"{_speaker_label(first_turn[0])} starts with {opener} "
        f"The exchange ends with {_speaker_label(last_turn[0])}: {closer}"
    )
