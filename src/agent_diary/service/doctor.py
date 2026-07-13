from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from agent_diary.config import Paths
from agent_diary.index.repository import connect_sqlite


_REQUIRED_TABLES = {
    "entries",
    "work_trace_events",
    "artifacts",
    "memory_index",
    "work_trace_entry_links",
    "memory_index_fts",
    "work_trace_fts",
}


def _limited_append(issues: list[dict[str, Any]], issue: dict[str, Any], max_issues: int) -> None:
    if len(issues) < max_issues:
        issues.append(issue)


def _json_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*.json") if path.is_file())


def _load_json(path: Path, issues: list[dict[str, Any]], max_issues: int) -> dict[str, Any] | None:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - exact JSON exception type is not important for users.
        _limited_append(
            issues,
            {"severity": "error", "code": "invalid_json", "path": str(path), "message": str(exc)},
            max_issues,
        )
        return None
    if not isinstance(body, dict):
        _limited_append(
            issues,
            {"severity": "error", "code": "invalid_json_shape", "path": str(path), "message": "JSON file must contain an object"},
            max_issues,
        )
        return None
    return body


def run_doctor(paths: Paths, *, max_issues: int = 100) -> dict[str, Any]:
    """Read-only consistency checks for release readiness and local data health."""
    issues: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []

    expected_dirs = [
        paths.data_root,
        paths.entries_dir,
        paths.work_trace_dir,
        paths.overlays_dir,
        paths.artifacts_dir,
        paths.imports_dir,
        paths.index_dir,
        paths.config_dir,
    ]
    missing_dirs = [str(path) for path in expected_dirs if not path.is_dir()]
    checks.append({"name": "data_directories", "ok": not missing_dirs, "missing": missing_dirs})
    for path in missing_dirs:
        _limited_append(issues, {"severity": "error", "code": "missing_directory", "path": path}, max_issues)

    if not paths.sqlite_path.exists():
        checks.append({"name": "sqlite_database", "ok": False, "path": str(paths.sqlite_path)})
        _limited_append(issues, {"severity": "error", "code": "missing_sqlite_database", "path": str(paths.sqlite_path)}, max_issues)
        return {"ok": False, "summary": {"issue_count": len(issues)}, "checks": checks, "issues": issues}

    with closing(connect_sqlite(paths.sqlite_path)) as conn:
        conn.row_factory = sqlite3.Row
        table_names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')").fetchall()
        }
        missing_tables = sorted(_REQUIRED_TABLES - table_names)
        checks.append({"name": "sqlite_schema", "ok": not missing_tables, "missing_tables": missing_tables})
        for table in missing_tables:
            _limited_append(issues, {"severity": "error", "code": "missing_table", "table": table}, max_issues)

        entry_rows = [dict(row) for row in conn.execute("SELECT entry_id, raw_file_path FROM entries").fetchall()]
        work_rows = [dict(row) for row in conn.execute("SELECT event_id, work_file_path FROM work_trace_events").fetchall()]
        artifact_rows = [dict(row) for row in conn.execute("SELECT artifact_id, entry_id FROM artifacts").fetchall()]
        memory_count = conn.execute("SELECT COUNT(*) FROM memory_index").fetchone()[0]
        memory_fts_count = conn.execute("SELECT COUNT(*) FROM memory_index_fts").fetchone()[0] if "memory_index_fts" in table_names else 0
        work_fts_count = conn.execute("SELECT COUNT(*) FROM work_trace_fts").fetchone()[0] if "work_trace_fts" in table_names else 0
        bad_artifact_refs = [dict(row) for row in conn.execute(
            """
            SELECT artifact_id, entry_id
            FROM artifacts
            WHERE entry_id NOT IN (SELECT entry_id FROM entries)
            """
        ).fetchall()]
        bad_work_links = [dict(row) for row in conn.execute(
            """
            SELECT event_id, entry_id
            FROM work_trace_entry_links
            WHERE entry_id NOT IN (SELECT entry_id FROM entries)
               OR event_id NOT IN (SELECT event_id FROM work_trace_events)
            """
        ).fetchall()]

    entry_ids = {str(row["entry_id"]) for row in entry_rows}
    work_ids = {str(row["event_id"]) for row in work_rows}
    missing_entry_files: list[str] = []
    mismatched_entry_files: list[str] = []
    for row in entry_rows:
        path = Path(str(row["raw_file_path"]))
        if not path.exists():
            missing_entry_files.append(str(path))
            _limited_append(issues, {"severity": "error", "code": "missing_entry_file", "entry_id": row["entry_id"], "path": str(path)}, max_issues)
            continue
        body = _load_json(path, issues, max_issues)
        if body is not None and body.get("entry_id") != row["entry_id"]:
            mismatched_entry_files.append(str(path))
            _limited_append(issues, {"severity": "error", "code": "entry_file_id_mismatch", "entry_id": row["entry_id"], "path": str(path)}, max_issues)

    missing_work_files: list[str] = []
    mismatched_work_files: list[str] = []
    for row in work_rows:
        path = Path(str(row["work_file_path"]))
        if not path.exists():
            missing_work_files.append(str(path))
            _limited_append(issues, {"severity": "error", "code": "missing_work_trace_file", "event_id": row["event_id"], "path": str(path)}, max_issues)
            continue
        body = _load_json(path, issues, max_issues)
        if body is not None and body.get("event_id") != row["event_id"]:
            mismatched_work_files.append(str(path))
            _limited_append(issues, {"severity": "error", "code": "work_trace_file_id_mismatch", "event_id": row["event_id"], "path": str(path)}, max_issues)

    orphan_entry_files: list[str] = []
    for path in _json_files(paths.entries_dir):
        body = _load_json(path, issues, max_issues)
        if body is not None and str(body.get("entry_id", "")) not in entry_ids:
            orphan_entry_files.append(str(path))
            _limited_append(issues, {"severity": "warning", "code": "entry_file_not_indexed", "path": str(path), "entry_id": body.get("entry_id")}, max_issues)

    orphan_work_files: list[str] = []
    for path in _json_files(paths.work_trace_dir):
        body = _load_json(path, issues, max_issues)
        if body is not None and str(body.get("event_id", "")) not in work_ids:
            orphan_work_files.append(str(path))
            _limited_append(issues, {"severity": "warning", "code": "work_trace_file_not_indexed", "path": str(path), "event_id": body.get("event_id")}, max_issues)

    for row in bad_artifact_refs:
        _limited_append(issues, {"severity": "error", "code": "artifact_entry_missing", **row}, max_issues)
    for row in bad_work_links:
        _limited_append(issues, {"severity": "error", "code": "work_trace_link_missing_target", **row}, max_issues)

    checks.extend(
        [
            {"name": "entry_index_files", "ok": not missing_entry_files and not mismatched_entry_files, "missing_count": len(missing_entry_files), "mismatch_count": len(mismatched_entry_files)},
            {"name": "work_trace_index_files", "ok": not missing_work_files and not mismatched_work_files, "missing_count": len(missing_work_files), "mismatch_count": len(mismatched_work_files)},
            {"name": "orphan_entry_files", "ok": not orphan_entry_files, "warning_count": len(orphan_entry_files)},
            {"name": "orphan_work_trace_files", "ok": not orphan_work_files, "warning_count": len(orphan_work_files)},
            {"name": "artifact_references", "ok": not bad_artifact_refs, "bad_reference_count": len(bad_artifact_refs)},
            {"name": "work_trace_entry_links", "ok": not bad_work_links, "bad_reference_count": len(bad_work_links)},
            {"name": "memory_fts_count", "ok": memory_count == memory_fts_count, "memory_index_count": memory_count, "fts_count": memory_fts_count},
            {"name": "work_trace_fts_count", "ok": len(work_rows) == work_fts_count, "work_trace_count": len(work_rows), "fts_count": work_fts_count},
        ]
    )
    if memory_count != memory_fts_count:
        _limited_append(issues, {"severity": "error", "code": "memory_fts_count_mismatch", "memory_index_count": memory_count, "fts_count": memory_fts_count}, max_issues)
    if len(work_rows) != work_fts_count:
        _limited_append(issues, {"severity": "error", "code": "work_trace_fts_count_mismatch", "work_trace_count": len(work_rows), "fts_count": work_fts_count}, max_issues)

    error_count = sum(1 for issue in issues if issue.get("severity") == "error")
    warning_count = sum(1 for issue in issues if issue.get("severity") == "warning")
    return {
        "ok": error_count == 0,
        "summary": {
            "entry_count": len(entry_rows),
            "work_trace_count": len(work_rows),
            "artifact_count": len(artifact_rows),
            "memory_index_count": memory_count,
            "issue_count": len(issues),
            "error_count": error_count,
            "warning_count": warning_count,
            "issues_truncated": len(issues) >= max_issues,
        },
        "checks": checks,
        "issues": issues,
    }
