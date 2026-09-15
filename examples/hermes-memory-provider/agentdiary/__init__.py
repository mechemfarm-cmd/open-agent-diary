"""Agent Diary memory provider.

Makes Agent Diary (a local HTTP server, default http://127.0.0.1:8041) the
primary durable memory store for Hermes.

Behaviors:
  - on_memory_write() mirrors every built-in memory-tool write into the diary
    (add / replace / remove), so the diary becomes a complete, searchable,
    append-only record of the agent's durable memory.
  - prefetch() recalls relevant diary context before each turn and injects it.
  - exposes agendiary_search_memory as a first-class tool.
  - declares the diary data root so `hermes backup` preserves it.

Stdlib-only (urllib) so there is nothing to install. Fails gracefully when the
server is unreachable: writes drop silently, recall returns empty.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib import request as urlrequest

from agent.memory_provider import MemoryProvider, is_trivial_prompt

logger = logging.getLogger(__name__)

_DIARY_BASE = os.environ.get("AGENTDIARY_URL", "http://127.0.0.1:8041")
# Diary data root differs per host (Emily: ~/development/agent-diary/data,
# Art: ~/open-agent-diary/data). Override with AGENTDIARY_DATA so one copy of
# this plugin works on every machine without per-host edits.
_DIARY_DATA = os.environ.get("AGENTDIARY_DATA", "~/development/agent-diary/data")
_TIMEOUT = 3.0


def _post(path: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """POST JSON to the diary API; returns parsed response or None on failure."""
    url = f"{_DIARY_BASE}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urlrequest.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urlrequest.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — local best-effort, never raise
        logger.debug("agentdiary POST %s failed: %s", path, exc)
        return None


_NOISE_PREFIXES = (
    "[The user sent an image",
    "[IMPORTANT:",
    "[System note:",
    "=== Alice",
)

# Speaker labels that pollute raw transcript chunks. Stripped so recall shows
# content, not "sampleuser: [tom]: ..." prefixes.
_SPEAKER_RE = re.compile(
    r"\b(sampleuser|assistant|system|user)\s*:\s*|\[(tom|alice|bill)\]\s*:\s*",
    re.IGNORECASE,
)


def _clean_snippet(text: Any) -> str:
    """Return a usable snippet, or '' for system noise."""
    if not text:
        return ""
    t = str(text).strip()
    while t.startswith("..."):
        t = t[3:].lstrip()
    if any(t.startswith(m) for m in _NOISE_PREFIXES):
        return ""
    t = _SPEAKER_RE.sub("", t).strip()
    return t


class AgentDiaryProvider(MemoryProvider):
    """MemoryProvider backed by a local Agent Diary server."""

    @property
    def name(self) -> str:
        return "agentdiary"

    def is_available(self) -> bool:
        # Local server on a default URL, no credentials required.
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id or ""
        # Skip writes in non-primary contexts (cron, subagents) so their
        # system prompts don't pollute the durable memory record.
        self._agent_context = kwargs.get("agent_context", "primary") or "primary"
        self._platform = kwargs.get("platform", "") or ""

    def system_prompt_block(self) -> str:
        return (
            "Agent Diary (agentdiary) is your primary durable memory. Facts you "
            "commit with the memory tool are mirrored into Agent Diary, and "
            "relevant past context is prefetched automatically before each turn. "
            "To recall older work beyond the injected context, use "
            "`agendiary_search_memory`."
        )

    # -- recall ----------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if is_trivial_prompt(query):
            return ""
        result = _post("/search_memory", {"query": query, "limit": 8})
        if not result or not result.get("ok"):
            return ""
        matches = result.get("result", {}).get("matches", [])
        if not matches:
            return ""

        # Rank clean diary_note facts first, then compressed-memory entries,
        # then raw transcripts last.
        def _rank(m: Dict[str, Any]) -> int:
            if m.get("entry_type") == "diary_note":
                return 0
            if "compressed_memory" in (m.get("supporting_layers") or []):
                return 1
            return 2

        matches = sorted(matches, key=_rank)
        lines: List[str] = ["Relevant recall from Agent Diary (primary memory):"]
        seen: set = set()
        for m in matches:
            snippet = _clean_snippet(m.get("match_text", ""))
            if not snippet or snippet in seen:
                continue
            seen.add(snippet)
            lines.append(f"- {snippet[:220]}")
            if len(lines) - 1 >= 5:
                break
        return "\n".join(lines) if len(lines) > 1 else ""

    # -- write mirror ----------------------------------------------------

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self._agent_context != "primary":
            return
        if not content or not content.strip():
            return
        body = {
            "entry_type": "diary_note",
            "source": "hermes-memory",
            "author_role": "assistant",
            "content": content,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "title": f"memory:{target}:{action}",
            "metadata": {
                "target": target,
                "action": action,
                "memory_source": "builtin",
                "session_id": self._session_id,
                **(metadata or {}),
            },
        }
        # Fire-and-forget so the built-in memory tool never blocks on the diary.
        threading.Thread(
            target=_post, args=("/append_entry", body), daemon=True
        ).start()

    # -- tools -----------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "agendiary_search_memory",
                "description": (
                    "Search Agent Diary (the primary durable memory) for past "
                    "conversations, decisions, and facts. Use to recall anything "
                    "not already present in the injected context."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Keywords or phrase to search for",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results (1-10, default 5)",
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                },
            }
        ]

    def handle_tool_call(
        self, tool_name: str, args: Dict[str, Any], **kwargs
    ) -> str:
        if tool_name == "agendiary_search_memory":
            query = str(args.get("query", "") or "").strip()
            limit = min(max(int(args.get("limit", 5) or 5), 1), 10)
            if not query:
                return json.dumps({"error": "query is required"})
            result = _post("/search_memory", {"query": query, "limit": limit})
            if not result or not result.get("ok"):
                return json.dumps({"error": "Agent Diary search failed", "detail": result})
            matches = result.get("result", {}).get("matches", [])

            def _rank(m: Dict[str, Any]) -> int:
                if m.get("entry_type") == "diary_note":
                    return 0
                if "compressed_memory" in (m.get("supporting_layers") or []):
                    return 1
                return 2

            matches = sorted(matches, key=_rank)
            out, seen = [], set()
            for m in matches:
                snippet = _clean_snippet(m.get("match_text", ""))
                if not snippet or snippet in seen:
                    continue
                seen.add(snippet)
                out.append(
                    {
                        "entry_id": m.get("entry_id", ""),
                        "entry_type": m.get("entry_type", "chat_log"),
                        "snippet": snippet[:300],
                    }
                )
            return json.dumps({"results": out, "count": len(out)})
        raise NotImplementedError(f"agentdiary has no tool {tool_name}")

    def backup_paths(self) -> List[str]:
        return [os.path.expanduser(_DIARY_DATA)]


def register(ctx) -> None:
    """Plugin entry point — register this provider with the memory system."""
    ctx.register_memory_provider(AgentDiaryProvider())