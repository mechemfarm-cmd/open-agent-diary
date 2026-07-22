from __future__ import annotations

import http.client
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from agent_diary.cli.openclaw_session_import import backfill_openclaw_session_key, discover_openclaw_session_files
from agent_diary.cli.openclaw_work_trace_import import (
    backfill_openclaw_work_trace_session_key,
    extract_openclaw_work_trace_events,
    import_openclaw_work_trace,
)
from agent_diary.analytics.conversation_briefs import build_conversation_brief_text
from agent_diary.analytics.compressed_memory import build_compressed_memory_text
from agent_diary.cli.session_builder import TranscriptMessage, build_session_entries, build_session_jsonl
from agent_diary.cli.transcript_adapter import adapt_session_export, build_openclaw_telegram_direct_transcript
from agent_diary.config import default_paths
from agent_diary.index.sqlite_index import bootstrap_sqlite
from agent_diary.service.doctor import run_doctor
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
    normalize_derived_artifact_lifecycle,
    produce_conversation_briefs,
    produce_compressed_memory,
    produce_open_loops,
    refresh_derived_for_import,
    search_all,
    search_memory,
    search_work_trace,
    status,
)
from agent_diary.service.http_server import AgentDiaryHandler, AgentDiaryHTTPServer
from agent_diary.storage.archiver import ArchiveConfig, archive_month, archive_report, archive_stale_entries
from agent_diary.storage.files import ensure_data_dirs


class AppendEntrySliceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = default_paths(self.root)
        ensure_data_dirs(self.paths)
        bootstrap_sqlite(self.paths.sqlite_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_append_entry_writes_raw_file_at_expected_path(self) -> None:
        created_at = "2026-05-20T10:00:00+00:00"
        result = append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "hello diary",
                "created_at": created_at,
                "metadata": {"mood": "focused"},
            },
        )

        raw_file = Path(result["raw_file"])
        self.assertTrue(raw_file.exists())
        self.assertEqual(raw_file.parent, self.paths.entries_dir / "2026" / "05" / "20")

        body = json.loads(raw_file.read_text(encoding="utf-8"))
        self.assertEqual(body["entry_id"], result["entry_id"])
        self.assertEqual(body["content"], "hello diary")


    def test_archive_month_keeps_raw_entry_fetchable_after_file_removed(self) -> None:
        created_at = "2026-01-15T12:00:00+00:00"
        result = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "test",
                "author_role": "mixed",
                "content": "archive me without losing a byte",
                "created_at": created_at,
            },
        )
        raw_file = Path(result["raw_file"])
        self.assertTrue(raw_file.exists())

        archive_result = archive_month(self.paths, 2026, 1)

        self.assertTrue(archive_result["ok"])
        self.assertFalse(raw_file.exists())
        fetched = fetch_raw_entry(self.paths, {"entry_id": result["entry_id"]})
        self.assertEqual(fetched["entry"]["content"], "archive me without losing a byte")
        self.assertEqual(fetched["entry"]["created_at"], created_at)
        self.assertTrue(fetched["entry_file"].startswith("archive:"))

        with sqlite3.connect(self.paths.sqlite_path) as conn:
            raw_file_path = conn.execute(
                "SELECT raw_file_path FROM entries WHERE entry_id = ?",
                (result["entry_id"],),
            ).fetchone()[0]
        self.assertTrue(raw_file_path.startswith("archive://"))
        self.assertIn("#entries/2026/01/15/", raw_file_path)

    def test_archive_stale_entries_respects_dry_run(self) -> None:
        result = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "test",
                "author_role": "mixed",
                "content": "dry run entry",
                "created_at": "2026-02-01T00:00:00+00:00",
            },
        )
        raw_file = Path(result["raw_file"])
        old = datetime(2025, 1, 1).timestamp()
        os.utime(raw_file, (old, old))

        results = archive_stale_entries(
            self.paths,
            ArchiveConfig(after_days=1, max_bytes=1, max_files=9999, dry_run=True),
        )

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["dry_run"])
        self.assertTrue(raw_file.exists())

    def test_archive_report_lists_archives_and_candidates(self) -> None:
        append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "test",
                "author_role": "mixed",
                "content": "report entry",
                "created_at": "2026-03-01T00:00:00+00:00",
            },
        )
        report_before = archive_report(self.paths)
        self.assertTrue(report_before["ok"])
        self.assertTrue(any(m["year"] == 2026 and m["month"] == 3 for m in report_before["candidate_months"]))

        archive_month(self.paths, 2026, 3)
        report_after = archive_report(self.paths)
        self.assertTrue(report_after["existing_archives"])

    def test_append_entry_registers_index_row(self) -> None:
        created_at = "2026-05-20T11:00:00+00:00"
        result = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "indexed text",
                "created_at": created_at,
            },
        )

        with sqlite3.connect(self.paths.sqlite_path) as conn:
            row = conn.execute(
                "SELECT entry_id, created_at, source, author_role, raw_file_path FROM entries WHERE entry_id = ?",
                (result["entry_id"],),
            ).fetchone()

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row[0], result["entry_id"])
        self.assertEqual(row[1], created_at)
        self.assertEqual(row[2], "openclaw")
        self.assertEqual(row[3], "agent")
        self.assertEqual(row[4], result["raw_file"])

    def test_append_work_trace_writes_file_and_index_row(self) -> None:
        result = append_work_trace_event(
            self.paths,
            {
                "event_type": "command",
                "summary": "Ran append-entry slice tests.",
                "created_at": "2026-05-29T08:00:00+00:00",
                "project": "agent-diary",
                "source_surface": "telegram-direct",
                "actor": "assistant",
                "details": {"command": "python3 -m unittest tests.test_append_entry_slice -v", "result": "pass"},
                "related_paths": ["tests/test_append_entry_slice.py"],
                "tags": ["verification"],
            },
        )

        work_file = Path(result["work_file"])
        self.assertTrue(work_file.exists())
        body = json.loads(work_file.read_text(encoding="utf-8"))
        self.assertEqual(body["event_id"], result["event_id"])
        self.assertEqual(body["event_type"], "command")
        self.assertEqual(body["details"]["result"], "pass")

        with sqlite3.connect(self.paths.sqlite_path) as conn:
            row = conn.execute(
                "SELECT event_id, created_at, event_type, summary, project, work_file_path FROM work_trace_events WHERE event_id = ?",
                (result["event_id"],),
            ).fetchone()

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row[0], result["event_id"])
        self.assertEqual(row[1], "2026-05-29T08:00:00+00:00")
        self.assertEqual(row[2], "command")
        self.assertEqual(row[3], "Ran append-entry slice tests.")
        self.assertEqual(row[4], "agent-diary")
        self.assertEqual(row[5], result["work_file"])

    def test_append_entry_auto_records_file_change_work_trace(self) -> None:
        result = append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "auto trace me",
                "created_at": "2026-05-29T10:00:00+00:00",
            },
        )

        listed = list_work_trace(self.paths, {"limit": 10, "offset": 0})
        self.assertGreaterEqual(len(listed["items"]), 1)
        first = listed["items"][0]
        self.assertEqual(first["event_type"], "file_change")
        fetched = fetch_work_trace_event(self.paths, {"event_id": first["event_id"]})
        self.assertEqual(fetched["event"]["related_entry_ids"], [result["entry_id"]])
        self.assertEqual(fetched["event"]["details"]["change_kind"], "create")

    def test_fetch_work_trace_returns_authoritative_record(self) -> None:
        created = append_work_trace_event(
            self.paths,
            {
                "event_type": "decision",
                "summary": "Deferred UI work-trace integration.",
                "details": {"reason": "backend first"},
            },
        )

        fetched = fetch_work_trace_event(self.paths, {"event_id": created["event_id"]})
        self.assertEqual(fetched["event"]["event_id"], created["event_id"])
        self.assertEqual(fetched["event"]["event_type"], "decision")
        self.assertEqual(fetched["event"]["details"]["reason"], "backend first")

    def test_list_work_trace_orders_newest_first(self) -> None:
        older = append_work_trace_event(
            self.paths,
            {
                "event_type": "decision",
                "summary": "Older decision",
                "created_at": "2026-05-29T07:00:00+00:00",
                "project": "agent-diary",
            },
        )
        newer = append_work_trace_event(
            self.paths,
            {
                "event_type": "command",
                "summary": "Newer command",
                "created_at": "2026-05-29T09:00:00+00:00",
                "project": "agent-diary",
            },
        )

        listed = list_work_trace(self.paths, {"limit": 10, "offset": 0, "project": "agent-diary"})
        self.assertEqual([item["event_id"] for item in listed["items"][:2]], [newer["event_id"], older["event_id"]])

    def test_search_work_trace_finds_summary_and_path(self) -> None:
        append_work_trace_event(
            self.paths,
            {
                "event_type": "file_change",
                "summary": "Adjusted timeline layout sizing.",
                "project": "agent-diary",
                "related_paths": ["ui/styles.css"],
                "details": {"change": "rebalanced nav column rows"},
            },
        )
        append_work_trace_event(
            self.paths,
            {
                "event_type": "command",
                "summary": "Ran unrelated command",
                "project": "other-project",
            },
        )

        with sqlite3.connect(self.paths.sqlite_path) as conn:
            fts_count = conn.execute("SELECT COUNT(*) FROM work_trace_fts").fetchone()[0]
        self.assertEqual(fts_count, 2)

        by_summary = search_work_trace(self.paths, {"query": "timeline layout", "limit": 5})
        self.assertEqual(by_summary["matches"][0]["event_type"], "file_change")

        by_project = search_work_trace(self.paths, {"query": "styles", "limit": 5, "project": "agent-diary"})
        self.assertEqual(len(by_project["matches"]), 1)
        self.assertEqual(by_project["matches"][0]["project"], "agent-diary")

    def test_search_all_combines_conversation_and_work_trace_results(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_message",
                "source": "telegram",
                "author_role": "human",
                "content": "We should verify the friday deploy before release.",
                "created_at": "2026-05-29T10:00:00+00:00",
            },
        )
        append_work_trace_event(
            self.paths,
            {
                "event_type": "test_run",
                "summary": "Ran friday deploy verification suite.",
                "project": "agent-diary",
                "source_surface": "openclaw-session",
                "created_at": "2026-05-29T10:05:00+00:00",
            },
        )

        results = search_all(self.paths, {"query": "friday deploy", "limit": 10})

        self.assertEqual(results["counts"]["conversation_matches"], 1)
        self.assertEqual(results["counts"]["work_trace_matches"], 1)
        self.assertEqual(results["counts"]["combined_matches"], 2)
        domains = [item["search_domain"] for item in results["matches"]]
        self.assertIn("conversation", domains)
        self.assertIn("work_trace", domains)
        conversation = next(item for item in results["matches"] if item["search_domain"] == "conversation")
        self.assertEqual(conversation["entry_id"], entry["entry_id"])
        self.assertIn("fetch_raw_entry", conversation)
        work_trace = next(item for item in results["matches"] if item["search_domain"] == "work_trace")
        self.assertEqual(work_trace["event_type"], "test_run")
        self.assertIn("fetch_work_trace", work_trace)

    def test_end_to_end_truth_derived_work_trace_and_unified_search_flow(self) -> None:
        source_file = self.root / "e2e-session.jsonl"
        source_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "entry_type": "chat_log",
                            "source": "telegram-direct-import",
                            "author_role": "human",
                            "created_at": "2026-05-29T09:00:00+00:00",
                            "content": "Please run the unittest before the Friday deploy.",
                            "metadata": {"source_message_id": "m1"},
                        }
                    ),
                    json.dumps(
                        {
                            "entry_type": "chat_log",
                            "source": "telegram-direct-import",
                            "author_role": "agent",
                            "created_at": "2026-05-29T09:01:00+00:00",
                            "content": "I will run the unittest and keep the raw record preserved.",
                            "metadata": {"source_message_id": "m2"},
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        imported = import_session_jsonl(
            self.paths,
            {
                "path": str(source_file),
                "import_id": "import-e2e",
                "source_session_id": "session-e2e",
                "source_conversation_id": "telegram:123456789",
            },
        )
        entry_ids = [item["entry_id"] for item in imported["imported"]]

        brief_out = produce_conversation_briefs(
            self.paths,
            {"entry_ids": entry_ids, "limit": 10, "force": True},
        )
        memory_out = produce_compressed_memory(
            self.paths,
            {"entry_ids": entry_ids, "limit": 10, "force": True},
        )

        session_path = self.root / "e2e-openclaw-work-trace.jsonl"
        self._write_openclaw_work_trace_fixture(session_path)
        trace_out = import_openclaw_work_trace(
            self.paths,
            input_path=session_path,
            session_key="agent:main:telegram:default:direct:123456789",
        )

        detail = fetch_entry_detail(self.paths, {"entry_id": entry_ids[0]})
        combined = search_all(self.paths, {"query": "unittest", "limit": 10})
        brief_artifacts = [artifact for artifact in detail["artifacts"] if artifact["artifact_type"] == "conversation-brief"]
        memory_artifacts = [artifact for artifact in detail["artifacts"] if artifact["artifact_type"] in {"memory", "compressed-memory"}]

        self.assertGreaterEqual(brief_out["produced_count"], 1)
        self.assertGreaterEqual(memory_out["produced_count"], 1)
        self.assertEqual(trace_out["imported_count"], 3)
        self.assertGreaterEqual(len(brief_artifacts), 1)
        self.assertGreaterEqual(len(memory_artifacts), 1)
        domains = {item["search_domain"] for item in combined["matches"]}
        self.assertIn("conversation", domains)
        self.assertIn("work_trace", domains)
        self.assertGreaterEqual(combined["counts"]["compressed_memory_hits"], 1)

    def test_cli_search_all_returns_combined_domains(self) -> None:
        append_entry(
            self.paths,
            {
                "entry_type": "chat_message",
                "source": "telegram",
                "author_role": "human",
                "content": "Scoped needle token for import search.",
                "created_at": "2026-05-29T10:00:00+00:00",
            },
        )
        append_work_trace_event(
            self.paths,
            {
                "event_type": "command",
                "summary": "Scoped needle command run.",
                "project": "agent-diary",
                "created_at": "2026-05-29T10:05:00+00:00",
            },
        )
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_diary.cli.main",
                "--json",
                "search-all",
                "--query",
                "scoped needle",
                "--project",
                "agent-diary",
            ],
            cwd=self.root,
            env={**os.environ, "PYTHONPATH": str(Path.cwd() / "src")},
            capture_output=True,
            text=True,
            check=True,
        )
        body = json.loads(proc.stdout)
        self.assertEqual(body["filters"]["project"], "agent-diary")
        domains = {item["search_domain"] for item in body["matches"]}
        self.assertIn("conversation", domains)
        self.assertIn("work_trace", domains)

    def test_fetch_raw_entry_returns_authoritative_record(self) -> None:
        result = append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "authoritative content",
                "created_at": "2026-05-20T12:00:00+00:00",
                "metadata": {"tag": "important"},
            },
        )

        fetched = fetch_raw_entry(self.paths, {"entry_id": result["entry_id"]})
        self.assertEqual(fetched["entry"]["entry_id"], result["entry_id"])
        self.assertEqual(fetched["entry"]["content"], "authoritative content")
        self.assertEqual(fetched["entry"]["metadata"], {"tag": "important"})

    def test_append_overlay_writes_file_and_keeps_raw_entry_unchanged(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "source truth content",
                "created_at": "2026-05-20T12:30:00+00:00",
            },
        )
        before = fetch_raw_entry(self.paths, {"entry_id": entry["entry_id"]})["entry"]
        out = append_overlay(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "overlay_type": "correction",
                "author": "operator",
                "content": "Correction note without rewriting source truth.",
            },
        )
        self.assertTrue(Path(out["overlay_file"]).exists())
        fetched = fetch_raw_entry(self.paths, {"entry_id": entry["entry_id"], "include_overlays": True})
        self.assertEqual(len(fetched["overlays"]), 1)
        self.assertEqual(fetched["overlays"][0]["overlay_type"], "correction")
        self.assertEqual(fetched["entry"], before)

    def test_fetch_entry_detail_includes_overlays_as_secondary_layer(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "cli",
                "author_role": "human",
                "content": "raw truth stays as-is",
                "created_at": "2026-05-20T12:40:00+00:00",
            },
        )
        append_overlay(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "overlay_type": "annotation",
                "author": "reviewer",
                "content": "Annotation about context.",
            },
        )
        detail = fetch_entry_detail(self.paths, {"entry_id": entry["entry_id"]})
        self.assertEqual(len(detail["overlays"]), 1)
        self.assertEqual(detail["overlays"][0]["overlay_type"], "annotation")
        self.assertEqual(detail["truth_model"]["primary"], "raw_entry")
        self.assertEqual(detail["truth_model"]["overlay_layer"], "overlays")

    def test_cli_append_entry_command_works(self) -> None:
        cmd = [
            sys.executable,
            "-m",
            "agent_diary.cli.main",
            "--json",
            "append-entry",
            "--entry-type",
            "manual_note",
            "--source",
            "cli",
            "--author-role",
            "human",
            "--content",
            "cli flow",
            "--created-at",
            "2026-05-20T13:00:00+00:00",
        ]

        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        completed = subprocess.run(
            cmd,
            cwd=self.root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        out = json.loads(completed.stdout)
        self.assertIn("entry_id", out)
        self.assertIn("raw_file", out)
        self.assertTrue(Path(out["raw_file"]).exists())

    def test_cli_append_overlay_command_works(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "base truth",
                "created_at": "2026-05-20T13:10:00+00:00",
            },
        )
        cmd = [
            sys.executable,
            "-m",
            "agent_diary.cli.main",
            "--json",
            "append-overlay",
            "--entry-id",
            entry["entry_id"],
            "--overlay-type",
            "annotation",
            "--author",
            "operator",
            "--content",
            "Overlay through CLI.",
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        completed = subprocess.run(
            cmd,
            cwd=self.root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        out = json.loads(completed.stdout)
        self.assertEqual(out["entry_id"], entry["entry_id"])
        self.assertEqual(out["overlay_type"], "annotation")
        self.assertTrue(Path(out["overlay_file"]).exists())

    def test_cli_append_work_trace_command_works(self) -> None:
        cmd = [
            sys.executable,
            "-m",
            "agent_diary.cli.main",
            "--json",
            "append-work-trace",
            "--event-type",
            "test_run",
            "--summary",
            "Ran focused work trace tests",
            "--project",
            "agent-diary",
            "--details",
            '{"command":"python3 -m unittest","result":"pass"}',
            "--related-paths",
            '["tests/test_append_entry_slice.py"]',
            "--tags",
            '["verification"]',
        ]

        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        completed = subprocess.run(
            cmd,
            cwd=self.root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        out = json.loads(completed.stdout)
        self.assertIn("event_id", out)
        self.assertIn("work_file", out)
        self.assertTrue(Path(out["work_file"]).exists())

    def test_cli_import_openclaw_work_trace_command_works(self) -> None:
        session_path = self.root / "work-trace-cli.jsonl"
        self._write_openclaw_work_trace_fixture(session_path)
        cmd = [
            sys.executable,
            "-m",
            "agent_diary.cli.main",
            "--json",
            "import-openclaw-work-trace",
            "--input-path",
            str(session_path),
            "--session-key",
            "agent:main:telegram:default:direct:123456789",
        ]

        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        completed = subprocess.run(
            cmd,
            cwd=self.root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        out = json.loads(completed.stdout)
        self.assertEqual(out["discovered_event_count"], 3)
        self.assertEqual(out["imported_count"], 3)

    def test_cli_append_entry_auto_records_command_work_trace(self) -> None:
        cmd = [
            sys.executable,
            "-m",
            "agent_diary.cli.main",
            "--json",
            "append-entry",
            "--entry-type",
            "manual_note",
            "--source",
            "cli",
            "--author-role",
            "human",
            "--content",
            "cli command auto trace",
            "--created-at",
            "2026-05-29T11:00:00+00:00",
        ]

        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        subprocess.run(
            cmd,
            cwd=self.root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        listed = list_work_trace(self.paths, {"limit": 5, "offset": 0})
        event_types = [item["event_type"] for item in listed["items"]]
        self.assertIn("command", event_types)
        self.assertIn("file_change", event_types)
        command_event_id = next(item["event_id"] for item in listed["items"] if item["event_type"] == "command")
        command_event = fetch_work_trace_event(self.paths, {"event_id": command_event_id})["event"]
        self.assertEqual(command_event["details"]["command"], "append-entry")

    def _request_test_server(self):
        server = AgentDiaryHTTPServer(("127.0.0.1", 0), self.paths)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_http_post_rejects_malformed_json_cleanly(self) -> None:
        server = self._request_test_server()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        self.addCleanup(conn.close)

        conn.request(
            "POST",
            "/search_memory",
            body=b"{not-json",
            headers={"Content-Type": "application/json", "Content-Length": "9"},
        )
        response = conn.getresponse()
        body = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, 400)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_http_post_rejects_oversized_body_before_reading_route(self) -> None:
        server = self._request_test_server()
        original_limit = AgentDiaryHandler.max_body_bytes
        AgentDiaryHandler.max_body_bytes = 8
        self.addCleanup(setattr, AgentDiaryHandler, "max_body_bytes", original_limit)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        self.addCleanup(conn.close)

        payload = json.dumps({"query": "memory", "limit": 1}).encode("utf-8")
        conn.request(
            "POST",
            "/search_memory",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = conn.getresponse()
        body = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, 413)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "payload_too_large")


    def test_http_cors_does_not_allow_cross_origin_by_default(self) -> None:
        server = self._request_test_server()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        self.addCleanup(conn.close)

        conn.request(
            "OPTIONS",
            "/search_memory",
            headers={"Origin": "http://evil.example", "Host": f"127.0.0.1:{server.server_port}"},
        )
        response = conn.getresponse()
        response.read()

        self.assertEqual(response.status, 204)
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))

    def test_http_cors_allows_same_origin_only(self) -> None:
        server = self._request_test_server()
        origin = f"http://127.0.0.1:{server.server_port}"
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        self.addCleanup(conn.close)

        conn.request(
            "OPTIONS",
            "/search_memory",
            headers={"Origin": origin, "Host": f"127.0.0.1:{server.server_port}"},
        )
        response = conn.getresponse()
        response.read()

        self.assertEqual(response.status, 204)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), origin)

    def test_ui_avoids_interpolated_html_for_api_controlled_lists(self) -> None:
        ui_js = (Path(__file__).resolve().parents[1] / "ui" / "app.js").read_text(encoding="utf-8")

        self.assertNotIn("li.innerHTML = `", ui_js)
        self.assertNotIn("insertAdjacentHTML", ui_js)
        self.assertIn("preview.textContent = hit.match_text", ui_js)
        self.assertIn("strong.textContent = item.import_id", ui_js)

    def test_bootstrap_sqlite_sets_wal_busy_timeout_and_core_indexes(self) -> None:
        with sqlite3.connect(self.paths.sqlite_path) as conn:
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            indexes = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = ?", ("index",)).fetchall()
            }

        with sqlite3.connect(self.paths.sqlite_path) as conn:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = ?", ("table",)).fetchall()
            }

        self.assertEqual(journal_mode.lower(), "wal")
        self.assertGreaterEqual(int(busy_timeout), 5000)
        self.assertIn("idx_entries_created_at", indexes)
        self.assertIn("idx_work_trace_created_at", indexes)
        self.assertIn("idx_memory_index_entry_created", indexes)
        self.assertIn("memory_index_fts", tables)
        self.assertIn("work_trace_fts", tables)

    def test_list_entries_preserves_user_visible_memory_fields(self) -> None:
        appended = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "ui-contract-test",
                "author_role": "mixed",
                "content": "User: keep the memory visible\nAssistant: yes, the UI contract stays intact",
                "created_at": "2026-07-13T10:00:00+00:00",
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": appended["entry_id"],
                "artifact_type": "conversation-brief",
                "producer": "test",
                "content": "Visible brief for the human UI.",
            },
        )

        listed = list_entries(self.paths, {"limit": 1})
        item = listed["items"][0]

        self.assertEqual(item["entry_id"], appended["entry_id"])
        for key in ("entry_id", "created_at", "entry_type", "source", "author_role", "brief", "preview", "provenance", "work_trace_count"):
            self.assertIn(key, item)
        self.assertEqual(item["brief"], "Visible brief for the human UI.")

    def test_status_handler_contract(self) -> None:
        body = status(self.paths)
        self.assertTrue(body["ok"])
        self.assertEqual(body["sqlite_path"], str(self.paths.sqlite_path))

    def test_doctor_reports_ok_for_consistent_backend_state(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "doctor-test",
                "author_role": "human",
                "content": "doctor should see this raw entry",
                "created_at": "2026-07-13T12:00:00+00:00",
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "compressed-memory",
                "producer": "test",
                "content": "doctor consistent memory",
                "created_at": "2026-07-13T12:01:00+00:00",
            },
        )
        append_work_trace_event(
            self.paths,
            {
                "event_type": "test_run",
                "summary": "doctor consistent work trace",
                "created_at": "2026-07-13T12:02:00+00:00",
                "related_entry_ids": [entry["entry_id"]],
            },
        )

        out = run_doctor(self.paths)

        self.assertTrue(out["ok"])
        self.assertEqual(out["summary"]["error_count"], 0)
        self.assertEqual(out["summary"]["entry_count"], 1)
        check_names = {check["name"] for check in out["checks"]}
        self.assertIn("memory_fts_count", check_names)
        self.assertIn("work_trace_fts_count", check_names)

    def test_doctor_reports_missing_raw_entry_file(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "doctor-test",
                "author_role": "human",
                "content": "doctor should catch missing files",
                "created_at": "2026-07-13T12:10:00+00:00",
            },
        )
        Path(entry["raw_file"]).unlink()

        out = run_doctor(self.paths)

        self.assertFalse(out["ok"])
        self.assertGreater(out["summary"]["error_count"], 0)
        self.assertIn("missing_entry_file", {issue["code"] for issue in out["issues"]})

    def test_http_routes_include_list_imports_and_producer_endpoints(self) -> None:
        self.assertIn("/append_work_trace", AgentDiaryHandler.routes)
        self.assertIn("/search_all", AgentDiaryHandler.routes)
        self.assertIn("/search_work_trace", AgentDiaryHandler.routes)
        self.assertIn("/fetch_work_trace", AgentDiaryHandler.routes)
        self.assertIn("/list_work_trace", AgentDiaryHandler.routes)
        self.assertIn("/list_imports", AgentDiaryHandler.routes)
        self.assertIn("/append_overlay", AgentDiaryHandler.routes)
        self.assertIn("/produce_open_loops", AgentDiaryHandler.routes)
        self.assertIn("/produce_conversation_briefs", AgentDiaryHandler.routes)
        self.assertIn("/produce_compressed_memory", AgentDiaryHandler.routes)

    def test_attach_compressed_memory_artifact_creates_memory_index_row(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "raw truth entry",
                "created_at": "2026-05-21T09:00:00+00:00",
            },
        )

        attached = attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "compressed-memory",
                "producer": "agent-v1",
                "content": "user prefers concise status updates",
                "created_at": "2026-05-21T09:01:00+00:00",
            },
        )

        self.assertTrue(Path(attached["artifact_file"]).exists())
        self.assertTrue(attached["indexed_in_memory"])

        with sqlite3.connect(self.paths.sqlite_path) as conn:
            row = conn.execute(
                """
                SELECT entry_id, artifact_id, created_at, memory_text
                FROM memory_index
                WHERE artifact_id = ?
                """,
                (attached["artifact_id"],),
            ).fetchone()

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row[0], entry["entry_id"])
        self.assertEqual(row[1], attached["artifact_id"])
        self.assertEqual(row[2], "2026-05-21T09:01:00+00:00")
        self.assertEqual(row[3], "user prefers concise status updates")

    def test_build_conversation_brief_text_summarizes_dialogue(self) -> None:
        text = build_conversation_brief_text(
            {
                "content": (
                    "SampleUser: can you check whether the browser node is back?\n"
                    "Assistant: yes, I’ll inspect the Mac-side route now.\n"
                    "SampleUser: great. let me know if the tunnel still works."
                )
            }
        )
        self.assertIn("User starts with", text)
        self.assertIn("Assistant", text)
        self.assertIn("browser node", text.lower())

    def test_build_compressed_memory_text_preserves_questions_responses_and_keywords(self) -> None:
        text = build_compressed_memory_text(
            {
                "created_at": "2026-05-22T12:00:00+00:00",
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "content": (
                    "SampleUser: can you check whether the browser node is back?\n"
                    "Assistant: yes, I’ll inspect the Mac-side route now.\n"
                    "SampleUser: great. let me know if the tunnel still works."
                ),
            }
        )
        self.assertIn("Source context:", text)
        self.assertIn("User asks:", text)
        self.assertIn("Assistant commitments:", text)
        self.assertIn("Ask/commit pair:", text)
        self.assertIn("Retrieval anchors:", text)
        self.assertIn("browser", text.lower())

    def test_build_compressed_memory_text_pairs_user_ask_with_following_assistant_commitment(self) -> None:
        text = build_compressed_memory_text(
            {
                "created_at": "2026-05-22T12:00:00+00:00",
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "content": (
                    "SampleUser: can you verify whether the browser node is back?\n"
                    "Assistant: yes, I’ll inspect the Mac-side route now.\n"
                    "SampleUser: also can you confirm whether tunnel retries are stable?\n"
                    "Assistant: noted. I will check tunnel retries after route validation."
                ),
            }
        )
        self.assertIn(
            "Ask/commit pair: User asked can you verify whether the browser node is back? -> Assistant committed yes, I’ll inspect the Mac-side route now.",
            text,
        )

    def test_build_compressed_memory_text_retrieval_anchors_preserve_phrases(self) -> None:
        text = build_compressed_memory_text(
            {
                "created_at": "2026-05-22T12:00:00+00:00",
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "content": (
                    "SampleUser: can you check whether the browser node is back?\n"
                    "Assistant: yes, I’ll inspect the Mac-side route now.\n"
                    "SampleUser: great. let me know if the browser node tunnel works."
                ),
            }
        )
        self.assertIn("Retrieval anchors:", text)
        anchors_line = [line for line in text.splitlines() if line.startswith("Retrieval anchors:")][0].lower()
        anchors = [part.strip() for part in anchors_line.split(":", 1)[1].split(",")]
        self.assertIn("browser node", anchors_line)
        self.assertIn("mac-side route", anchors_line)
        self.assertNotIn("check whether", anchors_line)
        self.assertNotIn("check", anchors)
        self.assertNotIn("great", anchors)
        self.assertNotIn("browser", anchors)
        self.assertNotIn("node", anchors)
        self.assertNotIn("mac-side", anchors)

    def test_build_compressed_memory_text_regression_representative_artifact_shape(self) -> None:
        text = build_compressed_memory_text(
            {
                "created_at": "2026-05-22T12:00:00+00:00",
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "content": (
                    "SampleUser: can you check whether the browser node is back?\n"
                    "Assistant: yes, I’ll inspect the Mac-side route now.\n"
                    "SampleUser: great. let me know if the browser node tunnel works."
                ),
            }
        )
        self.assertIn("User asks:", text)
        self.assertIn("Assistant commitments:", text)
        self.assertIn("Ask/commit pair:", text)
        self.assertIn("Retrieval anchors:", text)
        anchors_line = [line for line in text.splitlines() if line.startswith("Retrieval anchors:")][0].lower()
        anchors = [part.strip() for part in anchors_line.split(":", 1)[1].split(",")]

        self.assertIn("browser node", anchors)
        self.assertIn("mac-side route", anchors)
        self.assertNotIn("check whether", anchors_line)
        self.assertNotIn("check", anchors)
        self.assertNotIn("great", anchors)
        self.assertNotIn("browser", anchors)
        self.assertNotIn("node", anchors)

    def test_search_memory_returns_match_linked_to_entry_id(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "raw discussion",
                "created_at": "2026-05-21T10:00:00+00:00",
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "memory",
                "producer": "agent-v1",
                "content": "customer project deadline is friday",
                "created_at": "2026-05-21T10:01:00+00:00",
            },
        )

        results = search_memory(self.paths, {"query": "deadline", "limit": 10})
        self.assertEqual(results["query"], "deadline")
        self.assertEqual(len(results["matches"]), 1)
        hit = results["matches"][0]
        self.assertEqual(hit["entry_id"], entry["entry_id"])
        self.assertEqual(hit["fetch_raw_entry"]["entry_id"], entry["entry_id"])
        self.assertIn("deadline", hit["match_text"])
        self.assertNotIn("raw_file_path", hit)
        self.assertLessEqual(len(hit["match_text"]), 130)

    def test_fetch_raw_entry_after_search_hit_remains_truth_path(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "raw truth should remain authoritative",
                "created_at": "2026-05-21T11:00:00+00:00",
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "compressed-memory",
                "producer": "agent-v1",
                "content": "summary: raw truth is authoritative",
                "created_at": "2026-05-21T11:01:00+00:00",
            },
        )

        search = search_memory(self.paths, {"query": "authoritative", "limit": 5})
        hit_entry_id = search["matches"][0]["entry_id"]

        fetched = fetch_raw_entry(self.paths, {"entry_id": hit_entry_id})
        self.assertEqual(fetched["entry"]["entry_id"], entry["entry_id"])
        self.assertEqual(fetched["entry"]["content"], "raw truth should remain authoritative")

    def test_search_memory_prefers_latest_compressed_memory_per_entry(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "raw conversation body",
                "created_at": "2026-05-21T11:05:00+00:00",
            },
        )
        older = attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "compressed-memory",
                "producer": "compressed-memory.v2",
                "content": "older compressed text mentions tunnel",
                "created_at": "2026-05-21T11:06:00+00:00",
            },
        )
        newer = attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "compressed-memory",
                "producer": "compressed-memory.v2",
                "content": "newest compressed text mentions tunnel and browser node",
                "created_at": "2026-05-21T11:07:00+00:00",
            },
        )

        with sqlite3.connect(self.paths.sqlite_path) as conn:
            fts_count = conn.execute("SELECT COUNT(*) FROM memory_index_fts").fetchone()[0]
        self.assertGreaterEqual(fts_count, 2)

        results = search_memory(self.paths, {"query": "tunnel", "limit": 10})
        entry_hits = [m for m in results["matches"] if m["entry_id"] == entry["entry_id"]]
        self.assertEqual(len(entry_hits), 1)
        self.assertEqual(entry_hits[0]["artifact_id"], newer["artifact_id"])
        self.assertIn("newest compressed text", entry_hits[0]["match_text"])
        self.assertNotEqual(entry_hits[0]["artifact_id"], older["artifact_id"])

    def test_bootstrap_legacy_memory_index_adds_created_at_safely(self) -> None:
        legacy_db = self.root / "legacy.db"
        with sqlite3.connect(legacy_db) as conn:
            conn.executescript(
                """
                CREATE TABLE entries (
                  entry_id TEXT PRIMARY KEY,
                  created_at TEXT NOT NULL,
                  title TEXT,
                  source TEXT NOT NULL,
                  author_role TEXT NOT NULL,
                  raw_file_path TEXT NOT NULL
                );
                CREATE TABLE artifacts (
                  artifact_id TEXT PRIMARY KEY,
                  entry_id TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  artifact_type TEXT NOT NULL,
                  producer TEXT NOT NULL,
                  content TEXT NOT NULL
                );
                CREATE TABLE memory_index (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  entry_id TEXT NOT NULL,
                  artifact_id TEXT,
                  memory_text TEXT NOT NULL,
                  tags TEXT
                );
                """
            )
            conn.execute(
                "INSERT INTO memory_index(entry_id, artifact_id, memory_text, tags) VALUES (?, ?, ?, ?)",
                ("entry_legacy", "artifact_legacy", "legacy memory text", None),
            )
            conn.commit()

        bootstrap_sqlite(legacy_db)

        with sqlite3.connect(legacy_db) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_index)").fetchall()}
            created_at = conn.execute(
                "SELECT created_at FROM memory_index WHERE artifact_id = ?",
                ("artifact_legacy",),
            ).fetchone()

        self.assertIn("created_at", cols)
        self.assertIsNotNone(created_at)
        assert created_at is not None
        self.assertEqual(created_at[0], "")

    def test_search_memory_ranks_stronger_matches_first(self) -> None:
        strong_entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "raw strong",
                "created_at": "2026-05-22T09:00:00+00:00",
            },
        )
        weak_entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "raw weak",
                "created_at": "2026-05-22T09:00:01+00:00",
            },
        )

        attach_artifact(
            self.paths,
            {
                "entry_id": weak_entry["entry_id"],
                "artifact_type": "memory",
                "producer": "agent-v1",
                "content": "deadline mentioned once",
                "created_at": "2026-05-22T09:01:00+00:00",
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": strong_entry["entry_id"],
                "artifact_type": "memory",
                "producer": "agent-v1",
                "content": "project deadline friday and deadline planning details",
                "created_at": "2026-05-22T09:01:01+00:00",
            },
        )

        results = search_memory(self.paths, {"query": "deadline friday", "limit": 10})
        self.assertEqual(results["matches"][0]["entry_id"], strong_entry["entry_id"])
        self.assertIn("deadline", results["matches"][0]["match_text"])

    def test_search_memory_returns_compact_snippet_instead_of_full_text(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "raw content",
                "created_at": "2026-05-22T10:00:00+00:00",
            },
        )
        long_text = (
            "This is a long compressed memory entry that includes setup context, "
            "follow-up notes, and the target phrase deadline near the middle of the text "
            "with additional trailing details that should be truncated for compact recall output."
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "compressed-memory",
                "producer": "agent-v1",
                "content": long_text,
                "created_at": "2026-05-22T10:01:00+00:00",
            },
        )

        results = search_memory(self.paths, {"query": "deadline", "limit": 5})
        snippet = results["matches"][0]["match_text"]
        self.assertIn("deadline", snippet.lower())
        self.assertLess(len(snippet), len(long_text))

    def test_search_memory_raw_fallback_matches_overlay_effective_content(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "raw body without the target token",
                "created_at": "2026-05-22T10:05:00+00:00",
            },
        )
        append_overlay(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "overlay_type": "correction",
                "author": "operator",
                "content": "Effective correction mentions overlay-search-token.",
            },
        )

        results = search_memory(self.paths, {"query": "overlay-search-token", "limit": 5})
        self.assertEqual(results["match_summary"]["compressed_memory_hits"], 0)
        self.assertEqual(results["match_summary"]["raw_entry_hits"], 1)
        self.assertTrue(results["match_summary"]["using_raw_layer"])
        self.assertEqual(len(results["matches"]), 1)
        self.assertEqual(results["matches"][0]["entry_id"], entry["entry_id"])

    def test_search_memory_merges_raw_and_compressed_hits_per_entry(self) -> None:
        raw_first = append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "Ship the exact phrase mac-side route before noon.",
                "created_at": "2026-05-22T10:06:00+00:00",
            },
        )
        compressed_only = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "raw body without the target phrase",
                "created_at": "2026-05-22T10:07:00+00:00",
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": raw_first["entry_id"],
                "artifact_type": "compressed-memory",
                "producer": "agent-v1",
                "content": "summary mentions route planning in softer terms",
                "created_at": "2026-05-22T10:08:00+00:00",
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": compressed_only["entry_id"],
                "artifact_type": "compressed-memory",
                "producer": "agent-v1",
                "content": "compressed memory explicitly mentions mac-side route work",
                "created_at": "2026-05-22T10:09:00+00:00",
            },
        )

        results = search_memory(self.paths, {"query": "mac-side route", "limit": 5})
        self.assertEqual(results["matches"][0]["entry_id"], raw_first["entry_id"])
        self.assertEqual(results["matches"][0]["match_layer"], "raw_entry_fallback")
        self.assertIn("compressed_memory", results["matches"][0]["supporting_layers"])
        self.assertIn("raw_entry", results["matches"][0]["supporting_layers"])
        self.assertEqual(results["match_summary"]["compressed_memory_hits"], 2)
        self.assertEqual(results["match_summary"]["raw_entry_hits"], 1)
        self.assertEqual(results["match_summary"]["entry_matches"], 2)

    def test_search_memory_filters_compressed_hits_by_source_conversation_id(self) -> None:
        source_file_a = self.root / "search-compressed-scope-a.jsonl"
        source_file_a.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "mixed",
                    "created_at": "2026-05-25T12:00:00+00:00",
                    "content": "Scoped A raw entry.",
                    "metadata": {"source_message_id": "search-scope-a-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        source_file_b = self.root / "search-compressed-scope-b.jsonl"
        source_file_b.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "mixed",
                    "created_at": "2026-05-25T12:01:00+00:00",
                    "content": "Scoped B raw entry.",
                    "metadata": {"source_message_id": "search-scope-b-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        imported_a = import_session_jsonl(
            self.paths,
            {
                "path": str(source_file_a),
                "import_id": "import-search-scope-a",
                "source_session_id": "session-search-scope-a",
                "source_conversation_id": "telegram:search-scope-a",
            },
        )
        imported_b = import_session_jsonl(
            self.paths,
            {
                "path": str(source_file_b),
                "import_id": "import-search-scope-b",
                "source_session_id": "session-search-scope-b",
                "source_conversation_id": "telegram:search-scope-b",
            },
        )
        entry_id_a = imported_a["imported"][0]["entry_id"]
        entry_id_b = imported_b["imported"][0]["entry_id"]
        attach_artifact(
            self.paths,
            {
                "entry_id": entry_id_a,
                "artifact_type": "compressed-memory",
                "producer": "compressed-memory.v2",
                "content": "shared-token-for-scope appears in conversation A",
                "created_at": "2026-05-25T12:02:00+00:00",
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry_id_b,
                "artifact_type": "compressed-memory",
                "producer": "compressed-memory.v2",
                "content": "shared-token-for-scope appears in conversation B",
                "created_at": "2026-05-25T12:03:00+00:00",
            },
        )

        results = search_memory(
            self.paths,
            {
                "query": "shared-token-for-scope",
                "limit": 10,
                "source_conversation_id": "telegram:search-scope-a",
            },
        )
        self.assertEqual(len(results["matches"]), 1)
        self.assertEqual(results["matches"][0]["entry_id"], entry_id_a)
        self.assertEqual(results["matches"][0]["match_layer"], "compressed_memory")

    def test_search_memory_scope_recall_scans_older_entries_outside_recent_global_slice(self) -> None:
        older_file = self.root / "search-scope-older.jsonl"
        older_file.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "human",
                    "created_at": "2026-05-25T09:00:00+00:00",
                    "content": "older scoped raw body without the phrase",
                    "metadata": {"source_message_id": "older-scope-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        imported = import_session_jsonl(
            self.paths,
            {
                "path": str(older_file),
                "import_id": "import-search-scope-older",
                "source_session_id": "session-search-scope-older",
                "source_conversation_id": "telegram:scope-older",
            },
        )
        entry_id = imported["imported"][0]["entry_id"]
        attach_artifact(
            self.paths,
            {
                "entry_id": entry_id,
                "artifact_type": "compressed-memory",
                "producer": "compressed-memory.v2",
                "content": "scope-needle-rare appears only in this older scoped conversation",
                "created_at": "2026-05-25T09:01:00+00:00",
            },
        )
        for idx in range(260):
            append_entry(
                self.paths,
                {
                    "entry_type": "manual_note",
                    "source": "cli",
                    "author_role": "human",
                    "content": f"newer unrelated filler note {idx}",
                    "created_at": f"2026-05-25T12:{idx % 60:02d}:00+00:00",
                },
            )

        results = search_memory(
            self.paths,
            {
                "query": "scope-needle-rare",
                "limit": 10,
                "source_conversation_id": "telegram:scope-older",
            },
        )
        self.assertEqual(len(results["matches"]), 1)
        self.assertEqual(results["matches"][0]["entry_id"], entry_id)
        self.assertEqual(results["matches"][0]["match_layer"], "compressed_memory")

    def test_search_memory_raw_fallback_respects_import_id_and_truthful_only(self) -> None:
        imported_file_a = self.root / "search-fallback-import-a.jsonl"
        imported_file_a.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "human",
                    "created_at": "2026-05-25T12:10:00+00:00",
                    "content": "fallback-needle-unique appears in imported entry A",
                    "metadata": {"source_message_id": "fallback-a-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        imported_file_b = self.root / "search-fallback-import-b.jsonl"
        imported_file_b.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "human",
                    "created_at": "2026-05-25T12:11:00+00:00",
                    "content": "fallback-needle-unique appears in imported entry B",
                    "metadata": {"source_message_id": "fallback-b-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        imported_a = import_session_jsonl(
            self.paths,
            {
                "path": str(imported_file_a),
                "import_id": "import-fallback-target",
                "source_session_id": "session-fallback-a",
                "source_conversation_id": "telegram:fallback-a",
            },
        )
        import_session_jsonl(
            self.paths,
            {
                "path": str(imported_file_b),
                "import_id": "import-fallback-other",
                "source_session_id": "session-fallback-b",
                "source_conversation_id": "telegram:fallback-b",
            },
        )
        append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "fallback-needle-unique appears in manual note and should be filtered by truthful_only.",
                "created_at": "2026-05-25T12:12:00+00:00",
            },
        )

        scoped = search_memory(
            self.paths,
            {
                "query": "fallback-needle-unique",
                "limit": 10,
                "import_id": "import-fallback-target",
                "truthful_only": True,
            },
        )
        self.assertEqual(scoped["match_summary"]["compressed_memory_hits"], 0)
        self.assertTrue(scoped["match_summary"]["using_raw_layer"])
        self.assertEqual(len(scoped["matches"]), 1)
        self.assertEqual(scoped["matches"][0]["entry_id"], imported_a["imported"][0]["entry_id"])

    def test_search_memory_unscoped_behavior_remains_compatible(self) -> None:
        source_file_a = self.root / "search-unscoped-a.jsonl"
        source_file_a.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "human",
                    "created_at": "2026-05-25T12:20:00+00:00",
                    "content": "unscoped-needle-token in imported A",
                    "metadata": {"source_message_id": "search-unscoped-a-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        source_file_b = self.root / "search-unscoped-b.jsonl"
        source_file_b.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "human",
                    "created_at": "2026-05-25T12:21:00+00:00",
                    "content": "unscoped-needle-token in imported B",
                    "metadata": {"source_message_id": "search-unscoped-b-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        import_session_jsonl(
            self.paths,
            {
                "path": str(source_file_a),
                "import_id": "import-search-unscoped-a",
                "source_session_id": "session-search-unscoped-a",
                "source_conversation_id": "telegram:search-unscoped-a",
            },
        )
        import_session_jsonl(
            self.paths,
            {
                "path": str(source_file_b),
                "import_id": "import-search-unscoped-b",
                "source_session_id": "session-search-unscoped-b",
                "source_conversation_id": "telegram:search-unscoped-b",
            },
        )

        results = search_memory(self.paths, {"query": "unscoped-needle-token", "limit": 10})
        self.assertGreaterEqual(len(results["matches"]), 2)

    def test_cli_search_memory_forwards_provenance_filters(self) -> None:
        source_file = self.root / "cli-search-scoped.jsonl"
        source_file.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "human",
                    "created_at": "2026-05-25T12:30:00+00:00",
                    "content": "cli-search-needle appears in scoped import entry",
                    "metadata": {"source_message_id": "cli-search-scoped-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        other_file = self.root / "cli-search-other.jsonl"
        other_file.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "human",
                    "created_at": "2026-05-25T12:31:00+00:00",
                    "content": "cli-search-needle appears in other import entry",
                    "metadata": {"source_message_id": "cli-search-other-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        import_session_jsonl(
            self.paths,
            {
                "path": str(source_file),
                "import_id": "import-cli-search-target",
                "source_session_id": "session-cli-search-target",
                "source_conversation_id": "telegram:cli-search-target",
            },
        )
        import_session_jsonl(
            self.paths,
            {
                "path": str(other_file),
                "import_id": "import-cli-search-other",
                "source_session_id": "session-cli-search-other",
                "source_conversation_id": "telegram:cli-search-other",
            },
        )

        cmd = [
            sys.executable,
            "-m",
            "agent_diary.cli.main",
            "--json",
            "search-memory",
            "--query",
            "cli-search-needle",
            "--limit",
            "20",
            "--import-id",
            "import-cli-search-target",
            "--truthful-only",
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        completed = subprocess.run(
            cmd,
            cwd=self.root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        out = json.loads(completed.stdout)
        self.assertEqual(out["filters"]["import_id"], "import-cli-search-target")
        self.assertTrue(out["filters"]["truthful_only"])
        self.assertEqual(len(out["matches"]), 1)

    def test_list_entries_returns_human_browse_fields(self) -> None:
        append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "This is a human-readable diary entry body for preview testing.",
                "created_at": "2026-05-23T09:00:00+00:00",
            },
        )

        out = list_entries(self.paths, {"limit": 10, "offset": 0})
        self.assertEqual(out["limit"], 10)
        self.assertEqual(out["offset"], 0)
        self.assertGreaterEqual(len(out["items"]), 1)
        item = out["items"][0]
        self.assertIn("entry_id", item)
        self.assertIn("created_at", item)
        self.assertIn("entry_type", item)
        self.assertIn("source", item)
        self.assertIn("author_role", item)
        self.assertIn("preview", item)
        self.assertEqual(item["entry_type"], "manual_note")
        self.assertEqual(item["source"], "cli")
        self.assertEqual(item["author_role"], "human")

    def test_list_entries_prefers_newest_conversation_brief_by_created_at(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "author_role": "mixed",
                "content": "SampleUser: summarize status.\nAssistant: on it.",
                "created_at": "2026-05-23T09:30:00+00:00",
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "conversation-brief",
                "producer": "conversation-brief.v1",
                "content": "older brief text",
                "created_at": "2026-05-23T09:31:00+00:00",
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "conversation-brief",
                "producer": "conversation-brief.v1",
                "content": "newer brief text",
                "created_at": "2026-05-23T09:32:00+00:00",
            },
        )

        out = list_entries(self.paths, {"limit": 10, "offset": 0})
        item = [row for row in out["items"] if row["entry_id"] == entry["entry_id"]][0]
        self.assertEqual(item["brief"], "newer brief text")

    def test_fetch_entry_detail_returns_raw_primary_body(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "verbatim primary body",
                "created_at": "2026-05-23T10:00:00+00:00",
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "compressed-memory",
                "producer": "agent-v1",
                "content": "secondary memory summary",
                "created_at": "2026-05-23T10:01:00+00:00",
            },
        )

        detail = fetch_entry_detail(self.paths, {"entry_id": entry["entry_id"]})
        self.assertEqual(detail["entry_id"], entry["entry_id"])
        self.assertEqual(detail["raw_entry"]["content"], "verbatim primary body")
        self.assertEqual(detail["truth_model"]["primary"], "raw_entry")
        self.assertEqual(detail["truth_model"]["secondary"], "artifacts")

    def test_fetch_entry_detail_includes_linked_work_trace_events(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "Working through the UI panel decision.",
                "created_at": "2026-05-23T10:00:00+00:00",
            },
        )
        append_work_trace_event(
            self.paths,
            {
                "event_type": "file_change",
                "summary": "Added the Agent Work panel to the UI.",
                "created_at": "2026-05-23T10:02:00+00:00",
                "project": "agent-diary",
                "actor": "assistant",
                "source_surface": "openclaw-session",
                "related_entry_ids": [entry["entry_id"]],
                "related_paths": ["ui/index.html", "ui/app.js"],
                "details": {"change_kind": "update", "area": "right-side panel"},
            },
        )

        detail = fetch_entry_detail(self.paths, {"entry_id": entry["entry_id"]})
        self.assertGreaterEqual(detail["work_trace"]["summary"]["total_count"], 1)
        self.assertIn("file_change", detail["work_trace"]["summary"]["event_types"])
        event = next(
            item
            for item in detail["work_trace"]["events"]
            if item["summary"] == "Added the Agent Work panel to the UI."
        )
        self.assertEqual(event["summary"], "Added the Agent Work panel to the UI.")
        self.assertEqual(event["related_entry_ids"], [entry["entry_id"]])
        self.assertEqual(event["related_paths"], ["ui/index.html", "ui/app.js"])
        self.assertEqual(event["details"]["area"], "right-side panel")

    def test_fetch_entry_detail_includes_artifact_linkage_metadata(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "truth record",
                "created_at": "2026-05-23T11:00:00+00:00",
            },
        )
        attached = attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "memory",
                "producer": "agent-v1",
                "content": "memory helper",
                "created_at": "2026-05-23T11:01:00+00:00",
            },
        )

        detail = fetch_entry_detail(self.paths, {"entry_id": entry["entry_id"]})
        self.assertEqual(len(detail["artifacts"]), 1)
        artifact = detail["artifacts"][0]
        self.assertEqual(artifact["artifact_id"], attached["artifact_id"])
        self.assertEqual(artifact["artifact_type"], "memory")
        self.assertEqual(artifact["producer"], "agent-v1")
        self.assertEqual(detail["artifact_summary"]["total_count"], 1)
        self.assertEqual(detail["artifact_summary"]["current_count"], 1)
        self.assertEqual(detail["artifact_summary"]["artifact_types"], ["memory"])
        self.assertEqual(detail["artifact_summary"]["current_by_type"]["memory"]["artifact_id"], attached["artifact_id"])

    def test_fetch_entry_detail_includes_conversation_brief_content(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "author_role": "mixed",
                "content": "SampleUser: what happened on may 7th?\nAssistant: here is the rundown.",
                "created_at": "2026-05-23T11:00:00+00:00",
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "conversation-brief",
                "producer": "conversation-brief.v1",
                "content": "User asks for a May 7 rundown and Assistant responds with a concise recap.",
                "created_at": "2026-05-23T11:01:00+00:00",
                "metadata": {
                    "schema_version": "conversation-brief.v1",
                    "method": "deterministic-dialogue-brief-v1",
                    "source_entry_id": entry["entry_id"],
                },
            },
        )

        detail = fetch_entry_detail(self.paths, {"entry_id": entry["entry_id"]})
        brief = [artifact for artifact in detail["artifacts"] if artifact["artifact_type"] == "conversation-brief"][0]
        self.assertIn("May 7 rundown", brief["content"])
        provenance = brief.get("provenance", {})
        self.assertEqual(provenance.get("schema_version"), "conversation-brief.v1")
        self.assertEqual(provenance.get("method"), "deterministic-dialogue-brief-v1")
        self.assertEqual(provenance.get("source_entry_ids"), [entry["entry_id"]])
        self.assertEqual(detail["entry_provenance"]["source_conversation_id"], None)

    def test_fetch_entry_detail_marks_latest_artifact_as_current_per_type(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "author_role": "mixed",
                "content": "SampleUser: status?\nAssistant: checking now.",
                "created_at": "2026-05-23T11:30:00+00:00",
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "conversation-brief",
                "producer": "conversation-brief.v1",
                "content": "older brief",
                "created_at": "2026-05-23T11:31:00+00:00",
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "conversation-brief",
                "producer": "conversation-brief.v1",
                "content": "newer brief",
                "created_at": "2026-05-23T11:32:00+00:00",
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "compressed-memory",
                "producer": "compressed-memory.v2",
                "content": "older memory",
                "created_at": "2026-05-23T11:33:00+00:00",
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "compressed-memory",
                "producer": "compressed-memory.v2",
                "content": "newer memory",
                "created_at": "2026-05-23T11:34:00+00:00",
            },
        )

        detail = fetch_entry_detail(self.paths, {"entry_id": entry["entry_id"]})
        artifacts = detail["artifacts"]
        self.assertEqual(
            [a["created_at"] for a in artifacts],
            sorted([a["created_at"] for a in artifacts], reverse=True),
        )

        briefs = [a for a in artifacts if a["artifact_type"] == "conversation-brief"]
        self.assertEqual(len(briefs), 2)
        self.assertEqual(sum(1 for a in briefs if a.get("is_current")), 1)
        current_brief = [a for a in briefs if a.get("is_current")][0]
        self.assertEqual(current_brief["content"], "newer brief")

        memories = [a for a in artifacts if a["artifact_type"] == "compressed-memory"]
        self.assertEqual(len(memories), 2)
        self.assertEqual(sum(1 for a in memories if a.get("is_current")), 1)
        current_memory = [a for a in memories if a.get("is_current")][0]
        self.assertEqual(current_memory["content"], "newer memory")
        superseded_memory = [a for a in memories if not a.get("is_current")][0]
        self.assertEqual(superseded_memory["lifecycle_status"], "superseded")
        self.assertEqual(current_memory["lifecycle_status"], "active")

    def test_fetch_entry_detail_includes_open_loop_provenance_for_multi_entry_artifact(self) -> None:
        e1 = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "TODO: follow up on budget decision this week.",
                "created_at": "2026-05-23T12:30:00+00:00",
            },
        )
        e2 = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "Pending: next step is to confirm budget approval timing.",
                "created_at": "2026-05-23T12:31:00+00:00",
            },
        )
        produced = produce_open_loops(self.paths, {"entry_ids": [e1["entry_id"], e2["entry_id"]], "limit": 10})
        self.assertGreaterEqual(produced["loop_count"], 1)

        detail = fetch_entry_detail(self.paths, {"entry_id": e1["entry_id"]})
        open_loop = [a for a in detail["artifacts"] if a["artifact_type"] == "analysis:open-loop"][0]
        provenance = open_loop.get("provenance", {})
        self.assertEqual(provenance.get("schema_version"), "open-loop.v1")
        self.assertEqual(provenance.get("method"), "keyword-window-v1")
        self.assertEqual(provenance.get("method_version"), "1")
        self.assertTrue(str(provenance.get("generated_at", "")).strip())
        self.assertEqual(
            sorted(provenance.get("source_entry_ids", [])),
            sorted([e1["entry_id"], e2["entry_id"]]),
        )
        self.assertIsInstance(provenance.get("analysis_window"), dict)

    def test_fetch_entry_detail_marks_artifact_overlay_stale_when_overlay_is_newer(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "Initial note.",
                "created_at": "2026-05-23T10:00:00+00:00",
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "conversation-brief",
                "producer": "conversation-brief.v1",
                "content": "Brief generated before correction.",
                "created_at": "2026-05-23T10:05:00+00:00",
                "metadata": {"generated_at": "2026-05-23T10:05:00+00:00"},
            },
        )
        append_overlay(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "overlay_type": "correction",
                "author": "operator",
                "content": "Pending: follow up with corrected timeline.",
                "created_at": "2026-05-23T10:06:00+00:00",
            },
        )

        detail = fetch_entry_detail(self.paths, {"entry_id": entry["entry_id"]})
        artifact = [a for a in detail["artifacts"] if a["artifact_type"] == "conversation-brief"][0]
        self.assertTrue(artifact["overlay_stale"])
        self.assertEqual(artifact["overlay_stale_reason"], "overlay_added_after_artifact_generation")
        self.assertEqual(artifact["latest_overlay_at"], "2026-05-23T10:06:00+00:00")
        self.assertEqual(artifact["artifact_generated_at"], "2026-05-23T10:05:00+00:00")
        self.assertEqual(detail["artifact_summary"]["stale_count"], 1)

    def test_fetch_entry_detail_overlay_staleness_false_without_overlays(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "No overlays here.",
                "created_at": "2026-05-23T11:00:00+00:00",
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "compressed-memory",
                "producer": "compressed-memory.v2",
                "content": "Compressed summary.",
                "created_at": "2026-05-23T11:01:00+00:00",
            },
        )

        detail = fetch_entry_detail(self.paths, {"entry_id": entry["entry_id"]})
        artifact = [a for a in detail["artifacts"] if a["artifact_type"] == "compressed-memory"][0]
        self.assertFalse(artifact["overlay_stale"])
        self.assertIsNone(artifact["latest_overlay_at"])

    def test_fetch_entry_detail_overlay_staleness_false_when_overlay_older_than_artifact(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "Base content.",
                "created_at": "2026-05-23T12:00:00+00:00",
            },
        )
        append_overlay(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "overlay_type": "annotation",
                "author": "operator",
                "content": "Old note from before generation.",
                "created_at": "2026-05-23T12:01:00+00:00",
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "conversation-brief",
                "producer": "conversation-brief.v1",
                "content": "Regenerated after overlay.",
                "created_at": "2026-05-23T12:02:00+00:00",
                "metadata": {"generated_at": "2026-05-23T12:02:00+00:00"},
            },
        )

        detail = fetch_entry_detail(self.paths, {"entry_id": entry["entry_id"]})
        artifact = [a for a in detail["artifacts"] if a["artifact_type"] == "conversation-brief"][0]
        self.assertFalse(artifact["overlay_stale"])
        self.assertEqual(artifact["latest_overlay_at"], "2026-05-23T12:01:00+00:00")
        self.assertEqual(artifact["artifact_generated_at"], "2026-05-23T12:02:00+00:00")

    def test_attach_artifact_marks_prior_same_scope_artifacts_superseded(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "author_role": "mixed",
                "content": "SampleUser: status?\nAssistant: checking now.",
                "created_at": "2026-05-23T11:30:00+00:00",
            },
        )
        older = attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "conversation-brief",
                "producer": "conversation-brief.v1",
                "content": "older brief",
                "created_at": "2026-05-23T11:31:00+00:00",
                "metadata": {"source_entry_id": entry["entry_id"]},
            },
        )
        newer = attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "conversation-brief",
                "producer": "conversation-brief.v1",
                "content": "newer brief",
                "created_at": "2026-05-23T11:32:00+00:00",
                "metadata": {"source_entry_id": entry["entry_id"]},
            },
        )

        older_body = json.loads(Path(older["artifact_file"]).read_text(encoding="utf-8"))
        newer_body = json.loads(Path(newer["artifact_file"]).read_text(encoding="utf-8"))
        self.assertEqual(older_body["metadata"]["lifecycle_status"], "superseded")
        self.assertEqual(older_body["metadata"]["superseded_at"], "2026-05-23T11:32:00+00:00")
        self.assertEqual(older_body["metadata"]["superseded_by_artifact_id"], newer["artifact_id"])
        self.assertEqual(newer_body["metadata"]["lifecycle_status"], "active")
        self.assertTrue(str(newer_body["metadata"].get("generation_key", "")).strip())

    def test_normalize_derived_artifact_lifecycle_marks_one_active_per_scope(self) -> None:
        e1 = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "author_role": "mixed",
                "content": "entry one",
                "created_at": "2026-05-23T10:00:00+00:00",
            },
        )
        e2 = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "author_role": "mixed",
                "content": "entry two",
                "created_at": "2026-05-23T10:05:00+00:00",
            },
        )
        older = attach_artifact(
            self.paths,
            {
                "entry_id": e1["entry_id"],
                "artifact_type": "conversation-brief",
                "producer": "conversation-brief.v1",
                "content": "older brief",
                "created_at": "2026-05-23T10:10:00+00:00",
                "metadata": {"source_entry_id": e1["entry_id"], "lifecycle_status": "active"},
            },
        )
        newer = attach_artifact(
            self.paths,
            {
                "entry_id": e1["entry_id"],
                "artifact_type": "conversation-brief",
                "producer": "conversation-brief.v1",
                "content": "newer brief",
                "created_at": "2026-05-23T10:11:00+00:00",
                "metadata": {"source_entry_id": e1["entry_id"], "lifecycle_status": "active"},
            },
        )
        anchor = attach_artifact(
            self.paths,
            {
                "entry_id": e2["entry_id"],
                "artifact_type": "analysis:open-loop",
                "producer": "open-loop.v1",
                "content": json.dumps({"loops": [{"title": "older"}]}),
                "created_at": "2026-05-23T10:12:00+00:00",
                "metadata": {"source_entry_ids": [e1["entry_id"], e2["entry_id"]], "lifecycle_status": "active"},
            },
        )
        linked_newer = attach_artifact(
            self.paths,
            {
                "entry_id": e1["entry_id"],
                "artifact_type": "analysis:open-loop",
                "producer": "open-loop.v1",
                "content": json.dumps({"loops": [{"title": "newer"}]}),
                "created_at": "2026-05-23T10:13:00+00:00",
                "metadata": {"source_entry_ids": [e2["entry_id"], e1["entry_id"]], "lifecycle_status": "active"},
            },
        )

        older_body = json.loads(Path(older["artifact_file"]).read_text(encoding="utf-8"))
        newer_body = json.loads(Path(newer["artifact_file"]).read_text(encoding="utf-8"))
        anchor_body = json.loads(Path(anchor["artifact_file"]).read_text(encoding="utf-8"))
        linked_newer_body = json.loads(Path(linked_newer["artifact_file"]).read_text(encoding="utf-8"))
        # Simulate pre-lifecycle legacy state with ambiguous active markers and missing generation keys.
        for body, file_path in (
            (older_body, Path(older["artifact_file"])),
            (newer_body, Path(newer["artifact_file"])),
            (anchor_body, Path(anchor["artifact_file"])),
            (linked_newer_body, Path(linked_newer["artifact_file"])),
        ):
            metadata = dict(body.get("metadata") or {})
            metadata["lifecycle_status"] = "active"
            metadata.pop("superseded_at", None)
            metadata.pop("superseded_by_artifact_id", None)
            metadata.pop("generation_key", None)
            body["metadata"] = metadata
            file_path.write_text(json.dumps(body, indent=2), encoding="utf-8")

        result = normalize_derived_artifact_lifecycle(self.paths, {"dry_run": False})
        self.assertGreaterEqual(result["changed_artifact_count"], 1)

        older_body = json.loads(Path(older["artifact_file"]).read_text(encoding="utf-8"))
        newer_body = json.loads(Path(newer["artifact_file"]).read_text(encoding="utf-8"))
        anchor_body = json.loads(Path(anchor["artifact_file"]).read_text(encoding="utf-8"))
        linked_newer_body = json.loads(Path(linked_newer["artifact_file"]).read_text(encoding="utf-8"))
        self.assertEqual(newer_body["metadata"]["lifecycle_status"], "active")
        self.assertEqual(older_body["metadata"]["lifecycle_status"], "superseded")
        self.assertEqual(older_body["metadata"]["superseded_by_artifact_id"], newer["artifact_id"])
        self.assertEqual(linked_newer_body["metadata"]["lifecycle_status"], "active")
        self.assertEqual(anchor_body["metadata"]["lifecycle_status"], "superseded")
        self.assertEqual(anchor_body["metadata"]["superseded_by_artifact_id"], linked_newer["artifact_id"])

    def test_normalize_derived_artifact_lifecycle_dry_run_does_not_mutate(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "author_role": "mixed",
                "content": "entry",
                "created_at": "2026-05-23T11:00:00+00:00",
            },
        )
        older = attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "conversation-brief",
                "producer": "conversation-brief.v1",
                "content": "older",
                "created_at": "2026-05-23T11:01:00+00:00",
                "metadata": {"source_entry_id": entry["entry_id"], "lifecycle_status": "active"},
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "conversation-brief",
                "producer": "conversation-brief.v1",
                "content": "newer",
                "created_at": "2026-05-23T11:02:00+00:00",
                "metadata": {"source_entry_id": entry["entry_id"], "lifecycle_status": "active"},
            },
        )

        older_body = json.loads(Path(older["artifact_file"]).read_text(encoding="utf-8"))
        metadata = dict(older_body.get("metadata") or {})
        metadata["lifecycle_status"] = "active"
        metadata.pop("superseded_at", None)
        metadata.pop("superseded_by_artifact_id", None)
        metadata.pop("generation_key", None)
        older_body["metadata"] = metadata
        Path(older["artifact_file"]).write_text(json.dumps(older_body, indent=2), encoding="utf-8")

        before = Path(older["artifact_file"]).read_text(encoding="utf-8")
        out = normalize_derived_artifact_lifecycle(self.paths, {"dry_run": True})
        after = Path(older["artifact_file"]).read_text(encoding="utf-8")
        self.assertGreaterEqual(out["changed_artifact_count"], 1)
        self.assertEqual(before, after)

    def test_normalize_derived_artifact_lifecycle_is_idempotent(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "author_role": "mixed",
                "content": "entry",
                "created_at": "2026-05-23T12:00:00+00:00",
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "compressed-memory",
                "producer": "compressed-memory.v2",
                "content": "older",
                "created_at": "2026-05-23T12:01:00+00:00",
                "metadata": {"source_entry_id": entry["entry_id"], "lifecycle_status": "active"},
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "compressed-memory",
                "producer": "compressed-memory.v2",
                "content": "newer",
                "created_at": "2026-05-23T12:02:00+00:00",
                "metadata": {"source_entry_id": entry["entry_id"], "lifecycle_status": "active"},
            },
        )

        for path in (self.paths.artifacts_dir / entry["entry_id"]).glob("artifact_*.json"):
            body = json.loads(path.read_text(encoding="utf-8"))
            metadata = dict(body.get("metadata") or {})
            metadata["lifecycle_status"] = "active"
            metadata.pop("superseded_at", None)
            metadata.pop("superseded_by_artifact_id", None)
            metadata.pop("generation_key", None)
            body["metadata"] = metadata
            path.write_text(json.dumps(body, indent=2), encoding="utf-8")

        first = normalize_derived_artifact_lifecycle(self.paths, {"dry_run": False})
        second = normalize_derived_artifact_lifecycle(self.paths, {"dry_run": False})
        self.assertGreaterEqual(first["changed_artifact_count"], 1)
        self.assertEqual(second["changed_artifact_count"], 0)

    def test_normalize_derived_artifact_lifecycle_does_not_touch_raw_entries(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "raw truth must stay unchanged",
                "created_at": "2026-05-23T13:00:00+00:00",
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "conversation-brief",
                "producer": "conversation-brief.v1",
                "content": "older",
                "created_at": "2026-05-23T13:01:00+00:00",
                "metadata": {"source_entry_id": entry["entry_id"], "lifecycle_status": "active"},
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "conversation-brief",
                "producer": "conversation-brief.v1",
                "content": "newer",
                "created_at": "2026-05-23T13:02:00+00:00",
                "metadata": {"source_entry_id": entry["entry_id"], "lifecycle_status": "active"},
            },
        )

        raw_file = Path(fetch_raw_entry(self.paths, {"entry_id": entry["entry_id"]})["entry_file"])
        before = raw_file.read_text(encoding="utf-8")
        normalize_derived_artifact_lifecycle(self.paths, {"dry_run": False})
        after = raw_file.read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_fetch_entry_detail_includes_open_loop_payload_for_analysis_artifact(self) -> None:
        e1 = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "TODO: follow up on budget question.",
                "created_at": "2026-05-25T09:00:00+00:00",
            },
        )
        append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "Pending decision on budget timeline.",
                "created_at": "2026-05-25T10:00:00+00:00",
            },
        )
        produced = produce_open_loops(self.paths, {"limit": 5})
        anchor_entry_id = produced["source_entry_ids"][0]
        detail = fetch_entry_detail(self.paths, {"entry_id": anchor_entry_id})
        open_loop_artifacts = [a for a in detail["artifacts"] if a["artifact_type"] == "analysis:open-loop"]
        self.assertGreaterEqual(len(open_loop_artifacts), 1)
        self.assertIn("open_loops", open_loop_artifacts[0])
        self.assertIsInstance(open_loop_artifacts[0]["open_loops"], list)
        self.assertEqual(open_loop_artifacts[0]["lineage"]["link_mode"], "direct")

    def test_fetch_entry_detail_surfaces_lineage_linked_open_loop_for_non_anchor_entry(self) -> None:
        e1 = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "Pending question: do we have final budget?",
                "created_at": "2026-05-25T09:00:00+00:00",
            },
        )
        e2 = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "TODO follow-up tomorrow about budget approval.",
                "created_at": "2026-05-25T10:00:00+00:00",
            },
        )

        produced = produce_open_loops(self.paths, {"entry_ids": [e1["entry_id"], e2["entry_id"]], "limit": 10})
        anchor_entry_id = produced["source_entry_ids"][0]
        self.assertEqual(anchor_entry_id, e2["entry_id"])

        detail = fetch_entry_detail(self.paths, {"entry_id": e1["entry_id"]})
        open_loop_artifacts = [a for a in detail["artifacts"] if a["artifact_type"] == "analysis:open-loop"]
        self.assertGreaterEqual(len(open_loop_artifacts), 1)
        linked = [a for a in open_loop_artifacts if a["lineage"]["link_mode"] == "lineage"][0]
        self.assertEqual(linked["lineage"]["anchor_entry_id"], e2["entry_id"])
        self.assertIn(e1["entry_id"], linked["lineage"]["source_entry_ids"])
        self.assertIn(e2["entry_id"], linked["lineage"]["source_entry_ids"])
        self.assertIn("open_loops", linked)
        self.assertIsInstance(linked["open_loops"], list)

    def test_fetch_entry_detail_non_anchor_lineage_open_loops_marks_latest_current(self) -> None:
        supporting = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "Supporting entry for lineage-linked open-loop analysis.",
                "created_at": "2026-05-25T09:00:00+00:00",
            },
        )
        anchor = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "Anchor entry for open-loop analysis runs.",
                "created_at": "2026-05-25T10:00:00+00:00",
            },
        )

        attach_artifact(
            self.paths,
            {
                "entry_id": anchor["entry_id"],
                "artifact_type": "analysis:open-loop",
                "producer": "open-loop.v1",
                "content": json.dumps(
                    {
                        "loops": [
                            {"title": "Older unresolved concern", "supporting_entry_ids": [supporting["entry_id"]]},
                            {"title": "Older unresolved concern B", "supporting_entry_ids": [supporting["entry_id"]]},
                        ]
                    }
                ),
                "created_at": "2026-05-25T10:05:00+00:00",
                "metadata": {"source_entry_ids": [supporting["entry_id"], anchor["entry_id"]]},
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": anchor["entry_id"],
                "artifact_type": "analysis:open-loop",
                "producer": "open-loop.v1",
                "content": json.dumps(
                    {
                        "loops": [
                            {"title": "Current unresolved concern", "supporting_entry_ids": [supporting["entry_id"]]},
                        ]
                    }
                ),
                "created_at": "2026-05-25T10:06:00+00:00",
                "metadata": {"source_entry_ids": [supporting["entry_id"], anchor["entry_id"]]},
            },
        )

        detail = fetch_entry_detail(self.paths, {"entry_id": supporting["entry_id"]})
        linked_open_loops = [
            a
            for a in detail["artifacts"]
            if a["artifact_type"] == "analysis:open-loop" and a["lineage"]["link_mode"] == "lineage"
        ]
        self.assertEqual(len(linked_open_loops), 2)
        self.assertEqual(
            [a["created_at"] for a in linked_open_loops],
            sorted([a["created_at"] for a in linked_open_loops], reverse=True),
        )
        self.assertEqual(sum(1 for a in linked_open_loops if a.get("is_current")), 1)
        current = [a for a in linked_open_loops if a.get("is_current")][0]
        self.assertEqual(current["created_at"], "2026-05-25T10:06:00+00:00")
        self.assertEqual(current["open_loops"][0]["title"], "Current unresolved concern")

        listed = list_entries(self.paths, {"limit": 10, "offset": 0})
        row = {item["entry_id"]: item for item in listed["items"]}[supporting["entry_id"]]
        self.assertEqual(row["open_loop"]["count"], len(current["open_loops"]))
        self.assertEqual(row["open_loop"]["representative_title"], current["open_loops"][0]["title"])
        self.assertEqual(row["open_loop"]["last_seen_at"], current["created_at"])

    def test_produce_conversation_briefs_attaches_brief_and_list_entries_surfaces_it(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "author_role": "mixed",
                "content": (
                    "SampleUser: can you check whether the browser node is back?\n"
                    "Assistant: yes, I’ll inspect the Mac-side route now."
                ),
                "created_at": "2026-05-23T12:00:00+00:00",
            },
        )
        produced = produce_conversation_briefs(self.paths, {"entry_ids": [entry["entry_id"]], "limit": 5})
        self.assertEqual(produced["produced_count"], 1)

        listed = list_entries(self.paths, {"limit": 5, "offset": 0})
        item = [item for item in listed["items"] if item["entry_id"] == entry["entry_id"]][0]
        self.assertTrue(item["brief"])
        self.assertIn("User", item["brief"])

    def test_produce_conversation_briefs_skips_when_active_artifact_exists(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "author_role": "mixed",
                "content": "SampleUser: status update?\nAssistant: all good.",
                "created_at": "2026-05-23T12:10:00+00:00",
            },
        )
        first = produce_conversation_briefs(self.paths, {"entry_ids": [entry["entry_id"]], "limit": 5})
        self.assertEqual(first["produced_count"], 1)
        second = produce_conversation_briefs(self.paths, {"entry_ids": [entry["entry_id"]], "limit": 5})
        self.assertEqual(second["produced_count"], 0)
        self.assertEqual(second["skipped"], [entry["entry_id"]])

    def test_produce_conversation_briefs_regenerates_when_only_superseded_exists(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "author_role": "mixed",
                "content": "SampleUser: status update?\nAssistant: all good.",
                "created_at": "2026-05-23T12:20:00+00:00",
            },
        )
        attached = attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "conversation-brief",
                "producer": "conversation-brief.v1",
                "content": "old brief",
                "created_at": "2026-05-23T12:21:00+00:00",
                "metadata": {"source_entry_id": entry["entry_id"]},
            },
        )
        body = json.loads(Path(attached["artifact_file"]).read_text(encoding="utf-8"))
        metadata = dict(body.get("metadata") or {})
        metadata["lifecycle_status"] = "superseded"
        metadata["superseded_at"] = "2026-05-23T12:22:00+00:00"
        metadata["superseded_by_artifact_id"] = "artifact_newer"
        body["metadata"] = metadata
        Path(attached["artifact_file"]).write_text(json.dumps(body, indent=2), encoding="utf-8")

        out = produce_conversation_briefs(self.paths, {"entry_ids": [entry["entry_id"]], "limit": 5})
        self.assertEqual(out["produced_count"], 1)
        self.assertEqual(out["skipped_count"], 0)

    def test_produce_compressed_memory_legacy_artifact_without_lifecycle_still_skips(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "author_role": "mixed",
                "content": "SampleUser: check browser node\nAssistant: checking.",
                "created_at": "2026-05-23T12:30:00+00:00",
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "compressed-memory",
                "producer": "compressed-memory.v2",
                "content": "legacy memory body",
                "created_at": "2026-05-23T12:31:00+00:00",
                "metadata": {},
            },
        )
        # Simulate legacy artifact with no lifecycle metadata.
        artifact_files = sorted((self.paths.artifacts_dir / entry["entry_id"]).glob("*.json"))
        self.assertEqual(len(artifact_files), 1)
        body = json.loads(artifact_files[0].read_text(encoding="utf-8"))
        metadata = dict(body.get("metadata") or {})
        metadata.pop("lifecycle_status", None)
        metadata.pop("generation_key", None)
        body["metadata"] = metadata
        artifact_files[0].write_text(json.dumps(body, indent=2), encoding="utf-8")

        out = produce_compressed_memory(self.paths, {"entry_ids": [entry["entry_id"]], "limit": 5})
        self.assertEqual(out["produced_count"], 0)
        self.assertEqual(out["skipped"], [entry["entry_id"]])

    def test_produce_compressed_memory_force_generates_even_with_active_artifact(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "author_role": "mixed",
                "content": "SampleUser: check browser node\nAssistant: checking.",
                "created_at": "2026-05-23T12:40:00+00:00",
            },
        )
        first = produce_compressed_memory(self.paths, {"entry_ids": [entry["entry_id"]], "limit": 5})
        self.assertEqual(first["produced_count"], 1)
        second = produce_compressed_memory(self.paths, {"entry_ids": [entry["entry_id"]], "limit": 5, "force": True})
        self.assertEqual(second["produced_count"], 1)

    def test_produce_compressed_memory_indexes_artifact_and_search_hits_compressed_layer(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "author_role": "mixed",
                "content": (
                    "SampleUser: what happened on may 7th?\n"
                    "Assistant: I checked the May 7 entry and found the microcontractor and reMarkable discussion."
                ),
                "created_at": "2026-05-23T12:00:00+00:00",
            },
        )
        produced = produce_compressed_memory(self.paths, {"entry_ids": [entry["entry_id"]], "limit": 5})
        self.assertEqual(produced["produced_count"], 1)
        self.assertTrue(produced["produced"][0]["indexed_in_memory"])

        results = search_memory(self.paths, {"query": "May 7 microcontractor", "limit": 5})
        self.assertGreaterEqual(results["match_summary"]["compressed_memory_hits"], 1)
        self.assertTrue(results["match_summary"]["using_raw_layer"])
        self.assertEqual(results["matches"][0]["entry_id"], entry["entry_id"])
        self.assertIn(results["matches"][0]["match_layer"], {"compressed_memory", "raw_entry_fallback"})
        self.assertIn("compressed_memory", results["matches"][0]["supporting_layers"])

    def test_produce_compressed_memory_representative_transcript_improves_search_handles(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "author_role": "mixed",
                "content": (
                    "SampleUser: can you check whether the browser node is back?\n"
                    "Assistant: yes, I’ll inspect the Mac-side route now.\n"
                    "SampleUser: great. let me know if the browser node tunnel works."
                ),
                "created_at": "2026-05-23T12:05:00+00:00",
            },
        )
        produced = produce_compressed_memory(self.paths, {"entry_ids": [entry["entry_id"]], "limit": 5})
        self.assertEqual(produced["produced_count"], 1)
        self.assertTrue(produced["produced"][0]["indexed_in_memory"])

        results = search_memory(self.paths, {"query": "mac-side route", "limit": 5})
        self.assertGreaterEqual(results["match_summary"]["compressed_memory_hits"], 1)
        self.assertGreaterEqual(results["match_summary"]["raw_entry_hits"], 1)
        self.assertTrue(results["match_summary"]["using_raw_layer"])
        self.assertEqual(results["matches"][0]["entry_id"], entry["entry_id"])
        self.assertIn(results["matches"][0]["match_layer"], {"compressed_memory", "raw_entry_fallback"})
        self.assertIn("compressed_memory", results["matches"][0]["supporting_layers"])
        self.assertIn("mac-side route", results["matches"][0]["match_text"].lower())

    def test_produce_compressed_memory_search_portable_with_non_user_assistant_names(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "author_role": "mixed",
                "content": (
                    "Alex: can you check whether the browser node is back?\n"
                    "Codex: yes, I’ll inspect the Mac-side route now.\n"
                    "Alex: great. let me know if the node tunnel still works."
                ),
                "created_at": "2026-05-23T12:10:00+00:00",
            },
        )
        produced = produce_compressed_memory(self.paths, {"entry_ids": [entry["entry_id"]], "limit": 5})
        self.assertEqual(produced["produced_count"], 1)
        self.assertTrue(produced["produced"][0]["indexed_in_memory"])

        results = search_memory(self.paths, {"query": "mac-side route", "limit": 5})
        self.assertGreaterEqual(results["match_summary"]["compressed_memory_hits"], 1)
        self.assertGreaterEqual(results["match_summary"]["raw_entry_hits"], 1)
        self.assertTrue(results["match_summary"]["using_raw_layer"])
        self.assertEqual(results["matches"][0]["entry_id"], entry["entry_id"])
        self.assertIn(results["matches"][0]["match_layer"], {"compressed_memory", "raw_entry_fallback"})
        self.assertIn("compressed_memory", results["matches"][0]["supporting_layers"])
        self.assertIn("mac-side route", results["matches"][0]["match_text"].lower())

        detail = fetch_entry_detail(self.paths, {"entry_id": entry["entry_id"]})
        compressed = [a for a in detail["artifacts"] if a["artifact_type"] == "compressed-memory"][0]["content"]
        self.assertIn("User asks:", compressed)
        self.assertIn("Assistant commitments:", compressed)

    def test_produce_open_loops_emits_analysis_artifact(self) -> None:
        append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "TODO: follow up with customer on quote timeline.",
                "created_at": "2026-05-24T09:00:00+00:00",
            },
        )
        append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "Still pending: should send project update by Friday.",
                "created_at": "2026-05-24T10:00:00+00:00",
            },
        )

        produced = produce_open_loops(self.paths, {"limit": 10})
        self.assertGreaterEqual(produced["loop_count"], 1)

    def test_produce_open_loops_detects_overlay_added_unresolved_item(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "Status note without explicit open-loop markers.",
                "created_at": "2026-05-24T09:15:00+00:00",
            },
        )
        append_overlay(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "overlay_type": "correction",
                "author": "operator",
                "content": "TODO: follow up with customer on contract signature timing.",
            },
        )

        produced = produce_open_loops(self.paths, {"entry_ids": [entry["entry_id"]], "limit": 5})
        self.assertGreaterEqual(produced["loop_count"], 1)

    def test_produce_open_loops_with_overlay_does_not_mutate_raw_content(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "cli",
                "author_role": "mixed",
                "content": "SampleUser: capture baseline record only.",
                "created_at": "2026-05-24T09:20:00+00:00",
            },
        )
        append_overlay(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "overlay_type": "annotation",
                "author": "operator",
                "content": "Pending question: who owns follow-up next step?",
            },
        )
        before = fetch_raw_entry(self.paths, {"entry_id": entry["entry_id"]})["entry"]["content"]
        produce_open_loops(self.paths, {"entry_ids": [entry["entry_id"]], "limit": 5})
        after = fetch_raw_entry(self.paths, {"entry_id": entry["entry_id"]})["entry"]["content"]
        self.assertEqual(before, after)

    def test_produce_open_loops_overlay_path_preserves_lineage_source_entry_ids(self) -> None:
        e1 = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "cli",
                "author_role": "mixed",
                "content": "Baseline entry one without clear unresolved marker.",
                "created_at": "2026-05-24T09:25:00+00:00",
            },
        )
        e2 = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "cli",
                "author_role": "mixed",
                "content": "Baseline entry two with neutral text.",
                "created_at": "2026-05-24T09:26:00+00:00",
            },
        )
        append_overlay(
            self.paths,
            {
                "entry_id": e2["entry_id"],
                "overlay_type": "annotation",
                "author": "operator",
                "content": "TODO: pending item remains for tomorrow follow-up.",
            },
        )

        produced = produce_open_loops(self.paths, {"entry_ids": [e1["entry_id"], e2["entry_id"]], "limit": 10})
        self.assertGreaterEqual(produced["loop_count"], 1)
        artifact_file = Path(produced["artifact_file"])
        body = json.loads(artifact_file.read_text(encoding="utf-8"))
        metadata = body.get("metadata", {})
        self.assertEqual(sorted(metadata.get("source_entry_ids", [])), sorted([e1["entry_id"], e2["entry_id"]]))
        artifact_file = Path(produced["artifact_file"])
        self.assertTrue(artifact_file.exists())

        artifact_body = json.loads(artifact_file.read_text(encoding="utf-8"))
        self.assertEqual(artifact_body["artifact_type"], "analysis:open-loop")
        content = json.loads(artifact_body["content"])
        self.assertIn("loops", content)
        self.assertGreaterEqual(len(content["loops"]), 1)

    def test_produce_open_loops_preserves_lineage_source_entry_ids(self) -> None:
        e1 = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "Pending question: do we have final budget?",
                "created_at": "2026-05-24T11:00:00+00:00",
            },
        )
        e2 = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "TODO follow-up tomorrow about budget approval.",
                "created_at": "2026-05-24T12:00:00+00:00",
            },
        )

        produced = produce_open_loops(self.paths, {"entry_ids": [e1["entry_id"], e2["entry_id"]], "limit": 10})
        artifact_file = Path(produced["artifact_file"])
        artifact_body = json.loads(artifact_file.read_text(encoding="utf-8"))
        metadata = artifact_body["metadata"]
        self.assertEqual(sorted(metadata["source_entry_ids"]), sorted([e1["entry_id"], e2["entry_id"]]))
        loops = json.loads(artifact_body["content"])["loops"]
        self.assertTrue(any(e1["entry_id"] in loop["supporting_entry_ids"] or e2["entry_id"] in loop["supporting_entry_ids"] for loop in loops))

    def test_list_entries_surfaces_open_loop_participation_for_anchor_and_lineage(self) -> None:
        e1 = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "Pending question: do we have final budget?",
                "created_at": "2026-05-24T11:00:00+00:00",
            },
        )
        e2 = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "TODO follow-up tomorrow about budget approval.",
                "created_at": "2026-05-24T12:00:00+00:00",
            },
        )
        clean = append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "This entry is unrelated to open loops.",
                "created_at": "2026-05-24T13:00:00+00:00",
            },
        )
        produce_open_loops(self.paths, {"entry_ids": [e1["entry_id"], e2["entry_id"]], "limit": 10})

        listed = list_entries(self.paths, {"limit": 10, "offset": 0})
        by_id = {item["entry_id"]: item for item in listed["items"]}

        self.assertTrue(by_id[e1["entry_id"]]["open_loop"]["has_open_loops"])
        self.assertGreaterEqual(by_id[e1["entry_id"]]["open_loop"]["count"], 1)
        self.assertTrue(by_id[e1["entry_id"]]["open_loop"]["representative_title"])
        self.assertTrue(by_id[e1["entry_id"]]["open_loop"]["last_seen_at"])
        self.assertTrue(by_id[e2["entry_id"]]["open_loop"]["has_open_loops"])
        self.assertGreaterEqual(by_id[e2["entry_id"]]["open_loop"]["count"], 1)
        self.assertTrue(by_id[e2["entry_id"]]["open_loop"]["representative_title"])
        self.assertTrue(by_id[e2["entry_id"]]["open_loop"]["last_seen_at"])
        self.assertNotIn("open_loop", by_id[clean["entry_id"]])

        listed_filtered = list_entries(self.paths, {"limit": 10, "offset": 0, "only_with_open_loops": True})
        filtered_ids = {item["entry_id"] for item in listed_filtered["items"]}
        self.assertIn(e1["entry_id"], filtered_ids)
        self.assertIn(e2["entry_id"], filtered_ids)
        self.assertNotIn(clean["entry_id"], filtered_ids)

    def test_list_entries_open_loop_metadata_uses_latest_analysis_not_artifact_history(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "Open-loop participant entry.",
                "created_at": "2026-05-24T11:00:00+00:00",
            },
        )

        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "analysis:open-loop",
                "producer": "open-loop.v1",
                "content": json.dumps(
                    {
                        "loops": [
                            {"title": "Older unresolved concern A", "supporting_entry_ids": [entry["entry_id"]]},
                            {"title": "Older unresolved concern B", "supporting_entry_ids": [entry["entry_id"]]},
                        ]
                    }
                ),
                "created_at": "2026-05-24T11:05:00+00:00",
                "metadata": {"source_entry_ids": [entry["entry_id"]]},
            },
        )
        attach_artifact(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "artifact_type": "analysis:open-loop",
                "producer": "open-loop.v1",
                "content": json.dumps(
                    {
                        "loops": [
                            {"title": "Current unresolved concern", "supporting_entry_ids": [entry["entry_id"]]},
                        ]
                    }
                ),
                "created_at": "2026-05-24T11:06:00+00:00",
                "metadata": {"source_entry_ids": [entry["entry_id"]]},
            },
        )

        listed = list_entries(self.paths, {"limit": 10, "offset": 0})
        by_id = {item["entry_id"]: item for item in listed["items"]}
        open_loop = by_id[entry["entry_id"]]["open_loop"]
        self.assertTrue(open_loop["has_open_loops"])
        self.assertEqual(open_loop["count"], 1)
        self.assertEqual(open_loop["representative_title"], "Current unresolved concern")
        self.assertEqual(open_loop["last_seen_at"], "2026-05-24T11:06:00+00:00")

    def test_list_entries_only_with_open_loops_applies_limit_offset_after_filtering(self) -> None:
        e1 = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "Pending question: budget status?",
                "created_at": "2026-05-24T11:00:00+00:00",
            },
        )
        e2 = append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "Unrelated entry between participating rows.",
                "created_at": "2026-05-24T11:30:00+00:00",
            },
        )
        e3 = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "TODO follow-up with customer on budget approval.",
                "created_at": "2026-05-24T12:00:00+00:00",
            },
        )
        e4 = append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "Another unrelated entry.",
                "created_at": "2026-05-24T12:30:00+00:00",
            },
        )
        e5 = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "Pending decision: approve budget timeline.",
                "created_at": "2026-05-24T13:00:00+00:00",
            },
        )
        produce_open_loops(self.paths, {"entry_ids": [e1["entry_id"], e3["entry_id"], e5["entry_id"]], "limit": 10})

        unfiltered = list_entries(self.paths, {"limit": 10, "offset": 0})
        unfiltered_ids = {item["entry_id"] for item in unfiltered["items"]}
        self.assertIn(e2["entry_id"], unfiltered_ids)
        self.assertIn(e4["entry_id"], unfiltered_ids)

        filtered_all = list_entries(self.paths, {"limit": 10, "offset": 0, "only_with_open_loops": True})
        filtered_ids = [item["entry_id"] for item in filtered_all["items"]]
        self.assertEqual(filtered_ids, [e5["entry_id"], e3["entry_id"], e1["entry_id"]])

        filtered_page_1 = list_entries(self.paths, {"limit": 1, "offset": 0, "only_with_open_loops": True})
        filtered_page_2 = list_entries(self.paths, {"limit": 1, "offset": 1, "only_with_open_loops": True})
        filtered_page_3 = list_entries(self.paths, {"limit": 1, "offset": 2, "only_with_open_loops": True})
        self.assertEqual([item["entry_id"] for item in filtered_page_1["items"]], [e5["entry_id"]])
        self.assertEqual([item["entry_id"] for item in filtered_page_2["items"]], [e3["entry_id"]])
        self.assertEqual([item["entry_id"] for item in filtered_page_3["items"]], [e1["entry_id"]])

    def test_list_entries_only_with_open_loops_orders_by_last_seen_at_desc(self) -> None:
        older_entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "Pending budget approval follow-up.",
                "created_at": "2026-05-24T09:00:00+00:00",
            },
        )
        newer_entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "Pending staffing decision.",
                "created_at": "2026-05-24T11:00:00+00:00",
            },
        )

        # First run creates an older open-loop artifact for newer_entry.
        produce_open_loops(self.paths, {"entry_ids": [newer_entry["entry_id"]], "limit": 5})
        # Second run creates a newer open-loop artifact for older_entry.
        produce_open_loops(self.paths, {"entry_ids": [older_entry["entry_id"]], "limit": 5})

        unfiltered = list_entries(self.paths, {"limit": 10, "offset": 0})
        unfiltered_ids = [item["entry_id"] for item in unfiltered["items"]]
        self.assertLess(unfiltered_ids.index(newer_entry["entry_id"]), unfiltered_ids.index(older_entry["entry_id"]))

        filtered = list_entries(self.paths, {"limit": 10, "offset": 0, "only_with_open_loops": True})
        filtered_ids = [item["entry_id"] for item in filtered["items"]]
        self.assertEqual(filtered_ids[0], older_entry["entry_id"])
        self.assertEqual(filtered_ids[1], newer_entry["entry_id"])
        self.assertGreaterEqual(
            filtered["items"][0]["open_loop"]["last_seen_at"],
            filtered["items"][1]["open_loop"]["last_seen_at"],
        )

    def test_produce_open_loops_does_not_mutate_raw_entries(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "TODO: unresolved user question.",
                "created_at": "2026-05-24T13:00:00+00:00",
            },
        )
        before = fetch_raw_entry(self.paths, {"entry_id": entry["entry_id"]})["entry"]
        produce_open_loops(self.paths, {"entry_ids": [entry["entry_id"]], "limit": 5})
        after = fetch_raw_entry(self.paths, {"entry_id": entry["entry_id"]})["entry"]
        self.assertEqual(before, after)

    def test_produce_open_loops_suppresses_stray_question_false_positive(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "What a day? Great weather and coffee.",
                "created_at": "2026-05-24T14:00:00+00:00",
            },
        )
        produced = produce_open_loops(self.paths, {"entry_ids": [entry["entry_id"]], "limit": 5})
        artifact_file = Path(produced["artifact_file"])
        loops = json.loads(json.loads(artifact_file.read_text(encoding="utf-8"))["content"])["loops"]
        self.assertEqual(len(loops), 0)

    def test_produce_open_loops_does_not_emit_clearly_closed_language(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "openclaw",
                "author_role": "agent",
                "content": "TODO: finalize deployment checklist. This is done and resolved.",
                "created_at": "2026-05-24T15:00:00+00:00",
            },
        )
        produced = produce_open_loops(self.paths, {"entry_ids": [entry["entry_id"]], "limit": 5})
        artifact_file = Path(produced["artifact_file"])
        loops = json.loads(json.loads(artifact_file.read_text(encoding="utf-8"))["content"])["loops"]
        self.assertEqual(len(loops), 0)

    def test_produce_open_loops_detects_conversational_check_request(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "author_role": "mixed",
                "content": (
                    "SampleUser: can you check whether the browser node is back?\n"
                    "Assistant: yes, I’ll inspect the Mac-side route now.\n"
                    "SampleUser: great. let me know if the tunnel still works."
                ),
                "created_at": "2026-05-24T16:00:00+00:00",
            },
        )
        produced = produce_open_loops(self.paths, {"entry_ids": [entry["entry_id"]], "limit": 5})
        artifact_file = Path(produced["artifact_file"])
        loops = json.loads(json.loads(artifact_file.read_text(encoding="utf-8"))["content"])["loops"]
        self.assertGreaterEqual(len(loops), 1)
        loop_text = json.dumps(loops).lower()
        self.assertIn("browser node", loop_text)

    def test_produce_open_loops_suppresses_addressed_request_keeps_unresolved_followup(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "author_role": "mixed",
                "content": (
                    "SampleUser: can you check whether the browser node is back?\n"
                    "Assistant: checked now, browser node is back and confirmed.\n"
                    "SampleUser: great, can you let me know if the tunnel still works?"
                ),
                "created_at": "2026-05-24T16:10:00+00:00",
            },
        )
        produced = produce_open_loops(self.paths, {"entry_ids": [entry["entry_id"]], "limit": 5})
        artifact_file = Path(produced["artifact_file"])
        loops = json.loads(json.loads(artifact_file.read_text(encoding="utf-8"))["content"])["loops"]
        self.assertGreaterEqual(len(loops), 1)
        loop_text = json.dumps(loops).lower()
        self.assertIn("tunnel still works", loop_text)
        self.assertNotIn("can you check whether the browser node is back", loop_text)

    def test_produce_open_loops_portable_with_non_user_assistant_names(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "telegram-direct-transcript",
                "author_role": "mixed",
                "content": (
                    "Alex: can you check whether the browser node is back?\n"
                    "Codex: checked now, browser node is back and confirmed.\n"
                    "Alex: great, can you let me know if the tunnel still works?"
                ),
                "created_at": "2026-05-24T16:20:00+00:00",
            },
        )
        produced = produce_open_loops(self.paths, {"entry_ids": [entry["entry_id"]], "limit": 5})
        artifact_file = Path(produced["artifact_file"])
        loops = json.loads(json.loads(artifact_file.read_text(encoding="utf-8"))["content"])["loops"]
        self.assertGreaterEqual(len(loops), 1)
        loop_text = json.dumps(loops).lower()
        self.assertIn("tunnel still works", loop_text)
        self.assertNotIn("can you check whether the browser node is back", loop_text)

    def test_import_session_jsonl_writes_manifest_and_truthful_ingestion_metadata(self) -> None:
        source_file = self.root / "session.jsonl"
        source_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "entry_type": "chat_log",
                            "source": "telegram-direct-import",
                            "author_role": "human",
                            "created_at": "2026-05-25T12:00:00+00:00",
                            "content": "We should preserve the raw record.",
                            "metadata": {"source_message_id": "m1"},
                        }
                    ),
                    json.dumps(
                        {
                            "entry_type": "chat_log",
                            "source": "telegram-direct-import",
                            "author_role": "agent",
                            "created_at": "2026-05-25T12:01:00+00:00",
                            "content": "Agreed. Let's make imports idempotent.",
                            "metadata": {"source_message_id": "m2"},
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )

        result = import_session_jsonl(
            self.paths,
            {
                "path": str(source_file),
                "import_id": "import_test_truthful",
                "source_session_id": "session-123",
                "source_conversation_id": "telegram:123456789",
            },
        )

        self.assertEqual(result["imported_count"], 2)
        self.assertEqual(result["skipped_count"], 0)
        manifest_path = Path(result["manifest_path"])
        self.assertTrue(manifest_path.exists())
        ledger_path = Path(result["ledger_path"])
        self.assertTrue(ledger_path.exists())

        entry_id = result["imported"][0]["entry_id"]
        fetched = fetch_raw_entry(self.paths, {"entry_id": entry_id})
        ingestion = fetched["entry"]["metadata"]["ingestion"]
        self.assertTrue(ingestion["truthful_source"])
        self.assertEqual(ingestion["import_mode"], "session_jsonl")
        self.assertEqual(ingestion["import_id"], "import_test_truthful")
        self.assertEqual(ingestion["source_session_id"], "session-123")
        self.assertEqual(ingestion["source_conversation_id"], "telegram:123456789")

        conn = sqlite3.connect(self.paths.sqlite_path)
        try:
            indexed = conn.execute(
                """
                SELECT entry_type, source_session_id, source_conversation_id, import_id, truthful_source
                FROM entries
                WHERE entry_id = ?
                """,
                (entry_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(indexed, ("chat_log", "session-123", "telegram:123456789", "import_test_truthful", 1))

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["audit"]["line_count"], 2)
        self.assertEqual(manifest["audit"]["entry_type_counts"], {"chat_log": 2})
        self.assertEqual(manifest["audit"]["author_role_counts"], {"agent": 1, "human": 1})
        self.assertEqual(manifest["audit"]["source_counts"], {"telegram-direct-import": 2})
        self.assertEqual(
            manifest["audit"]["created_at_range"],
            {"first": "2026-05-25T12:00:00+00:00", "last": "2026-05-25T12:01:00+00:00"},
        )

    def test_import_session_jsonl_rejects_path_like_import_id_before_writes(self) -> None:
        source_file = self.root / "unsafe-import-id.jsonl"
        source_file.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "human",
                    "created_at": "2026-05-25T12:30:00+00:00",
                    "content": "This should not be written under an unsafe manifest id.",
                    "metadata": {"source_message_id": "unsafe-import-id-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(ValueError):
            import_session_jsonl(self.paths, {"path": str(source_file), "import_id": "../ledger"})

        self.assertEqual(list(self.paths.entries_dir.glob("**/*.json")), [])
        self.assertFalse((self.paths.imports_dir / "ledger.json").exists())

    def test_import_session_jsonl_skips_duplicate_source_items_on_repeat_run(self) -> None:
        source_file = self.root / "repeat.jsonl"
        source_file.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "human",
                    "created_at": "2026-05-25T13:00:00+00:00",
                    "content": "Same message should not import twice.",
                    "metadata": {"source_message_id": "dup-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        first = import_session_jsonl(self.paths, {"path": str(source_file), "import_id": "import_dup_first"})
        second = import_session_jsonl(self.paths, {"path": str(source_file), "import_id": "import_dup_second"})

        self.assertEqual(first["imported_count"], 1)
        self.assertEqual(second["imported_count"], 0)
        self.assertEqual(second["skipped_count"], 1)
        self.assertEqual(second["skipped"][0]["reason"], "duplicate_source_item")
        self.assertEqual(second["audit"]["duplicate_source_item_count"], 1)
        self.assertEqual(second["audit"]["duplicate_existing_entry_ids"], [first["imported"][0]["entry_id"]])

    def test_list_entries_filters_by_source_conversation_id(self) -> None:
        source_file_a = self.root / "conversation-a.jsonl"
        source_file_a.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "human",
                    "created_at": "2026-05-25T13:10:00+00:00",
                    "content": "Conversation A message.",
                    "metadata": {"source_message_id": "conv-a-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        source_file_b = self.root / "conversation-b.jsonl"
        source_file_b.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "human",
                    "created_at": "2026-05-25T13:11:00+00:00",
                    "content": "Conversation B message.",
                    "metadata": {"source_message_id": "conv-b-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        import_session_jsonl(
            self.paths,
            {
                "path": str(source_file_a),
                "import_id": "import-conv-a",
                "source_session_id": "session-a",
                "source_conversation_id": "telegram:conv-a",
            },
        )
        import_session_jsonl(
            self.paths,
            {
                "path": str(source_file_b),
                "import_id": "import-conv-b",
                "source_session_id": "session-b",
                "source_conversation_id": "telegram:conv-b",
            },
        )
        append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "Manual entry should not match scoped import filters.",
                "created_at": "2026-05-25T13:12:00+00:00",
            },
        )

        out = list_entries(self.paths, {"limit": 10, "offset": 0, "source_conversation_id": "telegram:conv-a"})
        self.assertEqual(len(out["items"]), 1)
        self.assertEqual(out["items"][0]["provenance"]["source_conversation_id"], "telegram:conv-a")
        self.assertEqual(out["items"][0]["provenance"]["import_id"], "import-conv-a")

    def test_list_entries_filters_by_import_id(self) -> None:
        source_file = self.root / "import-id-filter.jsonl"
        source_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "entry_type": "chat_log",
                            "source": "telegram-direct-import",
                            "author_role": "human",
                            "created_at": "2026-05-25T13:20:00+00:00",
                            "content": "Import target one.",
                            "metadata": {"source_message_id": "import-filter-1"},
                        }
                    ),
                    json.dumps(
                        {
                            "entry_type": "chat_log",
                            "source": "telegram-direct-import",
                            "author_role": "agent",
                            "created_at": "2026-05-25T13:21:00+00:00",
                            "content": "Import target two.",
                            "metadata": {"source_message_id": "import-filter-2"},
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        import_session_jsonl(
            self.paths,
            {
                "path": str(source_file),
                "import_id": "import-filter-target",
                "source_session_id": "session-filter",
                "source_conversation_id": "telegram:filter",
            },
        )
        append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "Manual note outside import batch.",
                "created_at": "2026-05-25T13:22:00+00:00",
            },
        )

        out = list_entries(self.paths, {"limit": 10, "offset": 0, "import_id": "import-filter-target"})
        self.assertEqual(len(out["items"]), 2)
        self.assertEqual({item["provenance"]["import_id"] for item in out["items"]}, {"import-filter-target"})

    def test_list_entries_truthful_only_excludes_manual_entries(self) -> None:
        source_file = self.root / "truthful-only.jsonl"
        source_file.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "human",
                    "created_at": "2026-05-25T13:30:00+00:00",
                    "content": "Truthy import message.",
                    "metadata": {"source_message_id": "truthful-only-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        import_session_jsonl(
            self.paths,
            {
                "path": str(source_file),
                "import_id": "import-truthful-only",
                "source_session_id": "session-truthful",
                "source_conversation_id": "telegram:truthful",
            },
        )
        append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "Manual note should be excluded by truthful_only.",
                "created_at": "2026-05-25T13:31:00+00:00",
            },
        )

        out = list_entries(self.paths, {"limit": 10, "offset": 0, "truthful_only": True})
        self.assertEqual(len(out["items"]), 1)
        self.assertTrue(out["items"][0]["provenance"]["truthful_source"])
        self.assertEqual(out["items"][0]["provenance"]["import_id"], "import-truthful-only")

    def test_list_entries_unfiltered_remains_compatible_and_includes_provenance_hints(self) -> None:
        source_file = self.root / "unfiltered-provenance.jsonl"
        source_file.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "human",
                    "created_at": "2026-05-25T13:40:00+00:00",
                    "content": "Imported message for unfiltered list.",
                    "metadata": {"source_message_id": "unfiltered-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        import_session_jsonl(
            self.paths,
            {
                "path": str(source_file),
                "import_id": "import-unfiltered",
                "source_session_id": "session-unfiltered",
                "source_conversation_id": "telegram:unfiltered",
            },
        )
        append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "Manual unfiltered entry.",
                "created_at": "2026-05-25T13:41:00+00:00",
            },
        )

        out = list_entries(self.paths, {"limit": 10, "offset": 0})
        self.assertGreaterEqual(len(out["items"]), 2)
        by_source = {item["source"]: item for item in out["items"]}
        imported_item = by_source["telegram-direct-import"]
        manual_item = by_source["cli"]
        self.assertEqual(imported_item["provenance"]["import_id"], "import-unfiltered")
        self.assertEqual(imported_item["provenance"]["source_conversation_id"], "telegram:unfiltered")
        self.assertIsNone(manual_item["provenance"]["import_id"])
        self.assertFalse(manual_item["provenance"]["truthful_source"])

    def test_cli_list_entries_forwards_provenance_filters(self) -> None:
        source_file = self.root / "cli-list-entries-filtered.jsonl"
        source_file.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "human",
                    "created_at": "2026-05-25T13:50:00+00:00",
                    "content": "CLI scoped import message.",
                    "metadata": {"source_message_id": "cli-filter-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        import_session_jsonl(
            self.paths,
            {
                "path": str(source_file),
                "import_id": "import-cli-filter",
                "source_session_id": "session-cli-filter",
                "source_conversation_id": "telegram:cli-filter",
            },
        )
        append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "Manual note outside truthful import scope.",
                "created_at": "2026-05-25T13:51:00+00:00",
            },
        )

        cmd = [
            sys.executable,
            "-m",
            "agent_diary.cli.main",
            "--json",
            "list-entries",
            "--limit",
            "20",
            "--offset",
            "0",
            "--source-conversation-id",
            "telegram:cli-filter",
            "--import-id",
            "import-cli-filter",
            "--truthful-only",
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        completed = subprocess.run(
            cmd,
            cwd=self.root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        out = json.loads(completed.stdout)
        self.assertEqual(len(out["items"]), 1)
        item = out["items"][0]
        self.assertEqual(item["source"], "telegram-direct-import")
        self.assertEqual(item["provenance"]["source_conversation_id"], "telegram:cli-filter")
        self.assertEqual(item["provenance"]["import_id"], "import-cli-filter")
        self.assertTrue(item["provenance"]["truthful_source"])

    def test_produce_open_loops_scopes_by_source_conversation_id(self) -> None:
        source_file_a = self.root / "produce-loops-conv-a.jsonl"
        source_file_a.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "mixed",
                    "created_at": "2026-05-25T14:00:00+00:00",
                    "content": "SampleUser: can you check the browser route?\nAssistant: yes, checking now.",
                    "metadata": {"source_message_id": "loops-conv-a-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        source_file_b = self.root / "produce-loops-conv-b.jsonl"
        source_file_b.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "mixed",
                    "created_at": "2026-05-25T14:01:00+00:00",
                    "content": "SampleUser: this is another conversation scope.",
                    "metadata": {"source_message_id": "loops-conv-b-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        import_session_jsonl(
            self.paths,
            {
                "path": str(source_file_a),
                "import_id": "import-loops-conv-a",
                "source_session_id": "session-loops-a",
                "source_conversation_id": "telegram:loops-conv-a",
            },
        )
        import_session_jsonl(
            self.paths,
            {
                "path": str(source_file_b),
                "import_id": "import-loops-conv-b",
                "source_session_id": "session-loops-b",
                "source_conversation_id": "telegram:loops-conv-b",
            },
        )

        produced = produce_open_loops(
            self.paths,
            {"limit": 10, "source_conversation_id": "telegram:loops-conv-a"},
        )
        self.assertEqual(produced["selection_mode"], "provenance_scope")
        self.assertEqual(produced["filters"]["source_conversation_id"], "telegram:loops-conv-a")
        self.assertEqual(len(produced["source_entry_ids"]), 1)

        detail = fetch_entry_detail(self.paths, {"entry_id": produced["source_entry_ids"][0]})
        self.assertEqual(
            detail["raw_entry"]["metadata"]["ingestion"]["source_conversation_id"],
            "telegram:loops-conv-a",
        )

    def test_produce_compressed_memory_scopes_by_import_id(self) -> None:
        source_file_a = self.root / "produce-memory-import-a.jsonl"
        source_file_a.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "mixed",
                    "created_at": "2026-05-25T14:10:00+00:00",
                    "content": "SampleUser: note for import A scope.",
                    "metadata": {"source_message_id": "memory-import-a-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        source_file_b = self.root / "produce-memory-import-b.jsonl"
        source_file_b.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "mixed",
                    "created_at": "2026-05-25T14:11:00+00:00",
                    "content": "SampleUser: note for import B scope.",
                    "metadata": {"source_message_id": "memory-import-b-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        import_session_jsonl(
            self.paths,
            {
                "path": str(source_file_a),
                "import_id": "import-memory-target",
                "source_session_id": "session-memory-a",
                "source_conversation_id": "telegram:memory-a",
            },
        )
        import_session_jsonl(
            self.paths,
            {
                "path": str(source_file_b),
                "import_id": "import-memory-other",
                "source_session_id": "session-memory-b",
                "source_conversation_id": "telegram:memory-b",
            },
        )

        out = produce_compressed_memory(self.paths, {"limit": 10, "import_id": "import-memory-target"})
        self.assertEqual(out["selection_mode"], "provenance_scope")
        self.assertEqual(out["filters"]["import_id"], "import-memory-target")
        self.assertEqual(out["produced_count"], 1)
        self.assertEqual(out["skipped_count"], 0)
        entry_id = out["produced"][0]["entry_id"]
        detail = fetch_entry_detail(self.paths, {"entry_id": entry_id})
        self.assertEqual(detail["raw_entry"]["metadata"]["ingestion"]["import_id"], "import-memory-target")

    def test_produce_conversation_briefs_truthful_only_excludes_manual_entries(self) -> None:
        source_file = self.root / "produce-brief-truthful-only.jsonl"
        source_file.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "mixed",
                    "created_at": "2026-05-25T14:20:00+00:00",
                    "content": "SampleUser: imported truthful message for brief scope.",
                    "metadata": {"source_message_id": "brief-truthful-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        import_session_jsonl(
            self.paths,
            {
                "path": str(source_file),
                "import_id": "import-brief-truthful",
                "source_session_id": "session-brief-truthful",
                "source_conversation_id": "telegram:brief-truthful",
            },
        )
        manual = append_entry(
            self.paths,
            {
                "entry_type": "manual_note",
                "source": "cli",
                "author_role": "human",
                "content": "Manual note that should not be briefed by truthful_only scope.",
                "created_at": "2026-05-25T14:21:00+00:00",
            },
        )

        out = produce_conversation_briefs(self.paths, {"limit": 10, "truthful_only": True})
        produced_ids = {item["entry_id"] for item in out["produced"]}
        self.assertEqual(out["selection_mode"], "provenance_scope")
        self.assertNotIn(manual["entry_id"], produced_ids)
        self.assertGreaterEqual(out["produced_count"], 1)

    def test_produce_compressed_memory_uses_overlay_effective_content(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "cli",
                "author_role": "mixed",
                "content": "SampleUser: baseline discussion without unique token.",
                "created_at": "2026-05-25T14:25:00+00:00",
            },
        )
        append_overlay(
            self.paths,
            {
                "entry_id": entry["entry_id"],
                "overlay_type": "annotation",
                "author": "operator",
                "content": "Correction context adds token delta-router-overlay for downstream memory.",
            },
        )
        out = produce_compressed_memory(self.paths, {"entry_ids": [entry["entry_id"]], "limit": 5, "force": True})
        self.assertEqual(out["produced_count"], 1)

        detail = fetch_entry_detail(self.paths, {"entry_id": entry["entry_id"]})
        memory_artifacts = [a for a in detail["artifacts"] if a["artifact_type"] == "compressed-memory"]
        self.assertGreaterEqual(len(memory_artifacts), 1)
        self.assertIn("delta-router-overlay", memory_artifacts[0]["content"])

    def test_entries_without_overlays_remain_compatible_for_fallback_and_producers(self) -> None:
        entry = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "cli",
                "author_role": "mixed",
                "content": "No overlays here, but token stable-compat-token appears in raw text.",
                "created_at": "2026-05-25T14:26:00+00:00",
            },
        )
        fallback = search_memory(self.paths, {"query": "stable-compat-token", "limit": 5})
        self.assertEqual(fallback["matches"][0]["entry_id"], entry["entry_id"])

        produced = produce_conversation_briefs(self.paths, {"entry_ids": [entry["entry_id"]], "limit": 5, "force": True})
        self.assertEqual(produced["produced_count"], 1)

    def test_producer_entry_ids_override_provenance_filters(self) -> None:
        scoped_file = self.root / "producer-entry-override-scoped.jsonl"
        scoped_file.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "mixed",
                    "created_at": "2026-05-25T14:30:00+00:00",
                    "content": "Scoped imported entry.",
                    "metadata": {"source_message_id": "producer-override-scoped"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        import_session_jsonl(
            self.paths,
            {
                "path": str(scoped_file),
                "import_id": "import-producer-override",
                "source_session_id": "session-producer-override",
                "source_conversation_id": "telegram:producer-override",
            },
        )
        manual = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "cli",
                "author_role": "mixed",
                "content": "Manual explicit entry id target.",
                "created_at": "2026-05-25T14:31:00+00:00",
            },
        )

        out = produce_conversation_briefs(
            self.paths,
            {
                "limit": 10,
                "entry_ids": [manual["entry_id"]],
                "source_conversation_id": "telegram:producer-override",
            },
        )
        self.assertEqual(out["selection_mode"], "entry_ids")
        self.assertEqual(out["produced_count"], 1)
        self.assertEqual(out["produced"][0]["entry_id"], manual["entry_id"])

    def test_produce_compressed_memory_unscoped_behavior_remains_compatible(self) -> None:
        imported_file = self.root / "producer-unscoped-imported.jsonl"
        imported_file.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "mixed",
                    "created_at": "2026-05-25T14:40:00+00:00",
                    "content": "Unscoped imported entry.",
                    "metadata": {"source_message_id": "producer-unscoped-imported"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        import_session_jsonl(
            self.paths,
            {
                "path": str(imported_file),
                "import_id": "import-producer-unscoped",
                "source_session_id": "session-producer-unscoped",
                "source_conversation_id": "telegram:producer-unscoped",
            },
        )
        manual = append_entry(
            self.paths,
            {
                "entry_type": "chat_log",
                "source": "cli",
                "author_role": "mixed",
                "content": "Unscoped manual entry.",
                "created_at": "2026-05-25T14:41:00+00:00",
            },
        )

        out = produce_compressed_memory(self.paths, {"limit": 10, "force": True})
        self.assertEqual(out["selection_mode"], "unscoped")
        produced_ids = {item["entry_id"] for item in out["produced"]}
        self.assertIn(manual["entry_id"], produced_ids)
        self.assertGreaterEqual(out["produced_count"], 2)

    def test_cli_produce_compressed_memory_forwards_provenance_filters(self) -> None:
        target_file = self.root / "cli-producer-scope-target.jsonl"
        target_file.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "mixed",
                    "created_at": "2026-05-25T14:50:00+00:00",
                    "content": "CLI scoped producer target message.",
                    "metadata": {"source_message_id": "cli-producer-target-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        other_file = self.root / "cli-producer-scope-other.jsonl"
        other_file.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "mixed",
                    "created_at": "2026-05-25T14:51:00+00:00",
                    "content": "CLI scoped producer non-target message.",
                    "metadata": {"source_message_id": "cli-producer-other-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        import_session_jsonl(
            self.paths,
            {
                "path": str(target_file),
                "import_id": "import-cli-producer-target",
                "source_session_id": "session-cli-producer-target",
                "source_conversation_id": "telegram:cli-producer-target",
            },
        )
        import_session_jsonl(
            self.paths,
            {
                "path": str(other_file),
                "import_id": "import-cli-producer-other",
                "source_session_id": "session-cli-producer-other",
                "source_conversation_id": "telegram:cli-producer-other",
            },
        )

        cmd = [
            sys.executable,
            "-m",
            "agent_diary.cli.main",
            "--json",
            "produce-compressed-memory",
            "--limit",
            "20",
            "--import-id",
            "import-cli-producer-target",
            "--truthful-only",
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        completed = subprocess.run(
            cmd,
            cwd=self.root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        out = json.loads(completed.stdout)
        self.assertEqual(out["selection_mode"], "provenance_scope")
        self.assertEqual(out["filters"]["import_id"], "import-cli-producer-target")
        self.assertEqual(out["produced_count"], 1)

    def test_import_session_and_refresh_derived_runs_core_producers_for_imported_entries(self) -> None:
        source_file = self.root / "import-and-analyze.jsonl"
        source_file.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "mixed",
                    "created_at": "2026-05-25T15:00:00+00:00",
                    "content": (
                        "SampleUser: can you check whether the browser node is back?\n"
                        "Assistant: yes, I’ll inspect the Mac-side route now.\n"
                        "SampleUser: great. let me know if the tunnel still works."
                    ),
                    "metadata": {"source_message_id": "import-analyze-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        first = import_session_and_refresh_derived(
            self.paths,
            {
                "path": str(source_file),
                "import_id": "import_analyze_first",
                "source_session_id": "session-derive-1",
                "source_conversation_id": "telegram:derive-1",
            },
        )
        self.assertEqual(first["imported_count"], 1)
        self.assertEqual(first["skipped_count"], 0)
        self.assertEqual(first["derived"]["conversation_briefs"]["status"], "ok")
        self.assertEqual(first["derived"]["compressed_memory"]["status"], "ok")
        self.assertEqual(first["derived"]["open_loops"]["status"], "ok")
        self.assertGreaterEqual(first["derived"]["conversation_briefs"]["produced_count"], 1)
        self.assertGreaterEqual(first["derived"]["compressed_memory"]["produced_count"], 1)
        self.assertIsNotNone(first["derived"]["open_loops"]["artifact_id"])

        imported_entry_id = first["imported_entry_ids"][0]
        detail = fetch_entry_detail(self.paths, {"entry_id": imported_entry_id})
        artifact_types = {artifact["artifact_type"] for artifact in detail["artifacts"]}
        self.assertIn("conversation-brief", artifact_types)
        self.assertIn("compressed-memory", artifact_types)

        second = import_session_and_refresh_derived(
            self.paths,
            {
                "path": str(source_file),
                "import_id": "import_analyze_second",
                "source_session_id": "session-derive-1",
                "source_conversation_id": "telegram:derive-1",
            },
        )
        self.assertEqual(second["imported_count"], 0)
        self.assertEqual(second["skipped_count"], 1)
        self.assertEqual(second["derived"]["conversation_briefs"]["status"], "skipped_no_imports")
        self.assertEqual(second["derived"]["compressed_memory"]["status"], "skipped_no_imports")
        self.assertEqual(second["derived"]["open_loops"]["status"], "skipped_no_imports")

    def test_refresh_derived_for_import_reruns_without_reimporting_raw_entries(self) -> None:
        source_file = self.root / "refresh-derived.jsonl"
        source_file.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "mixed",
                    "created_at": "2026-05-25T16:00:00+00:00",
                    "content": (
                        "SampleUser: can you check whether the browser node is back?\n"
                        "Assistant: yes, I’ll inspect the Mac-side route now.\n"
                        "SampleUser: great. let me know if the tunnel still works."
                    ),
                    "metadata": {"source_message_id": "refresh-derived-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        imported = import_session_and_refresh_derived(
            self.paths,
            {
                "path": str(source_file),
                "import_id": "import_refresh_target",
                "source_session_id": "session-refresh-1",
                "source_conversation_id": "telegram:refresh-1",
            },
        )
        self.assertEqual(imported["imported_count"], 1)
        entry_id = imported["imported_entry_ids"][0]
        first_open_loop_artifact_id = imported["derived"]["open_loops"]["artifact_id"]

        refreshed = refresh_derived_for_import(self.paths, {"import_id": "import_refresh_target"})
        self.assertEqual(refreshed["import_id"], "import_refresh_target")
        self.assertEqual(refreshed["imported_entry_count"], 1)
        self.assertEqual(refreshed["imported_entry_ids"], [entry_id])
        self.assertTrue(refreshed["force"])
        self.assertEqual(refreshed["derived"]["conversation_briefs"]["status"], "ok")
        self.assertEqual(refreshed["derived"]["compressed_memory"]["status"], "ok")
        self.assertEqual(refreshed["derived"]["open_loops"]["status"], "ok")
        self.assertGreaterEqual(refreshed["derived"]["conversation_briefs"]["produced_count"], 1)
        self.assertGreaterEqual(refreshed["derived"]["compressed_memory"]["produced_count"], 1)
        self.assertIsNotNone(refreshed["derived"]["open_loops"]["artifact_id"])
        self.assertNotEqual(refreshed["derived"]["open_loops"]["artifact_id"], first_open_loop_artifact_id)

        repeated = refresh_derived_for_import(self.paths, {"import_id": "import_refresh_target"})
        self.assertEqual(repeated["imported_entry_count"], 1)
        self.assertEqual(repeated["derived"]["conversation_briefs"]["status"], "ok")
        self.assertEqual(repeated["derived"]["compressed_memory"]["status"], "ok")
        self.assertEqual(repeated["derived"]["open_loops"]["status"], "ok")

    def test_refresh_derived_for_import_rejects_path_like_import_id(self) -> None:
        with self.assertRaises(ValueError):
            refresh_derived_for_import(self.paths, {"import_id": "../ledger"})

    def test_import_session_jsonl_dry_run_reports_without_writing_entries(self) -> None:
        source_file = self.root / "dry-run.jsonl"
        source_file.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "telegram-direct-import",
                    "author_role": "human",
                    "created_at": "2026-05-25T14:00:00+00:00",
                    "content": "Preview this import only.",
                    "metadata": {"source_message_id": "dry-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = import_session_jsonl(self.paths, {"path": str(source_file), "dry_run": True, "import_id": "import_dry"})

        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(result["skipped_count"], 0)
        self.assertNotIn("ledger_path", result)
        self.assertTrue(Path(result["manifest_path"]).exists())
        self.assertEqual(list(self.paths.entries_dir.glob("**/*.json")), [])

    def test_build_session_entries_groups_messages_and_preserves_ids(self) -> None:
        transcript = self.root / "transcript.jsonl"
        transcript.write_text(
            "\n".join(
                [
                    json.dumps({"message_id": "1", "created_at": "2026-05-25T10:00:00+00:00", "author_role": "human", "speaker": "User", "content": "First"}),
                    json.dumps({"message_id": "2", "created_at": "2026-05-25T10:05:00+00:00", "author_role": "agent", "speaker": "Assistant", "content": "Second"}),
                    json.dumps({"message_id": "3", "created_at": "2026-05-25T11:10:00+00:00", "author_role": "human", "speaker": "User", "content": "Third"}),
                ]
            ),
            encoding="utf-8",
        )
        out_path = self.root / "built.jsonl"
        result = build_session_jsonl(
            input_path=transcript,
            output_path=out_path,
            source="telegram-direct-import",
            gap_minutes=30,
            max_chars=4000,
            min_messages_before_gap_split=0,
            min_chars_before_gap_split=0,
        )

        self.assertEqual(result["message_count"], 3)
        self.assertEqual(result["entry_count"], 2)
        built_lines = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(built_lines[0]["author_role"], "mixed")
        self.assertEqual(built_lines[0]["metadata"]["source_message_ids"], ["1", "2"])
        self.assertIn("User: First", built_lines[0]["content"])
        self.assertIn("Assistant: Second", built_lines[0]["content"])
        self.assertEqual(built_lines[1]["metadata"]["source_message_ids"], ["3"])

    def test_build_session_entries_splits_on_max_chars(self) -> None:
        messages = [
            TranscriptMessage(
                message_id="1",
                created_at="2026-05-25T10:00:00+00:00",
                author_role="human",
                speaker="User",
                content="A" * 160,
                metadata={},
            ),
            TranscriptMessage(
                message_id="2",
                created_at="2026-05-25T10:01:00+00:00",
                author_role="agent",
                speaker="Assistant",
                content="B" * 160,
                metadata={},
            ),
        ]
        built = build_session_entries(
            messages,
            source="telegram-direct-import",
            gap_minutes=30,
            max_chars=200,
        )
        self.assertEqual(len(built), 2)

    def test_build_session_entries_splits_on_max_messages(self) -> None:
        messages = [
            TranscriptMessage(
                message_id=str(idx),
                created_at=f"2026-05-25T10:0{idx}:00+00:00",
                author_role="human" if idx % 2 else "agent",
                speaker="User" if idx % 2 else "Assistant",
                content=f"Message {idx}",
                metadata={},
            )
            for idx in range(1, 4)
        ]
        built = build_session_entries(
            messages,
            source="telegram-direct-import",
            gap_minutes=60,
            max_chars=6000,
            max_messages=2,
        )
        self.assertEqual(len(built), 2)
        self.assertEqual(built[0]["metadata"]["source_message_ids"], ["1", "2"])
        self.assertEqual(built[1]["metadata"]["source_message_ids"], ["3"])

    def test_build_session_entries_keeps_small_long_gap_chunk_together(self) -> None:
        messages = [
            TranscriptMessage(
                message_id="1",
                created_at="2026-05-25T10:00:00+00:00",
                author_role="human",
                speaker="User",
                content="Can you check the browser on my Mac?",
                metadata={"source_session_id": "s1", "source_conversation_id": "c1"},
            ),
            TranscriptMessage(
                message_id="2",
                created_at="2026-05-25T11:15:00+00:00",
                author_role="agent",
                speaker="Assistant",
                content="Yes, I found the issue and fixed it.",
                metadata={"source_session_id": "s1", "source_conversation_id": "c1", "source_parent_id": "1"},
            ),
        ]
        built = build_session_entries(
            messages,
            source="telegram-direct-import",
            gap_minutes=60,
            max_chars=6000,
            max_messages=80,
        )
        self.assertEqual(len(built), 1)

    def test_build_session_entries_splits_on_unrelated_long_gap_restart(self) -> None:
        messages = [
            TranscriptMessage(
                message_id="1",
                created_at="2026-05-25T10:00:00+00:00",
                author_role="human",
                speaker="User",
                content="Can you check the browser on my Mac?",
                metadata={"source_session_id": "s1", "source_conversation_id": "c1"},
            ),
            TranscriptMessage(
                message_id="2",
                created_at="2026-05-25T11:15:00+00:00",
                author_role="human",
                speaker="User",
                content="Different topic: what happened with the Play Console build?",
                metadata={"source_session_id": "s1", "source_conversation_id": "c1"},
            ),
            TranscriptMessage(
                message_id="3",
                created_at="2026-05-25T11:16:00+00:00",
                author_role="agent",
                speaker="Assistant",
                content="It still needs the review-mode package.",
                metadata={"source_session_id": "s1", "source_conversation_id": "c1"},
            ),
        ]
        built = build_session_entries(
            messages,
            source="telegram-direct-import",
            gap_minutes=60,
            max_chars=6000,
            max_messages=80,
        )
        self.assertEqual(len(built), 2)
        self.assertEqual(built[0]["metadata"]["source_message_ids"], ["1"])
        self.assertEqual(built[1]["metadata"]["source_message_ids"], ["2", "3"])

    def test_build_session_entries_splits_on_source_conversation_change(self) -> None:
        messages = [
            TranscriptMessage(
                message_id="1",
                created_at="2026-05-25T10:00:00+00:00",
                author_role="human",
                speaker="User",
                content="First thread",
                metadata={"source_session_id": "s1", "source_conversation_id": "c1"},
            ),
            TranscriptMessage(
                message_id="2",
                created_at="2026-05-25T10:01:00+00:00",
                author_role="agent",
                speaker="Assistant",
                content="Second thread",
                metadata={"source_session_id": "s1", "source_conversation_id": "c2"},
            ),
        ]
        built = build_session_entries(
            messages,
            source="telegram-direct-import",
            gap_minutes=60,
            max_chars=6000,
            max_messages=80,
        )
        self.assertEqual(len(built), 2)
        self.assertEqual(built[0]["metadata"]["source_conversation_id"], "c1")
        self.assertEqual(built[1]["metadata"]["source_conversation_id"], "c2")

    def test_build_transcript_jsonl_generic_message_jsonl_outputs_canonical_shape(self) -> None:
        source_file = self.root / "raw-generic.jsonl"
        source_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "id": "g1",
                            "timestamp": "2026-05-27T09:00:00+00:00",
                            "role": "user",
                            "speaker": "User",
                            "text": "Hello there",
                            "metadata": {"channel": "telegram"},
                        }
                    ),
                    json.dumps(
                        {
                            "message_id": "g2",
                            "created_at": "2026-05-27T09:01:00+00:00",
                            "author_role": "assistant",
                            "content": "Hi User",
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        out_path = self.root / "transcript.jsonl"

        result = adapt_session_export(
            input_path=source_file,
            output_path=out_path,
            format_name="generic-message-jsonl",
            source_session_id="session-abc",
            source_conversation_id="conv-xyz",
        )

        self.assertEqual(result["message_count"], 2)
        lines = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(lines[0]["message_id"], "g1")
        self.assertEqual(lines[0]["created_at"], "2026-05-27T09:00:00+00:00")
        self.assertEqual(lines[0]["author_role"], "human")
        self.assertEqual(lines[0]["speaker"], "User")
        self.assertEqual(lines[0]["content"], "Hello there")
        self.assertIn("metadata", lines[0])
        self.assertEqual(lines[0]["metadata"]["source_message_id"], "g1")
        self.assertEqual(lines[1]["author_role"], "agent")

    def test_build_transcript_jsonl_rejects_malformed_input(self) -> None:
        source_file = self.root / "bad-generic.jsonl"
        source_file.write_text(
            json.dumps(
                {
                    "id": "bad-1",
                    "timestamp": "2026-05-27T09:00:00+00:00",
                    "role": "user",
                    # content/text/message intentionally missing
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(ValueError):
            adapt_session_export(
                input_path=source_file,
                output_path=self.root / "unused.jsonl",
                format_name="generic-message-jsonl",
            )

    def test_build_transcript_jsonl_openclaw_session_json_preserves_source_ids(self) -> None:
        source_file = self.root / "openclaw-session.json"
        source_file.write_text(
            json.dumps(
                {
                    "session_id": "oc-session-1",
                    "conversation_id": "oc-conv-1",
                    "messages": [
                        {
                            "id": "m-001",
                            "created_at": "2026-05-27T10:00:00+00:00",
                            "role": "user",
                            "speaker": "SampleUser",
                            "content": "Need follow-up reminder",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        out_path = self.root / "openclaw-transcript.jsonl"

        result = adapt_session_export(
            input_path=source_file,
            output_path=out_path,
            format_name="openclaw-session-json",
        )

        self.assertEqual(result["source_session_id"], "oc-session-1")
        self.assertEqual(result["source_conversation_id"], "oc-conv-1")
        row = json.loads(out_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["message_id"], "m-001")
        self.assertEqual(row["created_at"], "2026-05-27T10:00:00+00:00")
        self.assertEqual(row["metadata"]["source_message_id"], "m-001")
        self.assertEqual(row["metadata"]["source_session_id"], "oc-session-1")
        self.assertEqual(row["metadata"]["source_conversation_id"], "oc-conv-1")

    def test_build_transcript_jsonl_openclaw_telegram_jsonl_preserves_truth_fields(self) -> None:
        source_file = self.root / "telegram-source.json"
        source_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "key": "default:123456789:5316",
                            "node": {
                                "sourceMessage": {
                                    "message_id": 5316,
                                    "from": {"id": 123456789, "is_bot": False, "username": "SampleUser"},
                                    "chat": {"id": 123456789, "type": "private"},
                                    "date": 1778932179,
                                    "text": "so looks like we're all good",
                                }
                            },
                        }
                    )
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        out_path = self.root / "telegram-transcript.jsonl"
        result = adapt_session_export(
            input_path=source_file,
            output_path=out_path,
            format_name="openclaw-telegram-jsonl",
        )

        self.assertEqual(result["message_count"], 1)
        self.assertEqual(result["source_conversation_id"], "telegram:123456789")
        row = json.loads(out_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["message_id"], "5316")
        self.assertEqual(row["author_role"], "human")
        self.assertEqual(row["speaker"], "SampleUser")
        self.assertEqual(row["content"], "so looks like we're all good")
        self.assertTrue(row["created_at"].startswith("2026-"))
        self.assertEqual(row["metadata"]["source_key"], "default:123456789:5316")
        self.assertEqual(row["metadata"]["source_message_id"], "5316")
        self.assertEqual(row["metadata"]["telegram_chat_id"], 123456789)

    def test_build_transcript_jsonl_openclaw_telegram_jsonl_rejects_missing_source_message(self) -> None:
        source_file = self.root / "telegram-bad.json"
        source_file.write_text(json.dumps({"key": "bad", "node": {}}) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            adapt_session_export(
                input_path=source_file,
                output_path=self.root / "unused-telegram.jsonl",
                format_name="openclaw-telegram-jsonl",
            )

    def test_build_openclaw_telegram_direct_transcript_combines_inbound_and_sent_messages(self) -> None:
        inbound = self.root / "telegram-messages.json"
        inbound.write_text(
            json.dumps(
                {
                    "key": "default:123456789:7001",
                    "node": {
                        "sourceMessage": {
                            "message_id": 7001,
                            "from": {"id": 123456789, "is_bot": False, "username": "SampleUser"},
                            "chat": {"id": 123456789, "type": "private"},
                            "date": 1778925595,
                            "text": "hello assistant",
                        }
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        sessions_root = self.root / "sessions"
        sessions_root.mkdir()
        session_file = sessions_root / "run-1.jsonl"
        session_file.write_text(
            "\n".join(
                [
                    json.dumps({"type": "session", "id": "sess-1", "timestamp": "2026-05-16T10:00:00Z", "cwd": "/tmp"}),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "assistant-call-record",
                            "parentId": "prev",
                            "timestamp": "2026-05-16T10:00:10Z",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "toolCall",
                                        "id": "tool-call-1",
                                        "name": "message",
                                        "arguments": {"action": "send", "message": "hi user"},
                                    }
                                ],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "tool-result-record",
                            "parentId": "assistant-call-record",
                            "timestamp": "2026-05-16T10:00:11Z",
                            "message": {
                                "role": "toolResult",
                                "toolCallId": "tool-call-1",
                                "toolName": "message",
                                "content": [
                                    {
                                        "type": "toolResult",
                                        "content": json.dumps({"ok": True, "messageId": "7002", "chatId": "123456789"}),
                                    }
                                ],
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        out_path = self.root / "telegram-direct-transcript.jsonl"
        result = build_openclaw_telegram_direct_transcript(
            inbound_path=inbound,
            sessions_root=sessions_root,
            output_path=out_path,
            chat_id="123456789",
        )
        self.assertEqual(result["message_count"], 2)
        self.assertEqual(result["inbound_count"], 1)
        self.assertEqual(result["outbound_count"], 1)
        rows = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(rows[0]["message_id"], "7001")
        self.assertEqual(rows[0]["author_role"], "human")
        self.assertEqual(rows[1]["message_id"], "7002")
        self.assertEqual(rows[1]["author_role"], "agent")
        self.assertEqual(rows[1]["content"], "hi user")
        self.assertEqual(rows[1]["metadata"]["telegram_direction"], "outbound")
        self.assertEqual(rows[1]["metadata"]["source_runtime_tool_call_id"], "tool-call-1")

    def test_build_openclaw_telegram_direct_transcript_reads_plugin_state_sqlite(self) -> None:
        inbound = self.root / "telegram-plugin-state.sqlite"
        sessions_root = self.root / "sessions-plugin-state"
        self._write_telegram_plugin_state_fixture(inbound, sessions_root)

        out_path = self.root / "telegram-direct-transcript-sqlite.jsonl"
        result = build_openclaw_telegram_direct_transcript(
            inbound_path=inbound,
            sessions_root=sessions_root,
            output_path=out_path,
            chat_id="123456789",
        )
        self.assertEqual(result["message_count"], 2)
        self.assertEqual(result["inbound_count"], 1)
        self.assertEqual(result["outbound_count"], 1)
        rows = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual([row["message_id"] for row in rows], ["7001", "7002"])
        self.assertEqual(rows[0]["metadata"]["source_store"], "openclaw-plugin-state-sqlite")
        self.assertEqual(rows[0]["metadata"]["telegram_direction"], "inbound")

    def test_build_openclaw_telegram_direct_transcript_accepts_message_result_without_chat_id(self) -> None:
        inbound = self.root / "telegram-plugin-state-no-chat-id.sqlite"
        sessions_root = self.root / "sessions-plugin-state-no-chat-id"
        self._write_telegram_plugin_state_fixture(inbound, sessions_root, omit_result_chat_id=True)

        out_path = self.root / "telegram-direct-transcript-no-chat-id.jsonl"
        result = build_openclaw_telegram_direct_transcript(
            inbound_path=inbound,
            sessions_root=sessions_root,
            output_path=out_path,
            chat_id="123456789",
        )
        self.assertEqual(result["message_count"], 2)
        self.assertEqual(result["inbound_count"], 1)
        self.assertEqual(result["outbound_count"], 1)
        rows = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual([row["message_id"] for row in rows], ["7001", "7002"])
        self.assertEqual(rows[1]["metadata"]["telegram_chat_id"], "123456789")
        self.assertEqual(rows[1]["metadata"]["telegram_direction"], "outbound")

    def test_build_openclaw_telegram_direct_transcript_drops_outbound_before_inbound_window(self) -> None:
        inbound = self.root / "telegram-messages-window.json"
        inbound.write_text(
            json.dumps(
                {
                    "key": "default:123456789:7001",
                    "node": {
                        "sourceMessage": {
                            "message_id": 7001,
                            "from": {"id": 123456789, "is_bot": False, "username": "SampleUser"},
                            "chat": {"id": 123456789, "type": "private"},
                            "date": 1778925595,
                            "text": "hello assistant",
                        }
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        sessions_root = self.root / "sessions-window"
        sessions_root.mkdir()
        session_file = sessions_root / "run-1.jsonl"
        session_file.write_text(
            "\n".join(
                [
                    json.dumps({"type": "session", "id": "sess-1", "timestamp": "2026-05-16T09:00:00Z", "cwd": "/tmp"}),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "assistant-call-record-early",
                            "parentId": "prev",
                            "timestamp": "2026-05-16T08:59:10Z",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "toolCall",
                                        "id": "tool-call-early",
                                        "name": "message",
                                        "arguments": {"action": "send", "message": "old reply"},
                                    }
                                ],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "tool-result-record-early",
                            "parentId": "assistant-call-record-early",
                            "timestamp": "2026-05-16T08:59:11Z",
                            "message": {
                                "role": "toolResult",
                                "toolCallId": "tool-call-early",
                                "toolName": "message",
                                "content": [
                                    {
                                        "type": "toolResult",
                                        "content": json.dumps({"ok": True, "messageId": "6999", "chatId": "123456789"}),
                                    }
                                ],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "assistant-call-record-current",
                            "parentId": "prev",
                            "timestamp": "2026-05-16T10:00:10Z",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "toolCall",
                                        "id": "tool-call-current",
                                        "name": "message",
                                        "arguments": {"action": "send", "message": "current reply"},
                                    }
                                ],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "tool-result-record-current",
                            "parentId": "assistant-call-record-current",
                            "timestamp": "2026-05-16T10:00:11Z",
                            "message": {
                                "role": "toolResult",
                                "toolCallId": "tool-call-current",
                                "toolName": "message",
                                "content": [
                                    {
                                        "type": "toolResult",
                                        "content": json.dumps({"ok": True, "messageId": "7002", "chatId": "123456789"}),
                                    }
                                ],
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        out_path = self.root / "telegram-direct-transcript-window.jsonl"
        result = build_openclaw_telegram_direct_transcript(
            inbound_path=inbound,
            sessions_root=sessions_root,
            output_path=out_path,
            chat_id="123456789",
        )
        self.assertEqual(result["message_count"], 2)
        rows = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual([row["message_id"] for row in rows], ["7001", "7002"])

    def test_build_transcript_jsonl_openclaw_session_jsonl_maps_user_and_assistant_text(self) -> None:
        source_file = self.root / "session-log.jsonl"
        source_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session",
                            "version": 3,
                            "id": "session-123",
                            "timestamp": "2026-05-21T06:46:22.209Z",
                            "cwd": "/home/alex",
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "msg-1",
                            "parentId": None,
                            "timestamp": "2026-05-21T06:46:22.208Z",
                            "message": {
                                "role": "user",
                                "content": "hello there",
                                "timestamp": 1779345982195,
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "msg-2",
                            "parentId": "msg-1",
                            "timestamp": "2026-05-21T06:46:22.210Z",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {"type": "toolCall", "name": "memory_search"},
                                    {"type": "text", "text": "Here is the reply text."},
                                    {"type": "toolResult", "toolCallId": "x"},
                                ],
                                "timestamp": 1779345982210,
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        out_path = self.root / "session-transcript.jsonl"

        result = adapt_session_export(
            input_path=source_file,
            output_path=out_path,
            format_name="openclaw-session-jsonl",
        )

        self.assertEqual(result["message_count"], 2)
        rows = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(rows[0]["message_id"], "msg-1")
        self.assertEqual(rows[0]["author_role"], "human")
        self.assertEqual(rows[0]["speaker"], "User")
        self.assertEqual(rows[0]["content"], "hello there")
        self.assertEqual(rows[0]["metadata"]["source_session_id"], "session-123")
        self.assertNotIn("source_parent_id", rows[0]["metadata"])
        self.assertEqual(rows[1]["message_id"], "msg-2")
        self.assertEqual(rows[1]["author_role"], "agent")
        self.assertEqual(rows[1]["speaker"], "Assistant")
        self.assertEqual(rows[1]["content"], "Here is the reply text.")
        self.assertEqual(rows[1]["metadata"]["source_parent_id"], "msg-1")
        self.assertEqual(rows[1]["metadata"]["openclaw_message_role"], "assistant")
        self.assertEqual(rows[1]["metadata"]["source_session_cwd"], "/home/alex")

    def test_build_transcript_jsonl_openclaw_session_jsonl_skips_tool_only_assistant_messages(self) -> None:
        source_file = self.root / "session-tool-only.jsonl"
        source_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "message",
                            "id": "msg-1",
                            "parentId": None,
                            "timestamp": "2026-05-21T06:46:22.210Z",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "toolCall", "name": "memory_search"}],
                                "timestamp": 1779345982210,
                            },
                        }
                    )
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        out_path = self.root / "session-tool-only-transcript.jsonl"

        result = adapt_session_export(
            input_path=source_file,
            output_path=out_path,
            format_name="openclaw-session-jsonl",
        )

        self.assertEqual(result["message_count"], 0)
        self.assertEqual(out_path.read_text(encoding="utf-8"), "")

    def test_build_transcript_jsonl_openclaw_session_jsonl_rejects_bad_message_record(self) -> None:
        source_file = self.root / "session-bad.jsonl"
        source_file.write_text(
            json.dumps(
                {
                    "type": "message",
                    "id": "msg-1",
                    "parentId": None,
                    "timestamp": "2026-05-21T06:46:22.210Z",
                    "message": "not an object",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            adapt_session_export(
                input_path=source_file,
                output_path=self.root / "unused-session.jsonl",
                format_name="openclaw-session-jsonl",
            )

    def _write_openclaw_session_fixture(self, path: Path) -> None:
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session",
                            "version": 3,
                            "id": "session-plain-1",
                            "timestamp": "2026-05-21T10:00:00.000Z",
                            "cwd": "/home/alex",
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "m-1",
                            "parentId": None,
                            "timestamp": "2026-05-21T10:00:01.000Z",
                            "message": {
                                "role": "user",
                                "content": "hello there",
                                "timestamp": 1779360001000,
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "m-2",
                            "parentId": "m-1",
                            "timestamp": "2026-05-21T10:00:02.000Z",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {"type": "toolCall", "name": "memory_search"},
                                    {"type": "text", "text": "hi there"},
                                    {"type": "toolResult", "toolCallId": "x"},
                                ],
                                "timestamp": 1779360002000,
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "m-3",
                            "parentId": "m-2",
                            "timestamp": "2026-05-21T10:00:03.000Z",
                            "message": {
                                "role": "user",
                                "content": "what should we do next?",
                                "timestamp": 1779360003000,
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "m-4",
                            "parentId": "m-3",
                            "timestamp": "2026-05-21T10:00:04.000Z",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {"type": "text", "text": "We should keep the scope narrow."}
                                ],
                                "timestamp": 1779360004000,
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def _write_openclaw_work_trace_fixture(self, path: Path) -> None:
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session",
                            "version": 3,
                            "id": "session-work-1",
                            "timestamp": "2026-05-29T10:00:00.000Z",
                            "cwd": "/home/alex/projects/demo",
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "assistant-call-1",
                            "parentId": "prev",
                            "timestamp": "2026-05-29T10:00:10.000Z",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "toolCall",
                                        "id": "tool-call-bash",
                                        "name": "bash",
                                        "arguments": {"command": "git status --short", "cwd": "/home/alex/projects/demo"},
                                    }
                                ],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "tool-result-1",
                            "parentId": "assistant-call-1",
                            "timestamp": "2026-05-29T10:00:11.000Z",
                            "message": {
                                "role": "toolResult",
                                "toolCallId": "tool-call-bash",
                                "toolName": "bash",
                                "content": [{"type": "toolResult", "content": " M ui/app.js"}],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "assistant-call-2",
                            "parentId": "tool-result-1",
                            "timestamp": "2026-05-29T10:01:00.000Z",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "toolCall",
                                        "id": "tool-call-exec",
                                        "name": "exec_command",
                                        "arguments": {"cmd": "PYTHONPATH=src python3 -m unittest tests.test_append_entry_slice -v", "workdir": "/home/alex/development/agent-diary"},
                                    }
                                ],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "tool-result-2",
                            "parentId": "assistant-call-2",
                            "timestamp": "2026-05-29T10:01:05.000Z",
                            "message": {
                                "role": "toolResult",
                                "toolCallId": "tool-call-exec",
                                "toolName": "exec_command",
                                "content": [{"type": "toolResult", "content": json.dumps({"exit_code": 0, "output": "OK"})}],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "assistant-call-3",
                            "parentId": "tool-result-2",
                            "timestamp": "2026-05-29T10:02:00.000Z",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "toolCall",
                                        "id": "tool-call-browser",
                                        "name": "openclaw_browser",
                                        "arguments": {"action": "snapshot"},
                                    }
                                ],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "tool-result-3",
                            "parentId": "assistant-call-3",
                            "timestamp": "2026-05-29T10:02:03.000Z",
                            "message": {
                                "role": "toolResult",
                                "toolCallId": "tool-call-browser",
                                "toolName": "openclaw_browser",
                                "content": [{"type": "toolResult", "content": json.dumps({"ok": True, "targetId": "t1"})}],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "assistant-call-4",
                            "parentId": "tool-result-3",
                            "timestamp": "2026-05-29T10:03:00.000Z",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "toolCall",
                                        "id": "tool-call-message",
                                        "name": "message",
                                        "arguments": {"action": "send", "message": "done"},
                                    }
                                ],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "tool-result-4",
                            "parentId": "assistant-call-4",
                            "timestamp": "2026-05-29T10:03:02.000Z",
                            "message": {
                                "role": "toolResult",
                                "toolCallId": "tool-call-message",
                                "toolName": "message",
                                "content": [{"type": "toolResult", "content": json.dumps({"ok": True, "messageId": "1"})}],
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def _write_telegram_direct_import_fixture(self, inbound_path: Path, sessions_root: Path) -> None:
        inbound_path.write_text(
            json.dumps(
                {
                    "key": "default:123456789:7001",
                    "node": {
                        "sourceMessage": {
                            "message_id": 7001,
                            "from": {"id": 123456789, "is_bot": False, "username": "SampleUser"},
                            "chat": {"id": 123456789, "type": "private"},
                            "date": 1778925595,
                            "text": "hello assistant",
                        }
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self._write_telegram_direct_sessions_fixture(sessions_root)

    def _write_telegram_plugin_state_fixture(
        self,
        sqlite_path: Path,
        sessions_root: Path,
        *,
        omit_result_chat_id: bool = False,
    ) -> None:
        with sqlite3.connect(sqlite_path) as conn:
            conn.execute(
                """
                CREATE TABLE plugin_state_entries (
                    plugin_id TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    entry_key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER,
                    PRIMARY KEY (plugin_id, namespace, entry_key)
                )
                """
            )
            conn.executemany(
                """
                INSERT INTO plugin_state_entries (plugin_id, namespace, entry_key, value_json, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, NULL)
                """,
                [
                    (
                        "telegram",
                        "telegram.message-cache",
                        "scope:default:123456789:7001",
                        json.dumps(
                            {
                                "sourceMessage": {
                                    "message_id": 7001,
                                    "from": {"id": 123456789, "is_bot": False, "username": "SampleUser"},
                                    "chat": {"id": 123456789, "type": "private"},
                                    "date": 1778925595,
                                    "text": "hello assistant",
                                }
                            }
                        ),
                        1778925595000,
                    ),
                    (
                        "telegram",
                        "telegram.message-cache",
                        "scope:default:123456789:7002",
                        json.dumps(
                            {
                                "sourceMessage": {
                                    "message_id": 7002,
                                    "from": {"id": 8682079149, "is_bot": True, "username": "willhowardbot"},
                                    "chat": {"id": 123456789, "type": "private"},
                                    "date": 1778925611,
                                    "text": "hi user",
                                }
                            }
                        ),
                        1778925611000,
                    ),
                ],
            )
            conn.commit()
        self._write_telegram_direct_sessions_fixture(
            sessions_root,
            omit_result_chat_id=omit_result_chat_id,
        )

    def _write_telegram_direct_sessions_fixture(self, sessions_root: Path, *, omit_result_chat_id: bool = False) -> None:
        sessions_root.mkdir(parents=True, exist_ok=True)
        session_file = sessions_root / "run-1.jsonl"
        result_payload = {"ok": True, "messageId": "7002"}
        if not omit_result_chat_id:
            result_payload["chatId"] = "123456789"
        session_file.write_text(
            "\n".join(
                [
                    json.dumps({"type": "session", "id": "sess-1", "timestamp": "2026-05-16T10:00:00Z", "cwd": "/tmp"}),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "assistant-call-record",
                            "parentId": "prev",
                            "timestamp": "2026-05-16T10:00:10Z",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "toolCall",
                                        "id": "tool-call-1",
                                        "name": "message",
                                        "arguments": {"action": "send", "message": "hi user"},
                                    }
                                ],
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "tool-result-record",
                            "parentId": "assistant-call-record",
                            "timestamp": "2026-05-16T10:00:11Z",
                            "message": {
                                "role": "toolResult",
                                "toolCallId": "tool-call-1",
                                "toolName": "message",
                                "content": [
                                    {
                                        "type": "toolResult",
                                        "content": json.dumps(result_payload),
                                    }
                                ],
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def _write_trajectory_fixture(self, path: Path, *, session_key: str, ts: str, session_file: Path, session_id: str) -> None:
        path.write_text(
            json.dumps(
                {
                    "type": "session.started",
                    "ts": ts,
                    "sessionId": session_id,
                    "sessionKey": session_key,
                    "data": {
                        "sessionFile": str(session_file.resolve()),
                        "threadId": "telegram-direct-thread",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def test_import_openclaw_session_command_imports_fixture(self) -> None:
        source_file = self.root / "openclaw-session.jsonl"
        self._write_openclaw_session_fixture(source_file)

        cmd = [
            sys.executable,
            "-m",
            "agent_diary.cli.main",
            "--json",
            "import-openclaw-session",
            "--input-path",
            str(source_file),
            "--source",
            "openclaw-session-import",
            "--source-session-id",
            "session-plain-1",
            "--source-conversation-id",
            "openclaw:session-plain-1",
            "--import-id",
            "import-test-plain",
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        completed = subprocess.run(
            cmd,
            cwd=self.root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        out = json.loads(completed.stdout)
        self.assertFalse(out["dry_run"])
        self.assertEqual(out["resolved_source_session_id"], "session-plain-1")
        self.assertEqual(out["resolved_source_conversation_id"], "openclaw:session-plain-1")
        self.assertEqual(out["source_session_id"], "session-plain-1")
        self.assertEqual(out["source_conversation_id"], "openclaw:session-plain-1")
        self.assertEqual(out["transcript_message_count"], 4)
        self.assertEqual(out["session_chunk_count"], 1)
        self.assertEqual(out["imported_count"], 1)
        self.assertEqual(out["skipped_duplicate_count"], 0)
        self.assertEqual(out["import_id"], "import-test-plain")
        self.assertIn("session=session-plain-1", out["import_label"])
        self.assertIn("conversation=openclaw:session-plain-1", out["import_label"])
        self.assertIsInstance(out["batch_manifest_path"], str)
        self.assertTrue(out["batch_manifest_path"].endswith("import-test-plain.json"))
        self.assertEqual(out["adapter"]["message_count"], 4)
        self.assertEqual(out["session_builder"]["entry_count"], 1)
        self.assertEqual(out["import_result"]["imported_count"], 1)
        self.assertEqual(out["import_result"]["skipped_count"], 0)

        entry_id = out["import_result"]["imported"][0]["entry_id"]
        fetched = fetch_raw_entry(self.paths, {"entry_id": entry_id})
        self.assertEqual(fetched["entry"]["source"], "openclaw-session-import")
        self.assertEqual(fetched["entry"]["metadata"]["ingestion"]["truthful_source"], True)
        self.assertEqual(fetched["entry"]["metadata"]["ingestion"]["import_mode"], "session_jsonl")
        self.assertEqual(fetched["entry"]["metadata"]["ingestion"]["source_session_id"], "session-plain-1")

    def test_import_telegram_direct_command_imports_fixture(self) -> None:
        inbound = self.root / "telegram-messages-import.jsonl"
        sessions_root = self.root / "sessions-import"
        self._write_telegram_direct_import_fixture(inbound, sessions_root)

        cmd = [
            sys.executable,
            "-m",
            "agent_diary.cli.main",
            "--json",
            "import-telegram-direct",
            "--inbound-path",
            str(inbound),
            "--sessions-root",
            str(sessions_root),
            "--chat-id",
            "123456789",
            "--source",
            "telegram-direct-import",
            "--import-id",
            "import-telegram-direct-test",
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        completed = subprocess.run(cmd, cwd=self.root, env=env, check=True, capture_output=True, text=True)

        out = json.loads(completed.stdout)
        self.assertFalse(out["dry_run"])
        self.assertEqual(out["resolved_source_session_id"], "telegram-direct:123456789")
        self.assertEqual(out["resolved_source_conversation_id"], "telegram:123456789")
        self.assertEqual(out["transcript_message_count"], 2)
        self.assertEqual(out["session_chunk_count"], 1)
        self.assertEqual(out["imported_count"], 1)
        self.assertEqual(out["skipped_duplicate_count"], 0)
        self.assertEqual(out["import_id"], "import-telegram-direct-test")
        self.assertEqual(out["import_result"]["imported_count"], 1)
        self.assertEqual(out["import_result"]["skipped_count"], 0)
        self.assertIsInstance(out["batch_manifest_path"], str)
        self.assertTrue(out["batch_manifest_path"].endswith("import-telegram-direct-test.json"))

        entry_id = out["import_result"]["imported"][0]["entry_id"]
        fetched = fetch_raw_entry(self.paths, {"entry_id": entry_id})
        ingestion = fetched["entry"]["metadata"]["ingestion"]
        self.assertEqual(ingestion["truthful_source"], True)
        self.assertEqual(ingestion["source_session_id"], "telegram-direct:123456789")
        self.assertEqual(ingestion["source_conversation_id"], "telegram:123456789")

    def test_import_telegram_direct_command_imports_plugin_state_sqlite_fixture(self) -> None:
        inbound = self.root / "telegram-plugin-state-import.sqlite"
        sessions_root = self.root / "sessions-plugin-state-import"
        self._write_telegram_plugin_state_fixture(inbound, sessions_root)

        cmd = [
            sys.executable,
            "-m",
            "agent_diary.cli.main",
            "--json",
            "import-telegram-direct",
            "--inbound-path",
            str(inbound),
            "--sessions-root",
            str(sessions_root),
            "--chat-id",
            "123456789",
            "--source",
            "telegram-direct-import",
            "--import-id",
            "import-telegram-direct-plugin-state-test",
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        completed = subprocess.run(cmd, cwd=self.root, env=env, check=True, capture_output=True, text=True)

        out = json.loads(completed.stdout)
        self.assertFalse(out["dry_run"])
        self.assertEqual(out["transcript_message_count"], 2)
        self.assertEqual(out["imported_count"], 1)
        entry_id = out["import_result"]["imported"][0]["entry_id"]
        fetched = fetch_raw_entry(self.paths, {"entry_id": entry_id})
        self.assertIn("SampleUser: hello assistant", fetched["entry"]["content"])
        self.assertIn("Assistant: hi user", fetched["entry"]["content"])

    def test_import_telegram_direct_command_dry_run_does_not_write_raw_entries(self) -> None:
        inbound = self.root / "telegram-messages-dry-run.jsonl"
        sessions_root = self.root / "sessions-dry-run"
        self._write_telegram_direct_import_fixture(inbound, sessions_root)

        cmd = [
            sys.executable,
            "-m",
            "agent_diary.cli.main",
            "--json",
            "import-telegram-direct",
            "--inbound-path",
            str(inbound),
            "--sessions-root",
            str(sessions_root),
            "--chat-id",
            "123456789",
            "--dry-run",
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        completed = subprocess.run(cmd, cwd=self.root, env=env, check=True, capture_output=True, text=True)
        out = json.loads(completed.stdout)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["transcript_message_count"], 2)
        self.assertEqual(out["session_chunk_count"], 1)
        self.assertEqual(out["imported_count"], 1)
        self.assertEqual(out["skipped_duplicate_count"], 0)
        self.assertEqual(list_entries(self.paths, {"limit": 10, "offset": 0})["items"], [])

    def test_import_telegram_direct_command_skips_duplicates_on_repeat_run(self) -> None:
        inbound = self.root / "telegram-messages-repeat.jsonl"
        sessions_root = self.root / "sessions-repeat"
        self._write_telegram_direct_import_fixture(inbound, sessions_root)

        base_cmd = [
            sys.executable,
            "-m",
            "agent_diary.cli.main",
            "--json",
            "import-telegram-direct",
            "--inbound-path",
            str(inbound),
            "--sessions-root",
            str(sessions_root),
            "--chat-id",
            "123456789",
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        first = subprocess.run(base_cmd, cwd=self.root, env=env, check=True, capture_output=True, text=True)
        second = subprocess.run(base_cmd, cwd=self.root, env=env, check=True, capture_output=True, text=True)
        first_out = json.loads(first.stdout)
        second_out = json.loads(second.stdout)
        self.assertEqual(first_out["imported_count"], 1)
        self.assertEqual(first_out["skipped_duplicate_count"], 0)
        self.assertEqual(second_out["imported_count"], 0)
        self.assertEqual(second_out["skipped_duplicate_count"], 1)

    def test_import_telegram_direct_command_explicit_ids_are_surfaced(self) -> None:
        inbound = self.root / "telegram-messages-ids.jsonl"
        sessions_root = self.root / "sessions-ids"
        self._write_telegram_direct_import_fixture(inbound, sessions_root)

        cmd = [
            sys.executable,
            "-m",
            "agent_diary.cli.main",
            "--json",
            "import-telegram-direct",
            "--inbound-path",
            str(inbound),
            "--sessions-root",
            str(sessions_root),
            "--chat-id",
            "123456789",
            "--source-session-id",
            "telegram-direct:cusassistant",
            "--source-conversation-id",
            "telegram:cusassistant",
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        completed = subprocess.run(cmd, cwd=self.root, env=env, check=True, capture_output=True, text=True)
        out = json.loads(completed.stdout)
        self.assertEqual(out["resolved_source_session_id"], "telegram-direct:cusassistant")
        self.assertEqual(out["resolved_source_conversation_id"], "telegram:cusassistant")

    def test_backfill_telegram_direct_command_dry_run_discovers_without_writing_entries(self) -> None:
        inbound = self.root / "telegram-messages-backfill-dry.jsonl"
        sessions_root = self.root / "sessions-backfill-dry"
        self._write_telegram_direct_import_fixture(inbound, sessions_root)
        trajectories_root = self.root / "trajectories-backfill-dry"
        trajectories_root.mkdir(parents=True, exist_ok=True)
        self._write_trajectory_fixture(
            trajectories_root / "one.trajectory.jsonl",
            session_key="agent:main:telegram:default:direct:123456789",
            ts="2026-05-16T10:00:00Z",
            session_file=sessions_root / "run-1.jsonl",
            session_id="sess-1",
        )

        cmd = [
            sys.executable,
            "-m",
            "agent_diary.cli.main",
            "--json",
            "backfill-telegram-direct",
            "--inbound-path",
            str(inbound),
            "--sessions-root",
            str(sessions_root),
            "--trajectories-root",
            str(trajectories_root),
            "--session-key",
            "agent:main:telegram:default:direct:123456789",
            "--chat-id",
            "123456789",
            "--dry-run",
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        completed = subprocess.run(cmd, cwd=self.root, env=env, check=True, capture_output=True, text=True)
        out = json.loads(completed.stdout)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["discovered_session_file_count"], 1)
        self.assertEqual(out["processed_session_file_count"], 1)
        self.assertEqual(out["missing_session_files"], [])
        self.assertEqual(out["transcript_message_count"], 2)
        self.assertEqual(out["session_chunk_count"], 1)
        self.assertEqual(out["imported_count"], 1)
        self.assertEqual(out["skipped_duplicate_count"], 0)
        self.assertEqual(list_entries(self.paths, {"limit": 10, "offset": 0})["items"], [])

    def test_backfill_telegram_direct_command_imports_and_repeat_skips_duplicates(self) -> None:
        inbound = self.root / "telegram-messages-backfill-repeat.jsonl"
        sessions_root = self.root / "sessions-backfill-repeat"
        self._write_telegram_direct_import_fixture(inbound, sessions_root)
        trajectories_root = self.root / "trajectories-backfill-repeat"
        trajectories_root.mkdir(parents=True, exist_ok=True)
        self._write_trajectory_fixture(
            trajectories_root / "one.trajectory.jsonl",
            session_key="agent:main:telegram:default:direct:123456789",
            ts="2026-05-16T10:00:00Z",
            session_file=sessions_root / "run-1.jsonl",
            session_id="sess-1",
        )

        base_cmd = [
            sys.executable,
            "-m",
            "agent_diary.cli.main",
            "--json",
            "backfill-telegram-direct",
            "--inbound-path",
            str(inbound),
            "--sessions-root",
            str(sessions_root),
            "--trajectories-root",
            str(trajectories_root),
            "--session-key",
            "agent:main:telegram:default:direct:123456789",
            "--chat-id",
            "123456789",
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        first = subprocess.run(base_cmd, cwd=self.root, env=env, check=True, capture_output=True, text=True)
        second = subprocess.run(base_cmd, cwd=self.root, env=env, check=True, capture_output=True, text=True)
        first_out = json.loads(first.stdout)
        second_out = json.loads(second.stdout)
        self.assertEqual(first_out["imported_count"], 1)
        self.assertEqual(first_out["skipped_duplicate_count"], 0)
        self.assertEqual(second_out["imported_count"], 0)
        self.assertEqual(second_out["skipped_duplicate_count"], 1)

    def test_backfill_telegram_direct_command_scoping_excludes_non_matching_session_files(self) -> None:
        inbound = self.root / "telegram-messages-backfill-scope.jsonl"
        sessions_root = self.root / "sessions-backfill-scope"
        self._write_telegram_direct_import_fixture(inbound, sessions_root)
        extra_session = sessions_root / "run-2.jsonl"
        extra_session.write_text((sessions_root / "run-1.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
        trajectories_root = self.root / "trajectories-backfill-scope"
        trajectories_root.mkdir(parents=True, exist_ok=True)
        self._write_trajectory_fixture(
            trajectories_root / "one.trajectory.jsonl",
            session_key="agent:main:telegram:default:direct:123456789",
            ts="2026-05-16T10:00:00Z",
            session_file=sessions_root / "run-1.jsonl",
            session_id="sess-1",
        )
        self._write_trajectory_fixture(
            trajectories_root / "two.trajectory.jsonl",
            session_key="agent:main:telegram:default:direct:DIFFERENT",
            ts="2026-05-16T10:00:00Z",
            session_file=extra_session,
            session_id="sess-2",
        )

        cmd = [
            sys.executable,
            "-m",
            "agent_diary.cli.main",
            "--json",
            "backfill-telegram-direct",
            "--inbound-path",
            str(inbound),
            "--sessions-root",
            str(sessions_root),
            "--trajectories-root",
            str(trajectories_root),
            "--session-key",
            "agent:main:telegram:default:direct:123456789",
            "--chat-id",
            "123456789",
            "--dry-run",
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        completed = subprocess.run(cmd, cwd=self.root, env=env, check=True, capture_output=True, text=True)
        out = json.loads(completed.stdout)
        self.assertEqual(out["discovered_session_file_count"], 1)
        self.assertEqual(out["processed_session_file_count"], 1)
        self.assertEqual(len(out["items"]), 1)
        self.assertTrue(out["items"][0]["session_file"].endswith("run-1.jsonl"))

    def test_backfill_telegram_direct_command_reports_missing_session_files(self) -> None:
        inbound = self.root / "telegram-messages-backfill-missing.jsonl"
        sessions_root = self.root / "sessions-backfill-missing"
        self._write_telegram_direct_import_fixture(inbound, sessions_root)
        missing_path = sessions_root / "missing-run.jsonl"
        trajectories_root = self.root / "trajectories-backfill-missing"
        trajectories_root.mkdir(parents=True, exist_ok=True)
        self._write_trajectory_fixture(
            trajectories_root / "missing.trajectory.jsonl",
            session_key="agent:main:telegram:default:direct:123456789",
            ts="2026-05-16T10:00:00Z",
            session_file=missing_path,
            session_id="sess-missing",
        )

        cmd = [
            sys.executable,
            "-m",
            "agent_diary.cli.main",
            "--json",
            "backfill-telegram-direct",
            "--inbound-path",
            str(inbound),
            "--sessions-root",
            str(sessions_root),
            "--trajectories-root",
            str(trajectories_root),
            "--session-key",
            "agent:main:telegram:default:direct:123456789",
            "--chat-id",
            "123456789",
            "--dry-run",
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        completed = subprocess.run(cmd, cwd=self.root, env=env, check=True, capture_output=True, text=True)
        out = json.loads(completed.stdout)
        self.assertEqual(out["discovered_session_file_count"], 1)
        self.assertEqual(out["processed_session_file_count"], 0)
        self.assertEqual(len(out["missing_session_files"]), 1)
        self.assertTrue(out["missing_session_files"][0].endswith("missing-run.jsonl"))
        self.assertEqual(out["imported_count"], 0)
        self.assertEqual(out["skipped_duplicate_count"], 0)

    def test_import_openclaw_session_command_dry_run_does_not_write_raw_entries(self) -> None:
        source_file = self.root / "openclaw-session-dry-run.jsonl"
        self._write_openclaw_session_fixture(source_file)

        cmd = [
            sys.executable,
            "-m",
            "agent_diary.cli.main",
            "--json",
            "import-openclaw-session",
            "--input-path",
            str(source_file),
            "--source-session-id",
            "session-plain-1",
            "--source-conversation-id",
            "openclaw:session-plain-1",
            "--dry-run",
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        completed = subprocess.run(
            cmd,
            cwd=self.root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        out = json.loads(completed.stdout)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["resolved_source_session_id"], "session-plain-1")
        self.assertEqual(out["resolved_source_conversation_id"], "openclaw:session-plain-1")
        self.assertEqual(out["transcript_message_count"], 4)
        self.assertEqual(out["session_chunk_count"], 1)
        self.assertEqual(out["imported_count"], 1)
        self.assertEqual(out["skipped_duplicate_count"], 0)
        self.assertIsInstance(out["batch_manifest_path"], str)
        self.assertTrue(out["batch_manifest_path"].endswith(".json"))
        self.assertEqual(out["import_result"]["imported_count"], 1)
        self.assertEqual(out["import_result"]["skipped_count"], 0)
        self.assertEqual(list_entries(self.paths, {"limit": 10, "offset": 0})["items"], [])

    def test_import_openclaw_session_command_skips_duplicate_on_repeat_run(self) -> None:
        source_file = self.root / "openclaw-session-repeat.jsonl"
        self._write_openclaw_session_fixture(source_file)

        base_cmd = [
            sys.executable,
            "-m",
            "agent_diary.cli.main",
            "--json",
            "import-openclaw-session",
            "--input-path",
            str(source_file),
            "--source-session-id",
            "session-plain-1",
            "--source-conversation-id",
            "openclaw:session-plain-1",
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")

        first = subprocess.run(
            base_cmd,
            cwd=self.root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        first_out = json.loads(first.stdout)
        self.assertEqual(first_out["resolved_source_session_id"], "session-plain-1")
        self.assertEqual(first_out["resolved_source_conversation_id"], "openclaw:session-plain-1")
        self.assertEqual(first_out["transcript_message_count"], 4)
        self.assertEqual(first_out["session_chunk_count"], 1)
        self.assertEqual(first_out["imported_count"], 1)
        self.assertEqual(first_out["skipped_duplicate_count"], 0)
        self.assertEqual(first_out["import_result"]["imported_count"], 1)
        self.assertEqual(first_out["import_result"]["skipped_count"], 0)

        second = subprocess.run(
            base_cmd,
            cwd=self.root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        second_out = json.loads(second.stdout)
        self.assertEqual(second_out["resolved_source_session_id"], "session-plain-1")
        self.assertEqual(second_out["resolved_source_conversation_id"], "openclaw:session-plain-1")
        self.assertEqual(second_out["transcript_message_count"], 4)
        self.assertEqual(second_out["session_chunk_count"], 1)
        self.assertEqual(second_out["imported_count"], 0)
        self.assertEqual(second_out["skipped_duplicate_count"], 1)
        self.assertEqual(second_out["import_result"]["imported_count"], 0)
        self.assertEqual(second_out["import_result"]["skipped_count"], 1)

    def test_import_openclaw_session_command_infers_source_identifiers_when_omitted(self) -> None:
        source_file = self.root / "openclaw-session-infer.jsonl"
        self._write_openclaw_session_fixture(source_file)

        cmd = [
            sys.executable,
            "-m",
            "agent_diary.cli.main",
            "--json",
            "import-openclaw-session",
            "--input-path",
            str(source_file),
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        completed = subprocess.run(
            cmd,
            cwd=self.root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        out = json.loads(completed.stdout)
        self.assertEqual(out["resolved_source_session_id"], "session-plain-1")
        self.assertEqual(out["resolved_source_conversation_id"], "openclaw:session-plain-1")
        self.assertTrue(out["import_id"].startswith("import-openclaw-session-import-session-plain-1-"))
        self.assertIn("session=session-plain-1", out["import_label"])
        self.assertIn("conversation=openclaw:session-plain-1", out["import_label"])
        self.assertEqual(out["import_result"]["imported_count"], 1)
        self.assertEqual(out["import_result"]["skipped_count"], 0)

        entry_id = out["import_result"]["imported"][0]["entry_id"]
        fetched = fetch_raw_entry(self.paths, {"entry_id": entry_id})
        ingestion = fetched["entry"]["metadata"]["ingestion"]
        self.assertEqual(ingestion["source_session_id"], "session-plain-1")
        self.assertEqual(ingestion["source_conversation_id"], "openclaw:session-plain-1")

    def test_import_openclaw_session_command_explicit_ids_override_inferred_defaults(self) -> None:
        source_file = self.root / "openclaw-session-override.jsonl"
        self._write_openclaw_session_fixture(source_file)

        cmd = [
            sys.executable,
            "-m",
            "agent_diary.cli.main",
            "--json",
            "import-openclaw-session",
            "--input-path",
            str(source_file),
            "--source-session-id",
            "manual-session-override",
            "--source-conversation-id",
            "manual-conversation-override",
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd() / "src")
        completed = subprocess.run(
            cmd,
            cwd=self.root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        out = json.loads(completed.stdout)
        self.assertEqual(out["resolved_source_session_id"], "manual-session-override")
        self.assertEqual(out["resolved_source_conversation_id"], "manual-conversation-override")
        self.assertIn("session=manual-session-override", out["import_label"])
        self.assertIn("conversation=manual-conversation-override", out["import_label"])
        self.assertTrue(out["import_id"].startswith("import-openclaw-session-import-manual-session-override-"))

        entry_id = out["import_result"]["imported"][0]["entry_id"]
        fetched = fetch_raw_entry(self.paths, {"entry_id": entry_id})
        ingestion = fetched["entry"]["metadata"]["ingestion"]
        self.assertEqual(ingestion["source_session_id"], "manual-session-override")
        self.assertEqual(ingestion["source_conversation_id"], "manual-conversation-override")

    def test_list_imports_returns_recent_manifests_newest_first(self) -> None:
        import_session_jsonl(
            self.paths,
            {
                "path": str(self._write_session_import_fixture("manifest-older.jsonl", "older-1")),
                "import_id": "import-old",
                "source_session_id": "session-old",
                "source_conversation_id": "conv-old",
            },
        )
        import_session_jsonl(
            self.paths,
            {
                "path": str(self._write_session_import_fixture("manifest-newer.jsonl", "newer-1")),
                "import_id": "import-new",
                "source_session_id": "session-new",
                "source_conversation_id": "conv-new",
            },
        )

        result = list_imports(self.paths, {"limit": 10})
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["items"][0]["import_id"], "import-new")
        self.assertEqual(result["items"][1]["import_id"], "import-old")
        self.assertEqual(result["items"][0]["source_session_id"], "session-new")
        self.assertTrue(result["items"][0]["batch_manifest_path"].endswith("import-new.json"))
        self.assertEqual(result["items"][0]["audit"]["line_count"], 1)
        self.assertEqual(result["items"][0]["audit"]["source_counts"], {"openclaw-session-import": 1})

    def test_list_imports_honors_limit(self) -> None:
        import_session_jsonl(
            self.paths,
            {
                "path": str(self._write_session_import_fixture("manifest-a.jsonl", "a-1")),
                "import_id": "import-a",
                "source_session_id": "session-a",
                "source_conversation_id": "conv-a",
            },
        )
        import_session_jsonl(
            self.paths,
            {
                "path": str(self._write_session_import_fixture("manifest-b.jsonl", "b-1")),
                "import_id": "import-b",
                "source_session_id": "session-b",
                "source_conversation_id": "conv-b",
            },
        )

        result = list_imports(self.paths, {"limit": 1})
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(result["items"]), 1)

    def test_adapt_openclaw_session_jsonl_skips_synthetic_cron_prompt(self) -> None:
        session_path = self.root / "cronish.jsonl"
        output_path = self.root / "transcript.jsonl"
        session_path.write_text(
            "\n".join(
                [
                    json.dumps({"type": "session", "id": "sess-1", "timestamp": "2026-05-22T07:00:00Z", "cwd": "/tmp"}),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "m1",
                            "parentId": None,
                            "timestamp": "2026-05-22T07:00:00Z",
                            "message": {"role": "user", "content": "[cron:abc] Do a thing"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "m2",
                            "parentId": "m1",
                            "timestamp": "2026-05-22T07:00:05Z",
                            "message": {"role": "assistant", "content": [{"type": "text", "text": "Done"}]},
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        result = adapt_session_export(
            input_path=session_path,
            output_path=output_path,
            format_name="openclaw-session-jsonl",
        )

        self.assertEqual(result["message_count"], 1)
        rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(rows[0]["author_role"], "agent")
        self.assertEqual(rows[0]["content"], "Done")

    def test_extract_openclaw_work_trace_events_classifies_command_test_and_action(self) -> None:
        session_path = self.root / "work-trace-session.jsonl"
        self._write_openclaw_work_trace_fixture(session_path)

        result = extract_openclaw_work_trace_events(
            input_path=session_path,
            session_key="agent:main:telegram:default:direct:123456789",
        )

        self.assertEqual(result["event_count"], 3)
        event_types = [event["event_type"] for event in result["events"]]
        self.assertEqual(event_types, ["command", "test_run", "action"])
        self.assertEqual(result["events"][0]["details"]["tool_name"], "bash")
        self.assertEqual(result["events"][1]["details"]["tool_name"], "exec_command")
        self.assertEqual(result["events"][2]["details"]["tool_name"], "openclaw_browser")

    def test_import_openclaw_work_trace_writes_events_and_repeat_skips_duplicates(self) -> None:
        session_path = self.root / "work-trace-import.jsonl"
        self._write_openclaw_work_trace_fixture(session_path)

        first = import_openclaw_work_trace(
            self.paths,
            input_path=session_path,
            session_key="agent:main:telegram:default:direct:123456789",
        )
        second = import_openclaw_work_trace(
            self.paths,
            input_path=session_path,
            session_key="agent:main:telegram:default:direct:123456789",
        )

        self.assertEqual(first["discovered_event_count"], 3)
        self.assertEqual(first["imported_count"], 3)
        self.assertEqual(first["skipped_duplicate_count"], 0)
        self.assertEqual(second["imported_count"], 0)
        self.assertEqual(second["skipped_duplicate_count"], 3)

        listed = list_work_trace(self.paths, {"limit": 10, "offset": 0})
        self.assertEqual(len(listed["items"]), 3)

    def test_backfill_openclaw_work_trace_session_key_imports_discovered_session_files(self) -> None:
        trajectories = self.root / "trajectories-work-trace"
        trajectories.mkdir(parents=True, exist_ok=True)
        session_path = self.root / "work-trace-backfill.jsonl"
        self._write_openclaw_work_trace_fixture(session_path)
        (trajectories / "one.trajectory.jsonl").write_text(
            json.dumps(
                {
                    "type": "session.started",
                    "ts": "2026-05-29T10:00:00.000Z",
                    "sessionId": "session-work-1",
                    "sessionKey": "agent:main:telegram:default:direct:123456789",
                    "data": {"sessionFile": str(session_path.resolve()), "threadId": "thread-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        out = backfill_openclaw_work_trace_session_key(
            self.paths,
            trajectories_root=trajectories,
            session_key="agent:main:telegram:default:direct:123456789",
            since="2026-05-29",
            until="2026-05-30",
        )

        self.assertEqual(out["discovered_session_file_count"], 1)
        self.assertEqual(out["processed_session_file_count"], 1)
        self.assertEqual(out["discovered_event_count"], 3)
        self.assertEqual(out["imported_count"], 3)

    def test_discover_openclaw_session_files_filters_by_session_key_and_time(self) -> None:
        trajectories = self.root / "trajectories"
        trajectories.mkdir(parents=True, exist_ok=True)
        (trajectories / "a.trajectory.jsonl").write_text(
            json.dumps(
                {
                    "type": "session.started",
                    "ts": "2026-05-20T07:15:07.786Z",
                    "sessionId": "run-a",
                    "sessionKey": "agent:main:telegram:default:direct:123456789",
                    "data": {"sessionFile": str(self.root / "a.jsonl"), "threadId": "thread-a"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (trajectories / "b.trajectory.jsonl").write_text(
            json.dumps(
                {
                    "type": "session.started",
                    "ts": "2026-05-10T07:15:07.786Z",
                    "sessionId": "run-b",
                    "sessionKey": "agent:main:telegram:default:direct:other",
                    "data": {"sessionFile": str(self.root / "b.jsonl"), "threadId": "thread-b"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        discovered = discover_openclaw_session_files(
            trajectories_root=trajectories,
            session_key="agent:main:telegram:default:direct:123456789",
            since=datetime.fromisoformat("2026-05-19T00:00:00+00:00"),
            until=datetime.fromisoformat("2026-05-21T00:00:00+00:00"),
        )

        self.assertEqual(len(discovered), 1)
        self.assertEqual(discovered[0]["session_id"], "run-a")
        self.assertEqual(discovered[0]["thread_id"], "thread-a")

    def test_backfill_openclaw_session_key_dry_run_imports_matching_files(self) -> None:
        trajectories = self.root / "trajectories"
        trajectories.mkdir(parents=True, exist_ok=True)
        session_path = self.root / "turn-a.jsonl"
        session_path.write_text(
            "\n".join(
                [
                    json.dumps({"type": "session", "id": "sess-a", "timestamp": "2026-05-20T07:15:07.786Z", "cwd": "/tmp"}),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "u1",
                            "parentId": None,
                            "timestamp": "2026-05-20T07:15:08Z",
                            "message": {"role": "user", "content": "What happened on May 7th?"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "id": "a1",
                            "parentId": "u1",
                            "timestamp": "2026-05-20T07:15:10Z",
                            "message": {"role": "assistant", "content": [{"type": "text", "text": "Here is the rundown."}]},
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (trajectories / "turn-a.trajectory.jsonl").write_text(
            json.dumps(
                {
                    "type": "session.started",
                    "ts": "2026-05-20T07:15:07.786Z",
                    "sessionId": "run-a",
                    "sessionKey": "agent:main:telegram:default:direct:123456789",
                    "data": {"sessionFile": str(session_path), "threadId": "thread-a"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        out = backfill_openclaw_session_key(
            self.paths,
            trajectories_root=trajectories,
            session_key="agent:main:telegram:default:direct:123456789",
            source="telegram-direct-bootstrap",
            since="2026-05-19",
            until="2026-05-21",
            dry_run=True,
        )

        self.assertEqual(out["discovered_session_file_count"], 1)
        self.assertEqual(out["processed_session_file_count"], 1)
        self.assertEqual(out["transcript_message_count"], 2)
        self.assertEqual(out["session_chunk_count"], 1)
        self.assertEqual(out["imported_count"], 1)
        self.assertEqual(out["skipped_duplicate_count"], 0)

    def _write_session_import_fixture(self, filename: str, source_message_id: str) -> Path:
        source_file = self.root / filename
        source_file.write_text(
            json.dumps(
                {
                    "entry_type": "chat_log",
                    "source": "openclaw-session-import",
                    "author_role": "mixed",
                    "created_at": "2026-05-27T10:00:00+00:00",
                    "content": "User: hello\nAssistant: hi",
                    "metadata": {"source_message_id": source_message_id},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return source_file


if __name__ == "__main__":
    unittest.main()
