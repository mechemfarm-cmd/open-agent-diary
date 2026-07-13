from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from agent_diary.config import Paths
from agent_diary.models.types import Artifact, Overlay, RawEntry, WorkTraceEvent
from agent_diary.storage.imports import ensure_import_dirs


def ensure_data_dirs(paths: Paths) -> None:
    for p in (
        paths.entries_dir,
        paths.work_trace_dir,
        paths.overlays_dir,
        paths.artifacts_dir,
        paths.imports_dir,
        paths.index_dir,
        paths.config_dir,
    ):
        p.mkdir(parents=True, exist_ok=True)
    ensure_import_dirs(paths)


def _entry_path(entries_dir: Path, entry_id: str, created_at: str) -> Path:
    dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return entries_dir / f"{dt:%Y}" / f"{dt:%m}" / f"{dt:%d}" / f"{entry_id}.json"


def append_raw_entry(paths: Paths, entry: RawEntry) -> Path:
    target = _entry_path(paths.entries_dir, entry.entry_id, entry.created_at)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Append-only intent: write once and fail if the path already exists.
    with target.open("x", encoding="utf-8") as f:
        f.write(json.dumps(entry.to_dict(), indent=2))
    return target


def append_work_trace_event(paths: Paths, event: WorkTraceEvent) -> Path:
    target = _entry_path(paths.work_trace_dir, event.event_id, event.created_at)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as f:
        f.write(json.dumps(event.to_dict(), indent=2))
    return target


def append_artifact(paths: Paths, artifact: Artifact) -> Path:
    parent = paths.artifacts_dir / artifact.entry_id
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / f"{artifact.artifact_id}.json"
    target.write_text(json.dumps(artifact.to_dict(), indent=2), encoding="utf-8")
    return target


def append_overlay(paths: Paths, overlay: Overlay) -> Path:
    parent = paths.overlays_dir / overlay.entry_id
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / f"{overlay.overlay_id}.json"
    target.write_text(json.dumps(overlay.to_dict(), indent=2), encoding="utf-8")
    return target
