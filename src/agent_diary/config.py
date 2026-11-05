from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path


def config_file_path() -> Path:
    """Location of the per-user configuration file."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "agent-diary" / "config.json"


def _configured_data_root() -> Path | None:
    """Read data_root from the config file, treating any problem as absent."""
    try:
        payload = json.loads(config_file_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("data_root")
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).expanduser().resolve()


def resolve_data_root(explicit: Path | str | None = None, *, cwd: Path | None = None) -> Path:
    """Resolve the data root in strict precedence order.

    explicit argument, then AGENTDIARY_DATA, then the config file, then
    <cwd>/data. A missing or malformed config file is ignored rather than
    raising: losing memory to a typo in a JSON file would be worse than
    falling back to the documented default.
    """
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    from_env = os.environ.get("AGENTDIARY_DATA")
    if from_env:
        return Path(from_env).expanduser().resolve()
    configured = _configured_data_root()
    if configured is not None:
        return configured
    base = Path(cwd) if cwd is not None else Path.cwd()
    return base.resolve() / "data"


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


def default_paths(root: Path | None = None, data_root: Path | None = None) -> Paths:
    project_root = (root or Path.cwd()).resolve()
    if data_root is not None:
        resolved = Path(data_root).expanduser().resolve()
    elif root is None:
        resolved = resolve_data_root(cwd=project_root)
    else:
        resolved = project_root / "data"
    return Paths(
        root=project_root,
        data_root=resolved,
        entries_dir=resolved / "entries",
        work_trace_dir=resolved / "work_trace",
        overlays_dir=resolved / "overlays",
        artifacts_dir=resolved / "artifacts",
        imports_dir=resolved / "imports",
        index_dir=resolved / "index",
        config_dir=resolved / "config",
        archive_dir=resolved / "archives",
        sqlite_path=resolved / "index" / "memory.db",
    )
