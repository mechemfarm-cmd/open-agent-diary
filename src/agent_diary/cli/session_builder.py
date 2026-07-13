from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path
import re
from typing import Any


@dataclass
class TranscriptMessage:
    message_id: str
    created_at: str
    author_role: str
    speaker: str
    content: str
    metadata: dict[str, Any]

    @property
    def created_dt(self) -> datetime:
        return datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))


def _require_message_fields(payload: dict[str, Any], fields: list[str]) -> None:
    missing = [name for name in fields if name not in payload or payload[name] in (None, "")]
    if missing:
        raise ValueError(f"missing required message fields: {', '.join(missing)}")


def load_transcript_messages(path: Path) -> list[TranscriptMessage]:
    messages: list[TranscriptMessage] = []
    with path.open("r", encoding="utf-8") as handle:
        for idx, line in enumerate(handle, start=1):
            raw = line.strip()
            if not raw:
                continue
            obj = json.loads(raw)
            _require_message_fields(obj, ["message_id", "created_at", "author_role", "speaker", "content"])
            metadata = obj.get("metadata", {})
            if metadata is None:
                metadata = {}
            if not isinstance(metadata, dict):
                raise ValueError(f"message metadata must be an object on line {idx}")
            messages.append(
                TranscriptMessage(
                    message_id=str(obj["message_id"]),
                    created_at=str(obj["created_at"]),
                    author_role=str(obj["author_role"]),
                    speaker=str(obj["speaker"]),
                    content=str(obj["content"]),
                    metadata=metadata,
                )
            )
    messages.sort(key=lambda item: (item.created_dt, item.message_id))
    return messages


def _chunk_fingerprint(message_ids: list[str]) -> str:
    joined = "|".join(message_ids)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


STOPWORDS = {
    "the", "and", "for", "that", "this", "with", "have", "from", "your", "just", "into", "then", "they",
    "them", "what", "when", "where", "will", "would", "could", "should", "about", "there", "here", "their",
    "were", "been", "being", "also", "than", "them", "our", "you", "are", "but", "not", "too", "can",
}

ANSWER_OPENERS = (
    "yes", "no", "ok", "okay", "here", "here it is", "i found", "done", "it works", "that worked",
)

REQUEST_SIGNALS = (
    "?", "can you", "could you", "please", "need", "check", "find", "look at", "where are we", "what happened",
)

RESTART_CUES = (
    "different topic", "new topic", "separately", "switching gears", "another thing",
)

DOMAIN_TOKENS = {
    "openclaw", "codex", "gateway", "browser", "mac", "emily", "telegram", "chunking", "memory",
    "timeline", "agent", "diary", "microcontractor", "remarkable", "play", "console", "supabase",
}


def _tokenize(text: str) -> set[str]:
    lowered = text.lower()
    tokens = {token for token in re.findall(r"[a-z0-9]{3,}", lowered) if token not in STOPWORDS}
    return tokens


def _rendered_message(message: TranscriptMessage) -> str:
    return f"{message.speaker}: {message.content}"


def _message_chars(message: TranscriptMessage) -> int:
    return len(_rendered_message(message)) + 1


def _message_meta(message: TranscriptMessage, key: str) -> str | None:
    value = message.metadata.get(key)
    if value in (None, ""):
        return None
    return str(value)


def _same_metadata_key(left: TranscriptMessage, right: TranscriptMessage, key: str) -> bool:
    return _message_meta(left, key) == _message_meta(right, key)


def _has_explicit_new_thread_marker(message: TranscriptMessage) -> bool:
    for key in ("new_thread", "new_block", "thread_break", "block_break"):
        if message.metadata.get(key) is True:
            return True
    for key in ("thread_marker", "block_marker"):
        value = message.metadata.get(key)
        if isinstance(value, str) and value.strip().lower() in {"new", "start", "break"}:
            return True
    return False


def _has_hard_boundary(prev: TranscriptMessage, current: TranscriptMessage) -> bool:
    hard_keys = ("source_session_id", "source_conversation_id", "thread_id", "block_id")
    for key in hard_keys:
        left = _message_meta(prev, key)
        right = _message_meta(current, key)
        if left and right and left != right:
            return True
    return _has_explicit_new_thread_marker(current)


def _is_unresolved_request(text: str) -> bool:
    lowered = text.strip().lower()
    return any(signal in lowered for signal in REQUEST_SIGNALS)


def _has_restart_cue(message: TranscriptMessage) -> bool:
    lowered = message.content.strip().lower()
    return any(lowered.startswith(cue) for cue in RESTART_CUES)


def _is_obvious_continuation(prev: TranscriptMessage, current: TranscriptMessage) -> bool:
    parent_id = _message_meta(current, "source_parent_id")
    prev_id = _message_meta(prev, "source_message_id") or prev.message_id
    if parent_id and prev_id and parent_id == prev_id:
        return True

    lowered = current.content.strip().lower()
    if any(lowered.startswith(opener) for opener in ANSWER_OPENERS) and _is_unresolved_request(prev.content):
        return True

    prev_tokens = _tokenize(prev.content)
    current_tokens = _tokenize(current.content)
    overlap = prev_tokens & current_tokens
    if len(overlap) >= 2:
        return True

    domain_overlap = (prev_tokens & DOMAIN_TOKENS) & current_tokens
    if domain_overlap:
        return True

    return False


def build_session_entries(
    messages: list[TranscriptMessage],
    *,
    source: str,
    gap_minutes: int = 60,
    max_chars: int = 6000,
    max_messages: int = 80,
    min_messages_before_gap_split: int = 4,
    min_chars_before_gap_split: int = 400,
) -> list[dict[str, Any]]:
    if gap_minutes < 1:
        raise ValueError("gap_minutes must be >= 1")
    if max_chars < 200:
        raise ValueError("max_chars must be >= 200")
    if max_messages < 1:
        raise ValueError("max_messages must be >= 1")
    if min_messages_before_gap_split < 0:
        raise ValueError("min_messages_before_gap_split must be >= 0")
    if min_chars_before_gap_split < 0:
        raise ValueError("min_chars_before_gap_split must be >= 0")
    if not messages:
        return []

    chunks: list[list[TranscriptMessage]] = []
    current: list[TranscriptMessage] = []
    current_chars = 0
    gap_seconds = gap_minutes * 60

    for message in messages:
        rendered = _rendered_message(message)
        rendered_chars = len(rendered) + 1
        start_new = False
        if current:
            prev = current[-1]
            delta = (message.created_dt - prev.created_dt).total_seconds()
            if _has_hard_boundary(prev, message):
                start_new = True
            elif len(current) >= max_messages:
                start_new = True
            elif current_chars + rendered_chars > max_chars:
                start_new = True
            elif delta > gap_seconds:
                current_is_small = len(current) < min_messages_before_gap_split or current_chars < min_chars_before_gap_split
                if _has_restart_cue(message):
                    start_new = True
                elif not _is_obvious_continuation(prev, message) and not current_is_small:
                    start_new = True

        if start_new:
            chunks.append(current)
            current = []
            current_chars = 0

        current.append(message)
        current_chars += rendered_chars

    if current:
        chunks.append(current)

    entries: list[dict[str, Any]] = []
    for chunk in chunks:
        message_ids = [m.message_id for m in chunk]
        author_roles = {m.author_role for m in chunk}
        author_role = next(iter(author_roles)) if len(author_roles) == 1 else "mixed"
        content = "\n".join(f"{m.speaker}: {m.content}" for m in chunk)
        created_at = chunk[0].created_at
        chunk_id = f"{source}:{chunk[0].message_id}:{chunk[-1].message_id}:{_chunk_fingerprint(message_ids)}"
        entries.append(
            {
                "entry_type": "chat_log",
                "source": source,
                "author_role": author_role,
                "created_at": created_at,
                "content": content,
                "metadata": {
                    "source_item_id": chunk_id,
                    "source_message_ids": message_ids,
                    "message_count": len(message_ids),
                    "chunk_start_message_id": chunk[0].message_id,
                    "chunk_end_message_id": chunk[-1].message_id,
                    **(
                        {"source_session_id": _message_meta(chunk[0], "source_session_id")}
                        if _message_meta(chunk[0], "source_session_id")
                        else {}
                    ),
                    **(
                        {"source_conversation_id": _message_meta(chunk[0], "source_conversation_id")}
                        if _message_meta(chunk[0], "source_conversation_id")
                        else {}
                    ),
                },
            }
        )
    return entries


def build_session_jsonl(
    *,
    input_path: Path,
    output_path: Path,
    source: str,
    gap_minutes: int = 60,
    max_chars: int = 6000,
    max_messages: int = 80,
    min_messages_before_gap_split: int = 4,
    min_chars_before_gap_split: int = 400,
) -> dict[str, Any]:
    messages = load_transcript_messages(input_path)
    entries = build_session_entries(
        messages,
        source=source,
        gap_minutes=gap_minutes,
        max_chars=max_chars,
        max_messages=max_messages,
        min_messages_before_gap_split=min_messages_before_gap_split,
        min_chars_before_gap_split=min_chars_before_gap_split,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False))
            handle.write("\n")
    return {
        "input_path": str(input_path),
        "output_path": str(output_path),
        "message_count": len(messages),
        "entry_count": len(entries),
        "source": source,
        "gap_minutes": gap_minutes,
        "max_chars": max_chars,
        "max_messages": max_messages,
        "min_messages_before_gap_split": min_messages_before_gap_split,
        "min_chars_before_gap_split": min_chars_before_gap_split,
    }
