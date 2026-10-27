"""Metadata-only inventory for orientation onboarding.

The scanner never opens a file. It uses lstat/scandir metadata, does not follow
symlinked directories, and excludes common credential/private paths by default.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any

EXCLUDED_DIRECTORY_NAMES = {
    ".aws",
    ".cache",
    ".config",
    ".git",
    ".gnupg",
    ".hg",
    ".local",
    ".mozilla",
    ".password-store",
    ".ssh",
    ".svn",
}
EXCLUDED_FILE_NAMES = {
    ".bash_history",
    ".env",
    ".git-credentials",
    ".netrc",
}
EXCLUDED_FILE_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".kdbx"}


def _modified_at(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _excluded_file(name: str) -> bool:
    return name in EXCLUDED_FILE_NAMES or any(name.endswith(suffix) for suffix in EXCLUDED_FILE_SUFFIXES)


def inventory_metadata(root: Path, *, max_entries: int = 10_000) -> dict[str, Any]:
    """Inventory one directory without opening files or following symlinks."""
    if max_entries < 1:
        raise ValueError("max_entries must be >= 1")
    root = Path(root).expanduser()
    root_stat = root.lstat()
    if not root.is_dir() or root.is_symlink():
        raise ValueError("inventory root must be a real directory")

    items: list[dict[str, Any]] = []
    counts = {
        "files": 0,
        "directories": 1,
        "symlinks": 0,
        "excluded_files": 0,
        "excluded_directories": 0,
        "bytes": 0,
    }
    truncated = False
    stack: list[tuple[Path, str]] = [(root, "")]

    while stack:
        directory, relative_directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            relative_path = f"{relative_directory}/{name}".lstrip("/")
            if entry.is_dir(follow_symlinks=False):
                if name in EXCLUDED_DIRECTORY_NAMES:
                    counts["excluded_directories"] += 1
                    continue
                if len(items) >= max_entries:
                    truncated = True
                    break
                stat = entry.stat(follow_symlinks=False)
                counts["directories"] += 1
                items.append(
                    {
                        "relative_path": relative_path,
                        "type": "directory",
                        "size": 0,
                        "modified_at": _modified_at(stat.st_mtime),
                    }
                )
                stack.append((Path(entry.path), relative_path))
                continue

            if entry.is_symlink():
                if len(items) >= max_entries:
                    truncated = True
                    break
                stat = entry.stat(follow_symlinks=False)
                counts["symlinks"] += 1
                items.append(
                    {
                        "relative_path": relative_path,
                        "type": "symlink",
                        "size": 0,
                        "modified_at": _modified_at(stat.st_mtime),
                    }
                )
                continue

            if _excluded_file(name):
                counts["excluded_files"] += 1
                continue
            if len(items) >= max_entries:
                truncated = True
                break
            stat = entry.stat(follow_symlinks=False)
            size = int(stat.st_size)
            counts["files"] += 1
            counts["bytes"] += size
            items.append(
                {
                    "relative_path": relative_path,
                    "type": "file",
                    "size": size,
                    "modified_at": _modified_at(stat.st_mtime),
                }
            )
        if truncated:
            break

    items.sort(key=lambda item: item["relative_path"])
    return {
        "root": str(root.resolve()),
        "metadata_only": True,
        "truncated": truncated,
        "max_entries": max_entries,
        "counts": counts,
        "items": items,
    }
