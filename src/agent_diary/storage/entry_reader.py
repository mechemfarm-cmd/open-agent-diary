from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_diary.config import Paths
from agent_diary.index.repository import get_entry_row
from agent_diary.storage.archiver import read_archive_entry


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _find_entry_file(paths: Paths, entry_id: str) -> Path | None:
    matches = list(paths.entries_dir.glob(f"**/{entry_id}.json"))
    return matches[0] if matches else None


def _try_read_archive(paths: Paths, entry_id: str, row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Try to read an entry from an archive when the standalone file is missing.

    Supports two lookup strategies:
      1. If the SQLite ``raw_file_path`` is an ``archive://`` URI, parse it directly.
      2. Otherwise, use the entry's ``created_at`` to derive the archive path.
    """
    archive_uri: str | None = None
    created_at: str | None = None

    if row:
        rfp = row.get("raw_file_path", "")
        if isinstance(rfp, str) and rfp.startswith("archive://"):
            archive_uri = rfp
        created_at = row.get("created_at")

    if archive_uri:
        # archive://path/to/archive.tar.gz#entries/YYYY/MM/DD/entry_id.json
        try:
            _, rest = archive_uri.split("archive://", 1)
            internal_path = rest.split("#", 1)[1] if "#" in rest else None
            return read_archive_entry(paths, entry_id, internal_path=internal_path)
        except (IndexError, ValueError):
            pass

    if created_at:
        return read_archive_entry(paths, entry_id, created_at=created_at)

    return None


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

    entry_data: dict[str, Any] | None = None

    if entry_file is not None and entry_file.exists():
        entry_data = _read_json(entry_file)
        entry_file_str = str(entry_file)
    else:
        # Try archive fallback
        entry_data = _try_read_archive(paths, entry_id, row)
        if entry_data is not None:
            entry_file_str = f"archive:{entry_id}"
        else:
            raise FileNotFoundError(f"entry not found: {entry_id}")

    result: dict[str, Any] = {
        "entry": entry_data,
        "entry_file": entry_file_str,
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
