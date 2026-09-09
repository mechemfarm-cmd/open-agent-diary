from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from agent_diary.cli.session_builder import build_session_jsonl
from agent_diary.cli.openclaw_session_import import (
    backfill_openclaw_session_key,
    backfill_telegram_direct,
    import_openclaw_session,
    import_telegram_direct,
)
from agent_diary.cli.openclaw_work_trace_import import (
    backfill_openclaw_work_trace_session_key,
    import_openclaw_work_trace,
)
from agent_diary.cli.transcript_adapter import SUPPORTED_ADAPTER_FORMATS, adapt_session_export, build_openclaw_telegram_direct_transcript
from agent_diary.config import default_paths
from agent_diary.index.sqlite_index import bootstrap_sqlite
from agent_diary.service.handlers import (
    append_entry,
    append_overlay,
    append_work_trace_event,
    attach_artifact,
    fetch_entry_detail,
    fetch_raw_entry,
    fetch_work_trace_event,
    import_session_and_refresh_derived,
    import_session_jsonl,
    list_entries,
    list_imports,
    list_work_trace,
    produce_conversation_briefs,
    produce_compressed_memory,
    produce_open_loops,
    normalize_derived_artifact_lifecycle,
    refresh_derived_for_import,
    search_all,
    search_memory,
    search_work_trace,
    graph_add_fact,
    graph_backfill,
    graph_backfill_status,
    graph_claim_jobs,
    graph_correct_fact,
    graph_enqueue_recent,
    graph_explain_fact,
    graph_fail_extraction,
    graph_fetch_source,
    graph_find_entity,
    graph_get_entity,
    graph_get_subgraph,
    graph_merge_alias,
    graph_neighbors,
    graph_queue_status,
    graph_search,
    graph_split_entity,
    graph_submit_extraction,
)
from agent_diary.service.http_server import run_server
from agent_diary.storage.files import ensure_data_dirs


def _print(output: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(output, indent=2))
    else:
        print(output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-diary",
        description="Local-first Agent Diary CLI for raw entries, memory artifacts, and truthful recurring imports.",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON output")

    sub = parser.add_subparsers(dest="command", required=True)

    p_append = sub.add_parser("append-entry")
    p_append.add_argument("--entry-type", required=True)
    p_append.add_argument("--source", required=True)
    p_append.add_argument("--author-role", required=True)
    p_append.add_argument("--content", required=True)
    p_append.add_argument("--created-at", required=True)
    p_append.add_argument("--title")
    p_append.add_argument("--metadata", default="{}")

    p_work_trace = sub.add_parser("append-work-trace")
    p_work_trace.add_argument("--event-type", required=True)
    p_work_trace.add_argument("--summary", required=True)
    p_work_trace.add_argument("--created-at")
    p_work_trace.add_argument("--project")
    p_work_trace.add_argument("--source-surface")
    p_work_trace.add_argument("--actor")
    p_work_trace.add_argument("--session-key")
    p_work_trace.add_argument("--task-id")
    p_work_trace.add_argument("--details", default="{}")
    p_work_trace.add_argument("--related-entry-ids", default="[]")
    p_work_trace.add_argument("--related-artifact-ids", default="[]")
    p_work_trace.add_argument("--related-paths", default="[]")
    p_work_trace.add_argument("--tags", default="[]")

    p_artifact = sub.add_parser("attach-artifact")
    p_artifact.add_argument("--entry-id", required=True)
    p_artifact.add_argument("--artifact-type", required=True)
    p_artifact.add_argument("--producer", required=True)
    p_artifact.add_argument("--content", required=True)
    p_artifact.add_argument("--created-at")
    p_artifact.add_argument("--metadata", default="{}")

    p_overlay = sub.add_parser("append-overlay")
    p_overlay.add_argument("--entry-id", required=True)
    p_overlay.add_argument("--overlay-type", required=True)
    p_overlay.add_argument("--author", required=True)
    p_overlay.add_argument("--content", required=True)
    p_overlay.add_argument("--created-at")
    p_overlay.add_argument("--metadata", default="{}")

    p_search = sub.add_parser("search-memory")
    p_search.add_argument("--query", required=True)
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--filters", default="{}")
    p_search.add_argument("--source-conversation-id", help="optional provenance filter")
    p_search.add_argument("--source-session-id", help="optional provenance filter")
    p_search.add_argument("--import-id", help="optional provenance filter")
    p_search.add_argument("--truthful-only", action="store_true", help="include only truthful imported entries")

    p_search_work_trace = sub.add_parser("search-work-trace")
    p_search_work_trace.add_argument("--query", required=True)
    p_search_work_trace.add_argument("--limit", type=int, default=20)
    p_search_work_trace.add_argument("--event-type")
    p_search_work_trace.add_argument("--project")

    p_search_all = sub.add_parser("search-all")
    p_search_all.add_argument("--query", required=True)
    p_search_all.add_argument("--limit", type=int, default=20)
    p_search_all.add_argument("--filters", default="{}")
    p_search_all.add_argument("--source-conversation-id", help="optional provenance filter")
    p_search_all.add_argument("--source-session-id", help="optional provenance filter")
    p_search_all.add_argument("--import-id", help="optional provenance filter")
    p_search_all.add_argument("--truthful-only", action="store_true", help="include only truthful imported entries")
    p_search_all.add_argument("--event-type")
    p_search_all.add_argument("--project")

    p_fetch = sub.add_parser("fetch-raw-entry")
    p_fetch.add_argument("--entry-id", required=True)
    p_fetch.add_argument("--include-overlays", action="store_true")
    p_fetch.add_argument("--include-artifacts", action="store_true")

    p_fetch_work_trace = sub.add_parser("fetch-work-trace")
    p_fetch_work_trace.add_argument("--event-id", required=True)

    p_list = sub.add_parser("list-entries")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--offset", type=int, default=0)
    p_list.add_argument("--source-conversation-id", help="optional provenance filter")
    p_list.add_argument("--source-session-id", help="optional provenance filter")
    p_list.add_argument("--import-id", help="optional provenance filter")
    p_list.add_argument("--truthful-only", action="store_true", help="include only truthful imported entries")
    p_list.add_argument("--filters", default="{}", help="optional JSON object merged with explicit provenance filters")

    p_list_work_trace = sub.add_parser("list-work-trace")
    p_list_work_trace.add_argument("--limit", type=int, default=20)
    p_list_work_trace.add_argument("--offset", type=int, default=0)
    p_list_work_trace.add_argument("--event-type")
    p_list_work_trace.add_argument("--project")

    p_detail = sub.add_parser("fetch-entry-detail")
    p_detail.add_argument("--entry-id", required=True)

    p_open_loops = sub.add_parser("produce-open-loops")
    p_open_loops.add_argument("--limit", type=int, default=20)
    p_open_loops.add_argument("--entry-ids", nargs="*", default=None)
    p_open_loops.add_argument("--source-conversation-id", help="optional provenance filter")
    p_open_loops.add_argument("--source-session-id", help="optional provenance filter")
    p_open_loops.add_argument("--import-id", help="optional provenance filter")
    p_open_loops.add_argument("--truthful-only", action="store_true", help="include only truthful imported entries")
    p_open_loops.add_argument("--filters", default="{}", help="optional JSON object merged with explicit provenance filters")

    p_briefs = sub.add_parser("produce-conversation-briefs")
    p_briefs.add_argument("--limit", type=int, default=20)
    p_briefs.add_argument("--entry-ids", nargs="*", default=None)
    p_briefs.add_argument("--force", action="store_true")
    p_briefs.add_argument("--source-conversation-id", help="optional provenance filter")
    p_briefs.add_argument("--source-session-id", help="optional provenance filter")
    p_briefs.add_argument("--import-id", help="optional provenance filter")
    p_briefs.add_argument("--truthful-only", action="store_true", help="include only truthful imported entries")
    p_briefs.add_argument("--filters", default="{}", help="optional JSON object merged with explicit provenance filters")

    p_memory = sub.add_parser("produce-compressed-memory")
    p_memory.add_argument("--limit", type=int, default=20)
    p_memory.add_argument("--entry-ids", nargs="*", default=None)
    p_memory.add_argument("--force", action="store_true")
    p_memory.add_argument("--source-conversation-id", help="optional provenance filter")
    p_memory.add_argument("--source-session-id", help="optional provenance filter")
    p_memory.add_argument("--import-id", help="optional provenance filter")
    p_memory.add_argument("--truthful-only", action="store_true", help="include only truthful imported entries")
    p_memory.add_argument("--filters", default="{}", help="optional JSON object merged with explicit provenance filters")

    p_import = sub.add_parser("import-entries-jsonl")
    p_import.add_argument("--path", required=True)

    p_session_import = sub.add_parser(
        "import-session-jsonl",
        help="import session-import JSONL after transcript adaptation and session chunking",
        description="Import canonical session-import JSONL. Raw entries remain authoritative; duplicate source items are skipped through the import ledger.",
    )
    p_session_import.add_argument("--path", required=True, help="path to session-import JSONL")
    p_session_import.add_argument("--import-id", help="optional batch id; defaults to a readable id when omitted")
    p_session_import.add_argument("--source-session-id", help="override or supply the source session id")
    p_session_import.add_argument("--source-conversation-id", help="override or supply the source conversation id")
    p_session_import.add_argument("--dry-run", action="store_true", help="plan the import without writing raw entries")

    p_session_import_and_analyze = sub.add_parser(
        "import-session-and-analyze",
        help="import session-import JSONL and refresh core derived artifacts in one step",
        description="Import canonical session-import JSONL, then produce conversation briefs, compressed memory, and open-loop analysis for imported entries.",
    )
    p_session_import_and_analyze.add_argument("--path", required=True, help="path to session-import JSONL")
    p_session_import_and_analyze.add_argument("--import-id", help="optional batch id; defaults to a readable id when omitted")
    p_session_import_and_analyze.add_argument("--source-session-id", help="override or supply the source session id")
    p_session_import_and_analyze.add_argument("--source-conversation-id", help="override or supply the source conversation id")
    p_session_import_and_analyze.add_argument("--dry-run", action="store_true", help="plan the import without writing raw entries")

    p_refresh_import = sub.add_parser(
        "refresh-derived-for-import",
        help="recompute derived artifacts for a previously imported batch",
        description="Refresh conversation briefs, compressed memory, and open-loop analysis for entries already imported under one import_id.",
    )
    p_refresh_import.add_argument("--import-id", required=True, help="existing import batch id to refresh")
    p_refresh_import.add_argument("--dry-run", action="store_true", help="show what would refresh without writing new artifacts")
    p_refresh_import.add_argument("--no-force", action="store_true", help="do not force brief/memory regeneration when artifacts already exist")

    p_normalize_lifecycle = sub.add_parser(
        "normalize-derived-artifact-lifecycle",
        help="normalize lifecycle metadata for existing derived artifacts",
        description="Mark exactly one active artifact per logical scope and mark older siblings as superseded without deleting history.",
    )
    p_normalize_lifecycle.add_argument("--dry-run", action="store_true", help="report lifecycle fixes without writing artifact files")
    p_normalize_lifecycle.add_argument("--entry-id", help="optional entry id filter")
    p_normalize_lifecycle.add_argument("--artifact-type", help="optional artifact type filter")

    p_openclaw_import = sub.add_parser(
        "import-openclaw-session",
        help="one-step truthful import for OpenClaw session exports",
        description="Adapt an OpenClaw session export, build session-import JSONL, and import it through the truthful recurring-ingestion path.",
    )
    p_openclaw_import.add_argument("--input-path", required=True, help="raw OpenClaw session export path")
    p_openclaw_import.add_argument("--format", default="openclaw-session-jsonl", choices=SUPPORTED_ADAPTER_FORMATS, help="source export format to adapt")
    p_openclaw_import.add_argument("--source", default="openclaw-session-import", help="source label stored on imported raw entries")
    p_openclaw_import.add_argument("--source-session-id", help="override or supply the source session id")
    p_openclaw_import.add_argument("--source-conversation-id", help="override or supply the source conversation id")
    p_openclaw_import.add_argument("--import-id", help="override the generated import batch id")
    p_openclaw_import.add_argument("--dry-run", action="store_true", help="show what would import without writing raw entries")
    p_openclaw_import.add_argument("--gap-minutes", type=int, default=60, help="group transcript messages into a chunk when gaps exceed this many minutes")
    p_openclaw_import.add_argument("--max-chars", type=int, default=6000, help="split session chunks when rendered text exceeds this many characters")
    p_openclaw_import.add_argument("--max-messages", type=int, default=80, help="split session chunks when they exceed this many messages")
    p_openclaw_import.add_argument("--min-messages-before-gap-split", type=int, default=4, help="avoid splitting on a time gap when the current chunk is still smaller than this many messages")
    p_openclaw_import.add_argument("--min-chars-before-gap-split", type=int, default=400, help="avoid splitting on a time gap when the current chunk is still smaller than this many rendered characters")

    p_telegram_direct_import = sub.add_parser(
        "import-telegram-direct",
        help="one-step truthful import for Telegram direct chat transcripts",
        description="Build canonical Telegram-direct transcript from inbound Telegram logs plus OpenClaw session message.send events, then chunk and import through the truthful recurring-ingestion path.",
    )
    p_telegram_direct_import.add_argument("--inbound-path", required=True, help="path to Telegram inbound log JSONL or OpenClaw plugin-state SQLite")
    p_telegram_direct_import.add_argument("--sessions-root", required=True, help="directory containing OpenClaw session *.jsonl files")
    p_telegram_direct_import.add_argument("--chat-id", required=True, help="Telegram chat id to import")
    p_telegram_direct_import.add_argument("--source", default="telegram-direct-import", help="source label stored on imported raw entries")
    p_telegram_direct_import.add_argument("--source-session-id", help="override or supply the source session id")
    p_telegram_direct_import.add_argument("--source-conversation-id", help="override or supply the source conversation id")
    p_telegram_direct_import.add_argument("--import-id", help="override the generated import batch id")
    p_telegram_direct_import.add_argument("--dry-run", action="store_true", help="show what would import without writing raw entries")
    p_telegram_direct_import.add_argument("--gap-minutes", type=int, default=60, help="group transcript messages into a chunk when gaps exceed this many minutes")
    p_telegram_direct_import.add_argument("--max-chars", type=int, default=6000, help="split session chunks when rendered text exceeds this many characters")
    p_telegram_direct_import.add_argument("--max-messages", type=int, default=80, help="split session chunks when they exceed this many messages")
    p_telegram_direct_import.add_argument("--min-messages-before-gap-split", type=int, default=4, help="avoid splitting on a time gap when the current chunk is still smaller than this many messages")
    p_telegram_direct_import.add_argument("--min-chars-before-gap-split", type=int, default=400, help="avoid splitting on a time gap when the current chunk is still smaller than this many rendered characters")

    p_openclaw_work_trace = sub.add_parser(
        "import-openclaw-work-trace",
        help="import searchable work trace from one OpenClaw session file",
        description="Extract command/action/test evidence from an OpenClaw session JSONL file and store it as work-trace events.",
    )
    p_openclaw_work_trace.add_argument("--input-path", required=True, help="OpenClaw session JSONL file")
    p_openclaw_work_trace.add_argument("--session-key", help="optional session key to stamp onto imported work-trace events")
    p_openclaw_work_trace.add_argument("--dry-run", action="store_true", help="discover work-trace events without writing them")

    p_backfill = sub.add_parser(
        "backfill-openclaw-session-key",
        help="import many OpenClaw session files discovered from trajectory metadata for one session key",
        description="Find session files for a specific OpenClaw session key under the trajectory store, then import them through the truthful recurring-ingestion path as a controlled backfill.",
    )
    p_backfill.add_argument("--session-key", required=True, help="OpenClaw session key to backfill, for example agent:main:telegram:default:direct:713733361")
    p_backfill.add_argument("--trajectories-root", default="~/.openclaw/agents/main/sessions", help="directory containing *.trajectory.jsonl files")
    p_backfill.add_argument("--source", default="openclaw-session-backfill", help="source label stored on imported raw entries")
    p_backfill.add_argument("--since", help="inclusive lower bound for trajectory start time; accepts YYYY-MM-DD or ISO timestamp")
    p_backfill.add_argument("--until", help="exclusive upper bound for trajectory start time; accepts YYYY-MM-DD or ISO timestamp")
    p_backfill.add_argument("--days-back", type=int, help="convenience window counting backward from now when --since is omitted")
    p_backfill.add_argument("--dry-run", action="store_true", help="discover and plan the backfill without writing raw entries")
    p_backfill.add_argument("--gap-minutes", type=int, default=60, help="group transcript messages into a chunk when gaps exceed this many minutes")
    p_backfill.add_argument("--max-chars", type=int, default=6000, help="split session chunks when rendered text exceeds this many characters")
    p_backfill.add_argument("--max-messages", type=int, default=80, help="split session chunks when they exceed this many messages")
    p_backfill.add_argument("--min-messages-before-gap-split", type=int, default=4, help="avoid splitting on a time gap when the current chunk is still smaller than this many messages")
    p_backfill.add_argument("--min-chars-before-gap-split", type=int, default=400, help="avoid splitting on a time gap when the current chunk is still smaller than this many rendered characters")

    p_backfill_work_trace = sub.add_parser(
        "backfill-openclaw-work-trace-session-key",
        help="import work trace from many OpenClaw session files discovered from trajectory metadata",
        description="Find session files for a session key under the trajectory store and import command/action/test evidence into work trace.",
    )
    p_backfill_work_trace.add_argument("--session-key", required=True, help="OpenClaw session key to backfill")
    p_backfill_work_trace.add_argument("--trajectories-root", default="~/.openclaw/agents/main/sessions", help="directory containing *.trajectory.jsonl files")
    p_backfill_work_trace.add_argument("--since", help="inclusive lower bound for trajectory start time; accepts YYYY-MM-DD or ISO timestamp")
    p_backfill_work_trace.add_argument("--until", help="exclusive upper bound for trajectory start time; accepts YYYY-MM-DD or ISO timestamp")
    p_backfill_work_trace.add_argument("--days-back", type=int, help="convenience window counting backward from now when --since is omitted")
    p_backfill_work_trace.add_argument("--dry-run", action="store_true", help="discover and plan the backfill without writing work-trace events")

    p_backfill_telegram = sub.add_parser(
        "backfill-telegram-direct",
        help="truth-first backfill for one Telegram direct chat using trajectory-scoped session discovery",
        description="Discover OpenClaw session files from trajectory metadata for one session key and backfill that window through Telegram-direct reconstruction (inbound Telegram log + outbound message.send events).",
    )
    p_backfill_telegram.add_argument("--inbound-path", required=True, help="path to Telegram inbound log JSONL or OpenClaw plugin-state SQLite")
    p_backfill_telegram.add_argument("--sessions-root", required=True, help="directory containing OpenClaw session *.jsonl files")
    p_backfill_telegram.add_argument("--trajectories-root", default="~/.openclaw/agents/main/sessions", help="directory containing *.trajectory.jsonl files")
    p_backfill_telegram.add_argument("--session-key", required=True, help="OpenClaw session key used for trajectory-scoped discovery")
    p_backfill_telegram.add_argument("--chat-id", required=True, help="Telegram chat id to backfill")
    p_backfill_telegram.add_argument("--source", default="telegram-direct-backfill", help="source label stored on imported raw entries")
    p_backfill_telegram.add_argument("--since", help="inclusive lower bound for trajectory/message time; accepts YYYY-MM-DD or ISO timestamp")
    p_backfill_telegram.add_argument("--until", help="exclusive upper bound for trajectory/message time; accepts YYYY-MM-DD or ISO timestamp")
    p_backfill_telegram.add_argument("--days-back", type=int, help="convenience window counting backward from now when --since is omitted")
    p_backfill_telegram.add_argument("--dry-run", action="store_true", help="discover and plan backfill without writing raw entries")
    p_backfill_telegram.add_argument("--gap-minutes", type=int, default=60, help="group transcript messages into a chunk when gaps exceed this many minutes")
    p_backfill_telegram.add_argument("--max-chars", type=int, default=6000, help="split session chunks when rendered text exceeds this many characters")
    p_backfill_telegram.add_argument("--max-messages", type=int, default=80, help="split session chunks when they exceed this many messages")
    p_backfill_telegram.add_argument("--min-messages-before-gap-split", type=int, default=4, help="avoid splitting on a time gap when the current chunk is still smaller than this many messages")
    p_backfill_telegram.add_argument("--min-chars-before-gap-split", type=int, default=400, help="avoid splitting on a time gap when the current chunk is still smaller than this many rendered characters")

    p_list_imports = sub.add_parser(
        "list-imports",
        help="show recent truthful recurring-import batches",
        description="List recent import batch manifests in newest-first order so operators can review repeat imports and skipped duplicates.",
    )
    p_list_imports.add_argument("--limit", type=int, default=20, help="maximum number of import batches to show")

    p_build_session = sub.add_parser(
        "build-session-jsonl",
        help="convert canonical transcript messages into session-import JSONL",
        description="Chunk canonical transcript messages into session-import JSONL for truthful recurring ingestion.",
    )
    p_build_session.add_argument("--input-path", required=True, help="canonical transcript-message JSONL input")
    p_build_session.add_argument("--output-path", required=True, help="output path for session-import JSONL")
    p_build_session.add_argument("--source", required=True, help="source label stored on imported raw entries")
    p_build_session.add_argument("--gap-minutes", type=int, default=60, help="start a new chunk when message gaps exceed this many minutes")
    p_build_session.add_argument("--max-chars", type=int, default=6000, help="start a new chunk when rendered text exceeds this many characters")
    p_build_session.add_argument("--max-messages", type=int, default=80, help="start a new chunk when it exceeds this many messages")
    p_build_session.add_argument("--min-messages-before-gap-split", type=int, default=4, help="avoid splitting on a time gap when the current chunk is still smaller than this many messages")
    p_build_session.add_argument("--min-chars-before-gap-split", type=int, default=400, help="avoid splitting on a time gap when the current chunk is still smaller than this many rendered characters")

    p_build_transcript = sub.add_parser(
        "build-transcript-jsonl",
        help="adapt raw OpenClaw or generic exports into canonical transcript messages",
        description="Convert raw exports into canonical transcript-message JSONL for session building.",
    )
    p_build_transcript.add_argument("--input-path", required=True, help="raw export input path")
    p_build_transcript.add_argument("--output-path", required=True, help="output path for transcript JSONL")
    p_build_transcript.add_argument("--format", required=True, choices=SUPPORTED_ADAPTER_FORMATS, help="input format to adapt")
    p_build_transcript.add_argument("--source-session-id", help="override or supply the source session id")
    p_build_transcript.add_argument("--source-conversation-id", help="override or supply the source conversation id")

    p_build_telegram_direct = sub.add_parser(
        "build-telegram-direct-transcript",
        help="reconstruct a two-sided Telegram direct-chat transcript from Telegram-side logs and session-file sent messages",
        description="Build canonical transcript JSONL for one Telegram direct chat by combining inbound Telegram logs with assistant message sends recovered from OpenClaw session files.",
    )
    p_build_telegram_direct.add_argument("--inbound-path", required=True, help="path to sessions.json.telegram-messages.json or OpenClaw plugin-state SQLite")
    p_build_telegram_direct.add_argument("--sessions-root", required=True, help="directory containing OpenClaw session *.jsonl files")
    p_build_telegram_direct.add_argument("--chat-id", required=True, help="Telegram chat id to reconstruct")
    p_build_telegram_direct.add_argument("--output-path", required=True, help="output path for canonical transcript JSONL")
    p_build_telegram_direct.add_argument("--source-session-id", help="override the canonical source session id")
    p_build_telegram_direct.add_argument("--source-conversation-id", help="override the canonical source conversation id")

    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8041)

    # ── Knowledge Graph subcommands ─────────────────────────────────

    p_gfe = sub.add_parser("graph-find-entity")
    p_gfe.add_argument("--query", required=True)
    p_gfe.add_argument("--entity-type")

    p_gge = sub.add_parser("graph-get-entity")
    p_gge.add_argument("--entity-id", required=True)
    p_gge.add_argument("--include-history", action="store_true")

    p_gn = sub.add_parser("graph-neighbors")
    p_gn.add_argument("--entity-id", required=True)
    p_gn.add_argument("--depth", type=int, default=1)
    p_gn.add_argument("--states", default="current")

    p_gs = sub.add_parser("graph-search")
    p_gs.add_argument("--query", required=True)
    p_gs.add_argument("--states", default="current")

    p_gef = sub.add_parser("graph-explain-fact")
    p_gef.add_argument("--fact-id", required=True)

    p_ggs = sub.add_parser("graph-get-subgraph")
    p_ggs.add_argument("--entity-id", required=True)
    p_ggs.add_argument("--depth", type=int, default=1)
    p_ggs.add_argument("--max-nodes", type=int, default=50)
    p_ggs.add_argument("--states", default="current")

    p_gaf = sub.add_parser("graph-add-fact")
    p_gaf.add_argument("--subject-id", required=True)
    p_gaf.add_argument("--predicate", required=True)
    p_gaf.add_argument("--object-kind", required=True, choices=["entity", "value"])
    p_gaf.add_argument("--object-entity-id")
    p_gaf.add_argument("--object-value")
    p_gaf.add_argument("--object-value-type", default="text")
    p_gaf.add_argument("--source-kind", default="user_assertion")
    p_gaf.add_argument("--source-id", default="manual")
    p_gaf.add_argument("--author", default="user")
    p_gaf.add_argument("--reason")

    p_gcf = sub.add_parser("graph-correct-fact")
    p_gcf.add_argument("--fact-id", required=True)
    p_gcf.add_argument("--correction", required=True)
    p_gcf.add_argument("--reason", required=True)

    p_gma = sub.add_parser("graph-merge-alias")
    p_gma.add_argument("--entity-id", required=True)
    p_gma.add_argument("--alias", required=True)
    p_gma.add_argument("--source-kind", default="user_assertion")
    p_gma.add_argument("--source-id", default="manual")

    p_gse = sub.add_parser("graph-split-entity")
    p_gse.add_argument("--entity-id", required=True)
    p_gse.add_argument("--reason")

    p_gcj = sub.add_parser("graph-claim-jobs")
    p_gcj.add_argument("--limit", type=int, default=10)
    p_gcj.add_argument("--worker-id", default="hermes")
    p_gcj.add_argument("--lease-seconds", type=int, default=300)

    p_gfs = sub.add_parser("graph-fetch-source")
    p_gfs.add_argument("--job-id", required=True)

    p_gse2 = sub.add_parser("graph-submit-extraction")
    p_gse2.add_argument("--job-id", required=True)
    p_gse2.add_argument("--result-json", required=True)

    p_gfe2 = sub.add_parser("graph-fail-extraction")
    p_gfe2.add_argument("--job-id", required=True)
    p_gfe2.add_argument("--error", required=True)
    p_gfe2.add_argument("--no-retry", action="store_true")

    sub.add_parser("graph-queue-status")

    p_gb = sub.add_parser("graph-backfill")
    p_gb.add_argument("--batch-size", type=int, default=100)
    p_gb.add_argument("--dry-run", action="store_true")

    sub.add_parser("graph-backfill-status")

    p_ger = sub.add_parser("graph-enqueue-recent")
    p_ger.add_argument("--limit", type=int, default=50)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    def _merge_scope_filters(raw_filters: str, args_obj: argparse.Namespace) -> dict[str, Any]:
        merged = json.loads(raw_filters)
        if not isinstance(merged, dict):
            raise ValueError("--filters must decode to a JSON object")
        if getattr(args_obj, "source_conversation_id", None):
            merged["source_conversation_id"] = args_obj.source_conversation_id
        if getattr(args_obj, "source_session_id", None):
            merged["source_session_id"] = args_obj.source_session_id
        if getattr(args_obj, "import_id", None):
            merged["import_id"] = args_obj.import_id
        if bool(getattr(args_obj, "truthful_only", False)):
            merged["truthful_only"] = True
        return merged

    def _collect_str_values(obj: Any, keys: set[str]) -> list[str]:
        found: list[str] = []
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in keys and isinstance(value, str) and value.strip():
                    found.append(value.strip())
                else:
                    found.extend(_collect_str_values(value, keys))
        elif isinstance(obj, list):
            for item in obj:
                found.extend(_collect_str_values(item, keys))
        return found

    def _collect_list_values(obj: Any, keys: set[str]) -> list[str]:
        found: list[str] = []
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in keys and isinstance(value, list):
                    found.extend([str(item).strip() for item in value if str(item).strip()])
                else:
                    found.extend(_collect_list_values(value, keys))
        elif isinstance(obj, list):
            for item in obj:
                found.extend(_collect_list_values(item, keys))
        return found

    def _record_cli_command(command_name: str, result: dict[str, Any]) -> None:
        related_paths = sorted(
            {
                *[value for value in _collect_str_values(result, {"raw_file", "overlay_file", "artifact_file", "manifest_path", "ledger_path", "batch_manifest_path", "work_file", "work_file_path"}) if value],
            }
        )
        related_entry_ids = sorted(
            {
                *[value for value in _collect_str_values(result, {"entry_id"}) if value],
                *[value for value in _collect_list_values(result, {"imported_entry_ids", "source_entry_ids"}) if value],
            }
        )
        related_artifact_ids = sorted({*[
            value for value in _collect_str_values(result, {"artifact_id"}) if value
        ]})
        summary_map = {
            "append-entry": "Ran append-entry command.",
            "append-overlay": "Ran append-overlay command.",
            "attach-artifact": "Ran attach-artifact command.",
            "import-session-jsonl": "Ran import-session-jsonl command.",
            "import-session-and-analyze": "Ran import-session-and-analyze command.",
            "refresh-derived-for-import": "Ran refresh-derived-for-import command.",
            "produce-open-loops": "Ran produce-open-loops command.",
            "produce-conversation-briefs": "Ran produce-conversation-briefs command.",
            "produce-compressed-memory": "Ran produce-compressed-memory command.",
        }
        append_work_trace_event(
            paths,
            {
                "event_type": "command",
                "summary": summary_map.get(command_name, f"Ran {command_name} command."),
                "project": "agent-diary",
                "source_surface": "cli",
                "details": {"command": command_name},
                "related_entry_ids": related_entry_ids,
                "related_artifact_ids": related_artifact_ids,
                "related_paths": related_paths,
                "tags": ["auto", "command", command_name],
            },
        )

    paths = default_paths()
    ensure_data_dirs(paths)
    bootstrap_sqlite(paths.sqlite_path)

    if args.command == "append-entry":
        payload = {
            "entry_type": args.entry_type,
            "source": args.source,
            "author_role": args.author_role,
            "content": args.content,
            "created_at": args.created_at,
            "metadata": json.loads(args.metadata),
        }
        if args.title:
            payload["title"] = args.title
        out = append_entry(paths, payload)
        _record_cli_command(args.command, out)
        _print(out, args.json)
        return

    if args.command == "append-work-trace":
        payload = {
            "event_type": args.event_type,
            "summary": args.summary,
            "project": args.project,
            "source_surface": args.source_surface,
            "actor": args.actor,
            "session_key": args.session_key,
            "task_id": args.task_id,
            "details": json.loads(args.details),
            "related_entry_ids": json.loads(args.related_entry_ids),
            "related_artifact_ids": json.loads(args.related_artifact_ids),
            "related_paths": json.loads(args.related_paths),
            "tags": json.loads(args.tags),
        }
        if args.created_at:
            payload["created_at"] = args.created_at
        out = append_work_trace_event(paths, payload)
        _print(out, args.json)
        return

    if args.command == "attach-artifact":
        payload = {
            "entry_id": args.entry_id,
            "artifact_type": args.artifact_type,
            "producer": args.producer,
            "content": args.content,
            "metadata": json.loads(args.metadata),
        }
        if args.created_at:
            payload["created_at"] = args.created_at
        out = attach_artifact(paths, payload)
        _record_cli_command(args.command, out)
        _print(out, args.json)
        return

    if args.command == "append-overlay":
        payload = {
            "entry_id": args.entry_id,
            "overlay_type": args.overlay_type,
            "author": args.author,
            "content": args.content,
            "metadata": json.loads(args.metadata),
        }
        if args.created_at:
            payload["created_at"] = args.created_at
        out = append_overlay(paths, payload)
        _record_cli_command(args.command, out)
        _print(out, args.json)
        return

    if args.command == "search-memory":
        payload = {
            "query": args.query,
            "limit": args.limit,
            "filters": _merge_scope_filters(args.filters, args),
        }
        out = search_memory(paths, payload)
        _print(out, args.json)
        return

    if args.command == "search-work-trace":
        out = search_work_trace(
            paths,
            {
                "query": args.query,
                "limit": args.limit,
                "event_type": args.event_type,
                "project": args.project,
            },
        )
        _print(out, args.json)
        return

    if args.command == "search-all":
        out = search_all(
            paths,
            {
                "query": args.query,
                "limit": args.limit,
                "filters": _merge_scope_filters(args.filters, args),
                "event_type": args.event_type,
                "project": args.project,
            },
        )
        _print(out, args.json)
        return

    if args.command == "fetch-raw-entry":
        payload = {
            "entry_id": args.entry_id,
            "include_overlays": args.include_overlays,
            "include_artifacts": args.include_artifacts,
        }
        out = fetch_raw_entry(paths, payload)
        _print(out, args.json)
        return

    if args.command == "fetch-work-trace":
        out = fetch_work_trace_event(paths, {"event_id": args.event_id})
        _print(out, args.json)
        return

    if args.command == "list-entries":
        merged_filters = _merge_scope_filters(args.filters, args)
        out = list_entries(paths, {"limit": args.limit, "offset": args.offset, "filters": merged_filters})
        _print(out, args.json)
        return

    if args.command == "list-work-trace":
        out = list_work_trace(
            paths,
            {
                "limit": args.limit,
                "offset": args.offset,
                "event_type": args.event_type,
                "project": args.project,
            },
        )
        _print(out, args.json)
        return

    if args.command == "fetch-entry-detail":
        out = fetch_entry_detail(paths, {"entry_id": args.entry_id})
        _print(out, args.json)
        return

    if args.command == "produce-open-loops":
        out = produce_open_loops(
            paths,
            {
                "limit": args.limit,
                "entry_ids": args.entry_ids,
                "filters": _merge_scope_filters(args.filters, args),
            },
        )
        _record_cli_command(args.command, out)
        _print(out, args.json)
        return

    if args.command == "produce-conversation-briefs":
        merged_filters = _merge_scope_filters(args.filters, args)
        out = produce_conversation_briefs(
            paths,
            {"limit": args.limit, "entry_ids": args.entry_ids, "force": args.force, "filters": merged_filters},
        )
        _record_cli_command(args.command, out)
        _print(out, args.json)
        return

    if args.command == "produce-compressed-memory":
        merged_filters = _merge_scope_filters(args.filters, args)
        out = produce_compressed_memory(
            paths,
            {"limit": args.limit, "entry_ids": args.entry_ids, "force": args.force, "filters": merged_filters},
        )
        _record_cli_command(args.command, out)
        _print(out, args.json)
        return

    if args.command == "import-entries-jsonl":
        imported: list[dict[str, str]] = []
        with open(args.path, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f, start=1):
                raw = line.strip()
                if not raw:
                    continue
                obj = json.loads(raw)
                payload = {
                    "entry_type": obj["entry_type"],
                    "source": obj["source"],
                    "author_role": obj["author_role"],
                    "content": obj["content"],
                    "created_at": obj["created_at"],
                    "metadata": obj.get("metadata", {}),
                }
                if "title" in obj:
                    payload["title"] = obj["title"]
                result = append_entry(paths, payload)
                imported.append({"line": str(idx), "entry_id": result["entry_id"]})
        _print({"imported_count": len(imported), "entries": imported}, args.json)
        return

    if args.command == "import-session-jsonl":
        out = import_session_jsonl(
            paths,
            {
                "path": args.path,
                "import_id": args.import_id,
                "source_session_id": args.source_session_id,
                "source_conversation_id": args.source_conversation_id,
                "dry_run": args.dry_run,
            },
        )
        _record_cli_command(args.command, out)
        _print(out, args.json)
        return

    if args.command == "import-session-and-analyze":
        out = import_session_and_refresh_derived(
            paths,
            {
                "path": args.path,
                "import_id": args.import_id,
                "source_session_id": args.source_session_id,
                "source_conversation_id": args.source_conversation_id,
                "dry_run": args.dry_run,
            },
        )
        _record_cli_command(args.command, out)
        _print(out, args.json)
        return

    if args.command == "refresh-derived-for-import":
        out = refresh_derived_for_import(
            paths,
            {
                "import_id": args.import_id,
                "dry_run": args.dry_run,
                "force": not args.no_force,
            },
        )
        _record_cli_command(args.command, out)
        _print(out, args.json)
        return

    if args.command == "normalize-derived-artifact-lifecycle":
        out = normalize_derived_artifact_lifecycle(
            paths,
            {
                "dry_run": args.dry_run,
                "entry_id": args.entry_id,
                "artifact_type": args.artifact_type,
            },
        )
        _print(out, args.json)
        return

    if args.command == "build-session-jsonl":
        out = build_session_jsonl(
            input_path=Path(args.input_path).expanduser().resolve(),
            output_path=Path(args.output_path).expanduser().resolve(),
            source=args.source,
            gap_minutes=args.gap_minutes,
            max_chars=args.max_chars,
            max_messages=args.max_messages,
            min_messages_before_gap_split=args.min_messages_before_gap_split,
            min_chars_before_gap_split=args.min_chars_before_gap_split,
        )
        _print(out, args.json)
        return

    if args.command == "build-transcript-jsonl":
        out = adapt_session_export(
            input_path=Path(args.input_path).expanduser().resolve(),
            output_path=Path(args.output_path).expanduser().resolve(),
            format_name=args.format,
            source_session_id=args.source_session_id,
            source_conversation_id=args.source_conversation_id,
        )
        _print(out, args.json)
        return

    if args.command == "build-telegram-direct-transcript":
        out = build_openclaw_telegram_direct_transcript(
            inbound_path=Path(args.inbound_path).expanduser().resolve(),
            sessions_root=Path(args.sessions_root).expanduser().resolve(),
            output_path=Path(args.output_path).expanduser().resolve(),
            chat_id=str(args.chat_id),
            source_session_id=args.source_session_id,
            source_conversation_id=args.source_conversation_id,
        )
        _print(out, args.json)
        return

    if args.command == "import-openclaw-session":
        out = import_openclaw_session(
            paths,
            input_path=Path(args.input_path).expanduser().resolve(),
            format_name=args.format,
            source=args.source,
            import_id=args.import_id,
            source_session_id=args.source_session_id,
            source_conversation_id=args.source_conversation_id,
            dry_run=args.dry_run,
            gap_minutes=args.gap_minutes,
            max_chars=args.max_chars,
            max_messages=args.max_messages,
            min_messages_before_gap_split=args.min_messages_before_gap_split,
            min_chars_before_gap_split=args.min_chars_before_gap_split,
        )
        _print(out, args.json)
        return

    if args.command == "import-openclaw-work-trace":
        out = import_openclaw_work_trace(
            paths,
            input_path=Path(args.input_path).expanduser().resolve(),
            session_key=args.session_key,
            dry_run=args.dry_run,
        )
        _print(out, args.json)
        return

    if args.command == "import-telegram-direct":
        out = import_telegram_direct(
            paths,
            inbound_path=Path(args.inbound_path).expanduser().resolve(),
            sessions_root=Path(args.sessions_root).expanduser().resolve(),
            chat_id=str(args.chat_id),
            source=args.source,
            import_id=args.import_id,
            source_session_id=args.source_session_id,
            source_conversation_id=args.source_conversation_id,
            dry_run=args.dry_run,
            gap_minutes=args.gap_minutes,
            max_chars=args.max_chars,
            max_messages=args.max_messages,
            min_messages_before_gap_split=args.min_messages_before_gap_split,
            min_chars_before_gap_split=args.min_chars_before_gap_split,
        )
        _print(out, args.json)
        return

    if args.command == "backfill-openclaw-session-key":
        out = backfill_openclaw_session_key(
            paths,
            trajectories_root=Path(args.trajectories_root).expanduser().resolve(),
            session_key=args.session_key,
            source=args.source,
            since=args.since,
            until=args.until,
            days_back=args.days_back,
            dry_run=args.dry_run,
            gap_minutes=args.gap_minutes,
            max_chars=args.max_chars,
            max_messages=args.max_messages,
            min_messages_before_gap_split=args.min_messages_before_gap_split,
            min_chars_before_gap_split=args.min_chars_before_gap_split,
        )
        _print(out, args.json)
        return

    if args.command == "backfill-openclaw-work-trace-session-key":
        out = backfill_openclaw_work_trace_session_key(
            paths,
            trajectories_root=Path(args.trajectories_root).expanduser().resolve(),
            session_key=args.session_key,
            since=args.since,
            until=args.until,
            days_back=args.days_back,
            dry_run=args.dry_run,
        )
        _print(out, args.json)
        return

    if args.command == "backfill-telegram-direct":
        out = backfill_telegram_direct(
            paths,
            inbound_path=Path(args.inbound_path).expanduser().resolve(),
            sessions_root=Path(args.sessions_root).expanduser().resolve(),
            trajectories_root=Path(args.trajectories_root).expanduser().resolve(),
            session_key=str(args.session_key),
            chat_id=str(args.chat_id),
            source=args.source,
            since=args.since,
            until=args.until,
            days_back=args.days_back,
            dry_run=args.dry_run,
            gap_minutes=args.gap_minutes,
            max_chars=args.max_chars,
            max_messages=args.max_messages,
            min_messages_before_gap_split=args.min_messages_before_gap_split,
            min_chars_before_gap_split=args.min_chars_before_gap_split,
        )
        _print(out, args.json)
        return

    if args.command == "list-imports":
        out = list_imports(paths, {"limit": args.limit})
        _print(out, args.json)
        return

    if args.command == "graph-find-entity":
        payload = {"query": args.query}
        if getattr(args, "entity_type", None):
            payload["entity_type"] = args.entity_type
        _print(graph_find_entity(paths, payload), args.json)
        return

    if args.command == "graph-get-entity":
        payload = {"entity_id": args.entity_id, "include_history": args.include_history}
        _print(graph_get_entity(paths, payload), args.json)
        return

    if args.command == "graph-neighbors":
        payload = {
            "entity_id": args.entity_id,
            "depth": args.depth,
            "states": [s.strip() for s in args.states.split(",")],
        }
        _print(graph_neighbors(paths, payload), args.json)
        return

    if args.command == "graph-search":
        payload = {"query": args.query, "states": [s.strip() for s in args.states.split(",")]}
        _print(graph_search(paths, payload), args.json)
        return

    if args.command == "graph-explain-fact":
        _print(graph_explain_fact(paths, {"fact_id": args.fact_id}), args.json)
        return

    if args.command == "graph-get-subgraph":
        payload = {
            "entity_id": args.entity_id,
            "depth": args.depth,
            "max_nodes": args.max_nodes,
            "states": [s.strip() for s in args.states.split(",")],
        }
        _print(graph_get_subgraph(paths, payload), args.json)
        return

    if args.command == "graph-add-fact":
        payload = {
            "subject_id": args.subject_id,
            "predicate": args.predicate,
            "object_kind": args.object_kind,
            "source_kind": args.source_kind,
            "source_id": args.source_id,
            "author": args.author,
        }
        if args.object_kind == "entity":
            payload["object_entity_id"] = args.object_entity_id
        else:
            payload["object_value"] = args.object_value
            payload["object_value_type"] = args.object_value_type
        if args.reason:
            payload["reason"] = args.reason
        _print(graph_add_fact(paths, payload), args.json)
        return

    if args.command == "graph-correct-fact":
        _print(graph_correct_fact(paths, {
            "fact_id": args.fact_id,
            "correction": args.correction,
            "reason": args.reason,
        }), args.json)
        return

    if args.command == "graph-merge-alias":
        _print(graph_merge_alias(paths, {
            "entity_id": args.entity_id,
            "alias": args.alias,
            "source_kind": args.source_kind,
            "source_id": args.source_id,
        }), args.json)
        return

    if args.command == "graph-split-entity":
        payload = {"entity_id": args.entity_id}
        if args.reason:
            payload["reason"] = args.reason
        _print(graph_split_entity(paths, payload), args.json)
        return

    if args.command == "graph-claim-jobs":
        _print(graph_claim_jobs(paths, {
            "limit": args.limit,
            "worker_id": args.worker_id,
            "lease_seconds": args.lease_seconds,
        }), args.json)
        return

    if args.command == "graph-fetch-source":
        _print(graph_fetch_source(paths, {"job_id": args.job_id}), args.json)
        return

    if args.command == "graph-submit-extraction":
        import json as _json
        result = _json.loads(args.result_json)
        _print(graph_submit_extraction(paths, {"job_id": args.job_id, "result": result}), args.json)
        return

    if args.command == "graph-fail-extraction":
        _print(graph_fail_extraction(paths, {
            "job_id": args.job_id,
            "error": args.error,
            "retryable": not args.no_retry,
        }), args.json)
        return

    if args.command == "graph-queue-status":
        _print(graph_queue_status(paths, {}), args.json)
        return

    if args.command == "graph-backfill":
        _print(graph_backfill(paths, {
            "batch_size": args.batch_size,
            "dry_run": args.dry_run,
        }), args.json)
        return

    if args.command == "graph-backfill-status":
        _print(graph_backfill_status(paths, {}), args.json)
        return

    if args.command == "graph-enqueue-recent":
        _print(graph_enqueue_recent(paths, {"limit": args.limit}), args.json)
        return

    if args.command == "serve":
        run_server(paths, host=args.host, port=args.port)
        return


if __name__ == "__main__":
    main()
