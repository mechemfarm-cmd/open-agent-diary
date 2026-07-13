from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_diary.config import Paths
from agent_diary.index.repository import get_entry_row


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _find_entry_file(paths: Paths, entry_id: str) -> Path | None:
    matches = list(paths.entries_dir.glob(f"**/{entry_id}.json"))
    return matches[0] if matches else None


def fetch_raw_entry(
    paths: Paths,
    entry_id: str,
    include_overlays: bool = False,
    include_artifacts: bool = False,
) -> dict[str, Any]:
    entry_file: Path | None = None
    row = get_entry_row(paths.sqlite_path, entry_id)
    if row:
        entry_file = Path(row["raw_file_path"])

    if entry_file is None:
        entry_file = _find_entry_file(paths, entry_id)

    if entry_file is None:
        raise FileNotFoundError(f"entry not found: {entry_id}")

    result: dict[str, Any] = {
        "entry": _read_json(entry_file),
        "entry_file": str(entry_file),
    }

    if include_overlays:
        overlay_dir = paths.overlays_dir / entry_id
        overlay_files = sorted(overlay_dir.glob("*.json")) if overlay_dir.exists() else []
        result["overlays"] = [_read_json(p) for p in overlay_files]

    if include_artifacts:
        artifact_dir = paths.artifacts_dir / entry_id
        artifact_files = sorted(artifact_dir.glob("*.json")) if artifact_dir.exists() else []
        result["artifacts"] = [_read_json(p) for p in artifact_files]

    return result
