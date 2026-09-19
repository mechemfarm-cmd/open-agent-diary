from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    root: Path
    data_root: Path
    entries_dir: Path
    work_trace_dir: Path
    overlays_dir: Path
    artifacts_dir: Path
    imports_dir: Path
    index_dir: Path
    config_dir: Path
    archive_dir: Path
    sqlite_path: Path


def default_paths(root: Path | None = None) -> Paths:
    project_root = (root or Path.cwd()).resolve()
    configured_data_root = os.environ.get("AGENTDIARY_DATA") if root is None else None
    data_root = Path(configured_data_root).expanduser().resolve() if configured_data_root else project_root / "data"
    return Paths(
        root=project_root,
        data_root=data_root,
        entries_dir=data_root / "entries",
        work_trace_dir=data_root / "work_trace",
        overlays_dir=data_root / "overlays",
        artifacts_dir=data_root / "artifacts",
        imports_dir=data_root / "imports",
        index_dir=data_root / "index",
        config_dir=data_root / "config",
        archive_dir=data_root / "archives",
        sqlite_path=data_root / "index" / "memory.db",
    )
