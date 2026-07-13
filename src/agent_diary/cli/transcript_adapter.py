from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CANONICAL_TRANSCRIPT_FIELDS = [
    "message_id",
    "created_at",
    "author_role",
    "speaker",
    "content",
]

SUPPORTED_ADAPTER_FORMATS = [
    "generic-message-jsonl",
    "openclaw-session-json",
    "openclaw-telegram-jsonl",
    "openclaw-session-jsonl",
]

OPENCLAW_SYNTHETIC_PROMPT_PREFIXES = (
    "[cron:",
    "[heartbeat",
    "[wake:",
    "[system:",
)


def _normalize_author_role(value: str) -> str:
    role = value.strip().lower()
    if role in {"human", "user"}:
        return "human"
    if role in {"agent", "assistant"}:
        return "agent"
    if role in {"system", "tool", "mixed"}:
        return role
    return role


def _author_role_from_telegram_message(message: dict[str, Any]) -> str:
    sender = message.get("from", {})
    if isinstance(sender, dict) and sender.get("is_bot") is True:
        return "agent"
    return "human"


def _speaker_from_telegram_message(message: dict[str, Any], author_role: str) -> str:
    sender = message.get("from", {})
    if isinstance(sender, dict):
        for key in ("username", "first_name"):
            if sender.get(key) not in (None, ""):
                return _non_empty_str(sender[key])
        first = str(sender.get("first_name", "")).strip()
        last = str(sender.get("last_name", "")).strip()
        full = f"{first} {last}".strip()
        if full:
            return full
    return "Agent" if author_role == "agent" else "Human"


def _iso_from_unix_seconds(value: Any, *, line_hint: str) -> str:
    try:
        ts = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{line_hint}: invalid unix timestamp") from exc
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _parse_iso_datetime(value: str, *, line_hint: str) -> datetime:
    text = _non_empty_str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{line_hint}: invalid ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _non_empty_str(value: Any) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError("value must be a non-empty string")
    return text


def _require_message_id(raw: dict[str, Any], *, line_hint: str) -> str:
    for key in ("message_id", "source_message_id", "id"):
        if raw.get(key) not in (None, ""):
            return _non_empty_str(raw[key])
    raise ValueError(f"{line_hint}: missing message id (message_id/source_message_id/id)")


def _require_created_at(raw: dict[str, Any], *, line_hint: str) -> str:
    for key in ("created_at", "timestamp"):
        if raw.get(key) not in (None, ""):
            return _non_empty_str(raw[key])
    raise ValueError(f"{line_hint}: missing created_at/timestamp")


def _require_content(raw: dict[str, Any], *, line_hint: str) -> str:
    for key in ("content", "text", "message"):
        if raw.get(key) not in (None, ""):
            return _non_empty_str(raw[key])
    raise ValueError(f"{line_hint}: missing content/text/message")


def _require_role(raw: dict[str, Any], *, line_hint: str) -> str:
    for key in ("author_role", "role"):
        if raw.get(key) not in (None, ""):
            return _normalize_author_role(_non_empty_str(raw[key]))
    raise ValueError(f"{line_hint}: missing author_role/role")


def _normalize_speaker(raw: dict[str, Any], author_role: str) -> str:
    for key in ("speaker", "author", "name"):
        if raw.get(key) not in (None, ""):
            return _non_empty_str(raw[key])
    if author_role == "human":
        return "Human"
    if author_role == "agent":
        return "Agent"
    return author_role.capitalize()


def _normalize_metadata(raw: dict[str, Any], *, line_hint: str) -> dict[str, Any]:
    metadata = raw.get("metadata", {})
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise ValueError(f"{line_hint}: metadata must be an object")
    return dict(metadata)


def _extract_text_chunks(value: Any) -> list[str]:
    chunks: list[str] = []
    if isinstance(value, str):
        text = value.strip()
        if text:
            chunks.append(text)
        return chunks
    if isinstance(value, list):
        for item in value:
            chunks.extend(_extract_text_chunks(item))
        return chunks
    if isinstance(value, dict):
        if value.get("type") == "text":
            text = value.get("text")
            if text not in (None, ""):
                chunks.append(_non_empty_str(text))
    return chunks


def _is_synthetic_openclaw_prompt(text: str) -> bool:
    lowered = text.strip().lower()
    return any(lowered.startswith(prefix) for prefix in OPENCLAW_SYNTHETIC_PROMPT_PREFIXES)


def _load_openclaw_session_jsonl(path: Path) -> tuple[list[dict[str, Any]], str | None, str | None]:
    rows: list[dict[str, Any]] = []
    session_id: str | None = None
    session_timestamp: str | None = None
    session_cwd: str | None = None
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            raw = line.strip()
            if not raw:
                continue
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError(f"line {line_number}: session log record must be an object")

            record_type = obj.get("type")
            if record_type == "session":
                if obj.get("id") not in (None, ""):
                    session_id = _non_empty_str(obj.get("id"))
                if obj.get("timestamp") not in (None, ""):
                    session_timestamp = _non_empty_str(obj.get("timestamp"))
                if obj.get("cwd") not in (None, ""):
                    session_cwd = _non_empty_str(obj.get("cwd"))
                continue

            if record_type != "message":
                continue

            message = obj.get("message")
            if not isinstance(message, dict):
                raise ValueError(f"line {line_number}: missing nested message object")

            role = message.get("role")
            if role not in {"user", "assistant"}:
                continue

            message_id = obj.get("id")
            if message_id in (None, ""):
                raise ValueError(f"line {line_number}: missing top-level id")
            created_at = obj.get("timestamp") or message.get("timestamp")
            if created_at in (None, ""):
                raise ValueError(f"line {line_number}: missing timestamp")

            metadata: dict[str, Any] = {
                "source_message_id": _non_empty_str(message_id),
                "source_record_type": record_type,
                "source_parent_id": obj.get("parentId"),
                "source_record_timestamp": _non_empty_str(obj.get("timestamp")) if obj.get("timestamp") not in (None, "") else None,
                "source_message_timestamp": _non_empty_str(message.get("timestamp")) if message.get("timestamp") not in (None, "") else None,
                "openclaw_message_role": role,
                "openclaw_record_id": _non_empty_str(message_id),
            }
            if session_id:
                metadata["source_session_id"] = session_id
            if session_timestamp:
                metadata["source_session_timestamp"] = session_timestamp
            if session_cwd:
                metadata["source_session_cwd"] = session_cwd
            metadata = {k: v for k, v in metadata.items() if v not in (None, "")}

            if role == "user":
                content = message.get("content")
                if isinstance(content, str):
                    text = content.strip()
                else:
                    text = "\n".join(_extract_text_chunks(content)).strip()
                if not text:
                    continue
                if _is_synthetic_openclaw_prompt(text):
                    continue
                speaker = _normalize_speaker({"speaker": "User"}, "human")
                rows.append(
                    {
                        "line_hint": f"line {line_number}",
                        "message": {
                            "message_id": _non_empty_str(message_id),
                            "created_at": _non_empty_str(created_at),
                            "author_role": "human",
                            "speaker": speaker,
                            "content": text,
                            "metadata": metadata,
                        },
                    }
                )
                continue

            content_items = message.get("content")
            text_chunks = _extract_text_chunks(content_items)
            if not text_chunks:
                continue
            rows.append(
                {
                    "line_hint": f"line {line_number}",
                    "message": {
                        "message_id": _non_empty_str(message_id),
                        "created_at": _non_empty_str(created_at),
                        "author_role": "agent",
                        "speaker": _normalize_speaker({"speaker": "Assistant"}, "agent"),
                        "content": "\n".join(text_chunks).strip(),
                        "metadata": metadata,
                    },
                }
            )
    return rows, session_id, session_cwd


def _normalize_message(
    raw: dict[str, Any],
    *,
    line_hint: str,
    source_session_id: str | None,
    source_conversation_id: str | None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{line_hint}: each message must be an object")

    message_id = _require_message_id(raw, line_hint=line_hint)
    created_at = _require_created_at(raw, line_hint=line_hint)
    author_role = _require_role(raw, line_hint=line_hint)
    content = _require_content(raw, line_hint=line_hint)
    speaker = _normalize_speaker(raw, author_role)
    metadata = _normalize_metadata(raw, line_hint=line_hint)

    metadata.setdefault("source_message_id", message_id)
    if source_session_id:
        metadata.setdefault("source_session_id", source_session_id)
    if source_conversation_id:
        metadata.setdefault("source_conversation_id", source_conversation_id)

    return {
        "message_id": message_id,
        "created_at": created_at,
        "author_role": author_role,
        "speaker": speaker,
        "content": content,
        "metadata": metadata,
    }


def _load_generic_message_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            raw = line.strip()
            if not raw:
                continue
            item = json.loads(raw)
            rows.append({"line_hint": f"line {line_number}", "message": item})
    return rows


def _load_openclaw_session_json(path: Path) -> tuple[list[dict[str, Any]], str | None, str | None]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("openclaw-session-json must be a JSON object")
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError("openclaw-session-json must include a messages list")
    session_id = payload.get("session_id")
    conversation_id = payload.get("conversation_id")
    rows = [{"line_hint": f"messages[{idx}]", "message": m} for idx, m in enumerate(messages)]
    return rows, (str(session_id) if session_id else None), (str(conversation_id) if conversation_id else None)


def _load_openclaw_telegram_jsonl(path: Path) -> tuple[list[dict[str, Any]], str | None, str | None]:
    rows: list[dict[str, Any]] = []
    inferred_session: str | None = None
    inferred_conversation: str | None = None
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            raw = line.strip()
            if not raw:
                continue
            outer = json.loads(raw)
            if not isinstance(outer, dict):
                raise ValueError(f"line {line_number}: telegram record must be an object")
            node = outer.get("node", {})
            if not isinstance(node, dict):
                raise ValueError(f"line {line_number}: node must be an object")
            source_message = node.get("sourceMessage")
            if not isinstance(source_message, dict):
                raise ValueError(f"line {line_number}: missing node.sourceMessage object")

            role = _author_role_from_telegram_message(source_message)
            speaker = _speaker_from_telegram_message(source_message, role)
            content = _require_content(source_message, line_hint=f"line {line_number}")
            created_at = _iso_from_unix_seconds(source_message.get("date"), line_hint=f"line {line_number}")
            message_id = _non_empty_str(source_message.get("message_id"))

            metadata = {
                "source_message_id": message_id,
                "source_key": outer.get("key"),
                "telegram_chat_id": source_message.get("chat", {}).get("id") if isinstance(source_message.get("chat"), dict) else None,
                "telegram_from_id": source_message.get("from", {}).get("id") if isinstance(source_message.get("from"), dict) else None,
                "telegram_username": source_message.get("from", {}).get("username") if isinstance(source_message.get("from"), dict) else None,
            }
            metadata = {k: v for k, v in metadata.items() if v not in (None, "")}

            chat = source_message.get("chat", {})
            if isinstance(chat, dict):
                chat_id = chat.get("id")
                if chat_id not in (None, "") and inferred_conversation is None:
                    inferred_conversation = f"telegram:{chat_id}"
            if inferred_session is None and outer.get("key") not in (None, ""):
                inferred_session = _non_empty_str(outer.get("key"))

            rows.append(
                {
                    "line_hint": f"line {line_number}",
                    "message": {
                        "message_id": message_id,
                        "created_at": created_at,
                        "author_role": role,
                        "speaker": speaker,
                        "content": content,
                        "metadata": metadata,
                    },
                }
            )
    if inferred_session and inferred_conversation is None:
        inferred_conversation = f"openclaw:{inferred_session}"
    return rows, inferred_session, inferred_conversation


def _build_telegram_inbound_row(
    *,
    source_message: dict[str, Any],
    line_hint: str,
    source_key: str | None = None,
    source_store: str | None = None,
) -> dict[str, Any]:
    role = _author_role_from_telegram_message(source_message)
    speaker = _speaker_from_telegram_message(source_message, role)
    content = _require_content(source_message, line_hint=line_hint)
    created_at = _iso_from_unix_seconds(source_message.get("date"), line_hint=line_hint)
    message_id = _non_empty_str(source_message.get("message_id"))
    chat = source_message.get("chat", {})
    chat_value = chat.get("id") if isinstance(chat, dict) else None
    sender = source_message.get("from", {})
    metadata = {
        "transport": "telegram",
        "telegram_direction": "inbound",
        "source_message_id": message_id,
        "source_key": source_key,
        "source_store": source_store,
        "telegram_chat_id": chat_value,
        "telegram_from_id": sender.get("id") if isinstance(sender, dict) else None,
        "telegram_username": sender.get("username") if isinstance(sender, dict) else None,
        "is_bot": sender.get("is_bot") if isinstance(sender, dict) else None,
    }
    metadata = {k: v for k, v in metadata.items() if v not in (None, "")}
    return {
        "line_hint": line_hint,
        "message": {
            "message_id": message_id,
            "created_at": created_at,
            "author_role": role,
            "speaker": speaker,
            "content": content,
            "metadata": metadata,
        },
    }


def _load_openclaw_telegram_plugin_state_sqlite(path: Path, *, chat_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with sqlite3.connect(path) as conn:
        cursor = conn.execute(
            """
            SELECT entry_key, value_json
            FROM plugin_state_entries
            WHERE namespace = ?
              AND entry_key LIKE ?
            ORDER BY created_at ASC, entry_key ASC
            """,
            ("telegram.message-cache", f"%:{chat_id}:%"),
        )
        for entry_key, value_json in cursor.fetchall():
            payload = json.loads(value_json)
            if not isinstance(payload, dict):
                continue
            source_message = payload.get("sourceMessage")
            if not isinstance(source_message, dict):
                continue
            chat = source_message.get("chat", {})
            chat_value = chat.get("id") if isinstance(chat, dict) else None
            if str(chat_value) != str(chat_id):
                continue
            sender = source_message.get("from", {})
            if isinstance(sender, dict) and sender.get("is_bot") is True:
                continue
            rows.append(
                _build_telegram_inbound_row(
                    source_message=source_message,
                    line_hint=f"sqlite:{entry_key}",
                    source_key=str(entry_key),
                    source_store="openclaw-plugin-state-sqlite",
                )
            )
    return rows


def _load_openclaw_telegram_inbound_source(path: Path, *, chat_id: str) -> list[dict[str, Any]]:
    if path.suffix == ".sqlite":
        return _load_openclaw_telegram_plugin_state_sqlite(path, chat_id=chat_id)

    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            raw = line.strip()
            if not raw:
                continue
            outer = json.loads(raw)
            if not isinstance(outer, dict):
                raise ValueError(f"line {line_number}: telegram record must be an object")
            node = outer.get("node", {})
            if not isinstance(node, dict):
                raise ValueError(f"line {line_number}: node must be an object")
            source_message = node.get("sourceMessage")
            if not isinstance(source_message, dict):
                raise ValueError(f"line {line_number}: missing node.sourceMessage object")
            chat = source_message.get("chat", {})
            chat_value = chat.get("id") if isinstance(chat, dict) else None
            if str(chat_value) != str(chat_id):
                continue
            rows.append(
                _build_telegram_inbound_row(
                    source_message=source_message,
                    line_hint=f"line {line_number}",
                    source_key=outer.get("key"),
                    source_store="openclaw-telegram-jsonl",
                )
            )
    return rows


def _parse_tool_result_payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _message_send_call_targets_current_conversation(arguments: dict[str, Any]) -> bool:
    explicit_target_keys = {
        "target",
        "targets",
        "channel",
        "channelId",
        "channelIds",
        "chatId",
        "threadId",
        "guildId",
        "groupId",
        "accountId",
        "openId",
        "participant",
        "unionId",
    }
    for key in explicit_target_keys:
        value = arguments.get(key)
        if value not in (None, "", []):
            return False
    return True


def _assistant_sent_messages_from_session_files(
    *,
    sessions_root: Path,
    chat_id: str,
    session_files: list[Path] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if session_files is None:
        candidate_files = sorted(sessions_root.glob("*.jsonl"))
    else:
        candidate_files = sorted({Path(path).expanduser().resolve() for path in session_files})
    for path in candidate_files:
        if not path.exists():
            continue
        pending: dict[str, dict[str, Any]] = {}
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                raw = line.strip()
                if not raw:
                    continue
                obj = json.loads(raw)
                if not isinstance(obj, dict) or obj.get("type") != "message":
                    continue
                message = obj.get("message")
                if not isinstance(message, dict):
                    continue
                role = message.get("role")
                if role == "assistant":
                    content = message.get("content")
                    if not isinstance(content, list):
                        continue
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") != "toolCall" or item.get("name") != "message":
                            continue
                        arguments = item.get("arguments")
                        if not isinstance(arguments, dict):
                            arguments = item.get("input")
                        if not isinstance(arguments, dict):
                            continue
                        if arguments.get("action") != "send":
                            continue
                        text = arguments.get("message")
                        tool_call_id = item.get("id")
                        if text in (None, "") or tool_call_id in (None, ""):
                            continue
                        pending[str(tool_call_id)] = {
                            "message": _non_empty_str(text),
                            "call_timestamp": _non_empty_str(obj.get("timestamp") or message.get("timestamp")),
                            "runtime_record_id": _non_empty_str(obj.get("id")),
                            "session_file": str(path.resolve()),
                            "targets_current_conversation": _message_send_call_targets_current_conversation(arguments),
                        }
                    continue

                if role != "toolResult":
                    continue
                if message.get("toolName") != "message":
                    continue
                tool_call_id = message.get("toolCallId")
                if tool_call_id in (None, ""):
                    continue
                pending_item = pending.get(str(tool_call_id))
                if pending_item is None:
                    continue
                content = message.get("content")
                payload: dict[str, Any] | None = None
                if isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        payload = _parse_tool_result_payload(item.get("content")) or _parse_tool_result_payload(item.get("text"))
                        if payload is not None:
                            break
                if payload is None:
                    continue
                payload_chat_id = payload.get("chatId")
                payload_message_id = payload.get("messageId")
                if payload_message_id in (None, ""):
                    continue
                if payload_chat_id in (None, ""):
                    if pending_item.get("targets_current_conversation") is not True:
                        continue
                    payload_chat_id = chat_id
                elif str(payload_chat_id) != str(chat_id):
                    continue
                created_at = _non_empty_str(obj.get("timestamp") or message.get("timestamp"))
                rows.append(
                    {
                        "line_hint": f"{path.name}:line {line_number}",
                        "message": {
                            "message_id": _non_empty_str(payload_message_id),
                            "created_at": created_at,
                            "author_role": "agent",
                            "speaker": "Assistant",
                            "content": pending_item["message"],
                            "metadata": {
                                "transport": "telegram",
                                "telegram_chat_id": payload_chat_id,
                                "telegram_direction": "outbound",
                                "source_message_id": _non_empty_str(payload_message_id),
                                "source_runtime_tool_call_id": str(tool_call_id),
                                "source_runtime_record_id": pending_item["runtime_record_id"],
                                "source_runtime_session_file": pending_item["session_file"],
                                "source_runtime_call_timestamp": pending_item["call_timestamp"],
                            },
                        },
                    }
                )
                pending.pop(str(tool_call_id), None)
    return rows


def build_openclaw_telegram_direct_transcript(
    *,
    inbound_path: Path,
    sessions_root: Path,
    output_path: Path,
    chat_id: str,
    source_session_id: str | None = None,
    source_conversation_id: str | None = None,
    session_files: list[Path] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    rows = _load_openclaw_telegram_inbound_source(inbound_path, chat_id=chat_id)
    first_inbound_at: datetime | None = None
    filtered_rows: list[dict[str, Any]] = []
    for row in rows:
        created_at_dt = _parse_iso_datetime(
            row["message"]["created_at"],
            line_hint=row["line_hint"],
        )
        if since and created_at_dt < since:
            continue
        if until and created_at_dt >= until:
            continue
        filtered_rows.append(row)
        if first_inbound_at is None or created_at_dt < first_inbound_at:
            first_inbound_at = created_at_dt
    rows = filtered_rows

    outbound_rows = _assistant_sent_messages_from_session_files(
        sessions_root=sessions_root,
        chat_id=chat_id,
        session_files=session_files,
    )
    if first_inbound_at is not None:
        filtered_outbound_rows: list[dict[str, Any]] = []
        for row in outbound_rows:
            outbound_at = _parse_iso_datetime(
                row["message"]["created_at"],
                line_hint=row["line_hint"],
            )
            if since and outbound_at < since:
                continue
            if until and outbound_at >= until:
                continue
            if outbound_at < first_inbound_at:
                continue
            filtered_outbound_rows.append(row)
        outbound_rows = filtered_outbound_rows
    rows.extend(outbound_rows)
    effective_session_id = source_session_id or f"telegram-direct:{chat_id}"
    effective_conversation_id = source_conversation_id or f"telegram:{chat_id}"
    messages: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for row in rows:
        normalized = _normalize_message(
            row["message"],
            line_hint=row["line_hint"],
            source_session_id=effective_session_id,
            source_conversation_id=effective_conversation_id,
        )
        dedupe_key = f'{normalized["author_role"]}:{normalized["message_id"]}'
        if dedupe_key in seen_ids:
            continue
        seen_ids.add(dedupe_key)
        messages.append(normalized)
    messages.sort(key=lambda item: (item["created_at"], item["message_id"], item["author_role"]))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for message in messages:
            handle.write(json.dumps(message, ensure_ascii=False))
            handle.write("\n")
    inbound_count = sum(1 for message in messages if message["metadata"].get("telegram_direction") == "inbound")
    outbound_count = sum(1 for message in messages if message["metadata"].get("telegram_direction") == "outbound")
    return {
        "input_path": str(inbound_path),
        "sessions_root": str(sessions_root),
        "output_path": str(output_path),
        "format": "openclaw-telegram-direct",
        "message_count": len(messages),
        "inbound_count": inbound_count,
        "outbound_count": outbound_count,
        "schema_fields": CANONICAL_TRANSCRIPT_FIELDS,
        "source_session_id": effective_session_id,
        "source_conversation_id": effective_conversation_id,
        "chat_id": str(chat_id),
    }


def adapt_session_export(
    *,
    input_path: Path,
    output_path: Path,
    format_name: str,
    source_session_id: str | None = None,
    source_conversation_id: str | None = None,
) -> dict[str, Any]:
    if format_name not in SUPPORTED_ADAPTER_FORMATS:
        raise ValueError(f"unsupported format: {format_name}")

    if format_name == "generic-message-jsonl":
        rows = _load_generic_message_jsonl(input_path)
        inferred_session = None
        inferred_conversation = None
    elif format_name == "openclaw-session-json":
        rows, inferred_session, inferred_conversation = _load_openclaw_session_json(input_path)
    elif format_name == "openclaw-session-jsonl":
        rows, inferred_session, inferred_conversation = _load_openclaw_session_jsonl(input_path)
        if inferred_session:
            inferred_conversation = f"openclaw:{inferred_session}"
    else:
        rows, inferred_session, inferred_conversation = _load_openclaw_telegram_jsonl(input_path)

    effective_session_id = source_session_id or inferred_session
    effective_conversation_id = source_conversation_id or inferred_conversation

    messages: list[dict[str, Any]] = []
    for row in rows:
        messages.append(
            _normalize_message(
                row["message"],
                line_hint=row["line_hint"],
                source_session_id=effective_session_id,
                source_conversation_id=effective_conversation_id,
            )
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for message in messages:
            handle.write(json.dumps(message, ensure_ascii=False))
            handle.write("\n")

    return {
        "input_path": str(input_path),
        "output_path": str(output_path),
        "format": format_name,
        "message_count": len(messages),
        "schema_fields": CANONICAL_TRANSCRIPT_FIELDS,
        "source_session_id": effective_session_id,
        "source_conversation_id": effective_conversation_id,
    }
