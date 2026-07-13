from __future__ import annotations

import re
from typing import Any

from agent_diary.analytics.common import collect_source_rows

STOPWORDS = {
    "the", "and", "for", "that", "this", "with", "have", "from", "your", "just", "into", "then", "they",
    "them", "what", "when", "where", "will", "would", "could", "should", "about", "there", "here", "their",
    "were", "been", "being", "also", "than", "them", "our", "you", "are", "but", "not", "too", "can", "let",
    "know", "looks", "look", "said", "just", "still", "more", "like", "need",
    "assistant", "sampleuser",
}
WEAK_PHRASE_TOKENS = {
    "check", "verify", "confirm", "whether", "great", "works", "still", "route", "back",
}
WEAK_UNIGRAM_TOKENS = {
    "check", "great", "verify", "confirm", "whether", "works", "still",
}

DECISION_CUES = ("let's", "lets", "we should", "we will", "plan", "the plan", "decision", "agreed")
COMMITMENT_CUES = ("i'll", "i will", "i can", "i'm going to", "ill ", "i am going to", "next i")

def _clean_text(text: str) -> str:
    return " ".join(str(text).split()).strip()


def _sentenceish(text: str, limit: int = 220) -> str:
    compact = _clean_text(text)
    if len(compact) <= limit:
        return compact
    clipped = compact[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{clipped}…"


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


def _speaker_label(speaker: str) -> str:
    lowered = _clean_text(speaker).lower()
    if lowered in {"assistant", "agent", "assistant", "codex", "bot"}:
        return "Assistant"
    if lowered in {"user", "human", "user", "sampleuser", "sampleuser"}:
        return "User"
    return _clean_text(speaker) or "Speaker"


def _extract_keywords(text: str, limit: int = 8) -> list[str]:
    words = [token.lower() for token in re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", text)]
    counts: dict[str, int] = {}
    for token in words:
        if token in STOPWORDS or token in WEAK_UNIGRAM_TOKENS or len(token) < 4:
            continue
        counts[token] = counts.get(token, 0) + 1

    phrase_counts: dict[str, int] = {}
    for i in range(len(words) - 1):
        first = words[i]
        second = words[i + 1]
        if (
            first in STOPWORDS
            or second in STOPWORDS
            or len(first) < 4
            or len(second) < 4
        ):
            continue
        if first in WEAK_PHRASE_TOKENS and second in WEAK_PHRASE_TOKENS:
            continue
        has_distinctive_signal = (
            "-" in first
            or "-" in second
            or counts.get(first, 0) >= 2
            or counts.get(second, 0) >= 2
        )
        if not has_distinctive_signal and (first in WEAK_PHRASE_TOKENS or second in WEAK_PHRASE_TOKENS):
            continue
        phrase = f"{first} {second}"
        phrase_counts[phrase] = phrase_counts.get(phrase, 0) + 1

    anchors: list[str] = []
    top_phrases = sorted(phrase_counts.items(), key=lambda item: (-item[1], item[0]))[:3]
    anchors.extend(phrase for phrase, _ in top_phrases)
    covered_unigrams = {token for phrase, _ in top_phrases for token in phrase.split(" ")}
    top_tokens = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    for token, _ in top_tokens:
        if token in covered_unigrams:
            continue
        if token not in anchors:
            anchors.append(token)
        if len(anchors) >= limit:
            break
    return anchors[:limit]


def _first_matching(turns: list[str], cues: tuple[str, ...], *, limit: int = 140) -> list[str]:
    matched: list[str] = []
    for body in turns:
        if _has_cue(body, cues):
            matched.append(_sentenceish(body, limit=limit))
    return matched[:2]


def _has_cue(text: str, cues: tuple[str, ...]) -> bool:
    lowered = text.lower().replace("’", "'").replace("`", "'")
    return any(cue in lowered for cue in cues)


def build_compressed_memory_text(entry: dict[str, Any]) -> str:
    created_at = str(entry.get("created_at", "")).strip()
    entry_type = _clean_text(str(entry.get("entry_type", "")))
    source = _clean_text(str(entry.get("source", "")))
    content = str(entry.get("content", ""))
    turns = _split_turns(content)
    keywords = _extract_keywords(content)
    lines: list[str] = []
    if created_at:
        lines.append(f"Date: {created_at}")
    if entry_type or source:
        lines.append(f"Source context: type={entry_type or 'unknown'} source={source or 'unknown'}")

    if not turns:
        lines.append(f"Retrieval summary: {_sentenceish(content, limit=220)}")
        if keywords:
            lines.append(f"Retrieval anchors: {', '.join(keywords)}")
        return "\n".join(lines)

    user_turns = [body for speaker, body in turns if _speaker_label(speaker) != "Assistant"]
    assistant_turns = [body for speaker, body in turns if _speaker_label(speaker) == "Assistant"]
    question_turns = [body for speaker, body in turns if _speaker_label(speaker) != "Assistant" and "?" in body]
    decisions = _first_matching(user_turns + assistant_turns, DECISION_CUES, limit=130)
    commitments = _first_matching(assistant_turns, COMMITMENT_CUES, limit=130)
    ask_commit_pair: tuple[str, str] | None = None
    open_loops = [body for body in user_turns if "?" in body and body not in commitments]
    pending_question: str | None = None
    for speaker, body in turns:
        label = _speaker_label(speaker)
        if label != "Assistant" and "?" in body:
            pending_question = body
            continue
        if label == "Assistant" and pending_question and _has_cue(body, COMMITMENT_CUES):
            ask_commit_pair = (
                _sentenceish(pending_question, limit=110),
                _sentenceish(body, limit=110),
            )
            break

    opener = _sentenceish(turns[0][1], limit=140)
    closer = _sentenceish(turns[-1][1], limit=120)
    lines.append(f"Retrieval summary: {_speaker_label(turns[0][0])} opens with {opener}")
    if question_turns:
        lines.append(f"User asks: {' | '.join(_sentenceish(text, limit=130) for text in question_turns[:2])}")
    if user_turns:
        lines.append(f"User context: {' | '.join(_sentenceish(text, limit=130) for text in user_turns[:2])}")
    if commitments:
        lines.append(f"Assistant commitments: {' | '.join(commitments)}")
    elif assistant_turns:
        lines.append(f"Assistant response: {' | '.join(_sentenceish(text, limit=130) for text in assistant_turns[:2])}")
    if ask_commit_pair:
        lines.append(f"Ask/commit pair: User asked {ask_commit_pair[0]} -> Assistant committed {ask_commit_pair[1]}")
    if decisions:
        lines.append(f"Decisions / direction: {' | '.join(decisions)}")
    if open_loops:
        lines.append(f"Open loops: {' | '.join(_sentenceish(text, limit=130) for text in open_loops[:2])}")
    lines.append(f"Closing state: {_speaker_label(turns[-1][0])} ends with {closer}")
    if keywords:
        lines.append(f"Retrieval anchors: {', '.join(keywords)}")
    return "\n".join(lines)
