from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_diary.config import Paths
from agent_diary.index.repository import get_entry_row, list_entry_rows


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def entry_row_to_body(row: dict[str, Any]) -> dict[str, Any]:
    effective = row.get("_effective_body")
    if isinstance(effective, dict):
        return effective
    return read_json(Path(row["raw_file_path"]))


def collect_source_rows(
    paths: Paths,
    *,
    limit: int,
    entry_ids: list[str] | None,
) -> list[dict[str, Any]]:
    if entry_ids:
        rows: list[dict[str, Any]] = []
        for entry_id in entry_ids:
            row = get_entry_row(paths.sqlite_path, entry_id)
            if row is not None:
                rows.append(row)
        rows.sort(key=lambda r: (r["created_at"], r["entry_id"]), reverse=True)
        return rows[:limit]
    return list_entry_rows(paths.sqlite_path, limit=limit, offset=0)


def entry_has_active_artifact_type(paths: Paths, *, entry_id: str, artifact_type: str) -> bool:
    artifact_dir = paths.artifacts_dir / entry_id
    if not artifact_dir.exists():
        return False

    for path in artifact_dir.glob("*.json"):
        body = read_json(path)
        if str(body.get("artifact_type", "")).strip() != artifact_type:
            continue
        metadata = body.get("metadata")
        if not isinstance(metadata, dict):
            return True
        lifecycle = str(metadata.get("lifecycle_status", "")).strip().lower()
        if lifecycle != "superseded":
            return True
    return False
