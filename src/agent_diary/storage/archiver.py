"""Transparent archival of old entry data into compressed tar.gz bundles.

Months that exceed configured thresholds are packed into a single archive
file under ``data/archives/YYYY-MM.tar.gz``.  The entry reader transparently
falls back to these archives when a standalone file no longer exists, so
search and retrieval keep working without any schema changes.
"""

from __future__ import annotations

import json
import tarfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_diary.config import Paths
from agent_diary.index.repository import connect_sqlite


# ── Config ----------------------------------------------------------------

@dataclass(frozen=True)
class ArchiveConfig:
    """Thresholds that trigger archival of a calendar month directory.

    A month is archived only after it is old enough *and* exceeds at least
    one size pressure threshold: total bytes or file count.
    """

    # Minimum age of the *last-modified* file in the month before archiving.
    after_days: int = 60

    # Maximum total file size (in bytes) before archiving.
    max_bytes: int = 10 * 1024 * 1024  # 10 MiB

    # Maximum file count before archiving.
    max_files: int = 500

    # If True, the archive command runs in dry-run / report-only mode.
    dry_run: bool = False


# ── Helpers ---------------------------------------------------------------

def _archive_path(paths: Paths, year: int, month: int) -> Path:
    return paths.archive_dir / f"{year:04d}-{month:02d}.tar.gz"


def _internal_entry_path(entry_id: str, created_at: str) -> str:
    """Return the path *inside* the archive for a given entry."""
    dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return f"entries/{dt:%Y/%m/%d}/{entry_id}.json"


def _parse_created_at(fallback_path: Path) -> str | None:
    """Try to extract ``created_at`` from a JSON entry file without loading
    the full content into a structured object."""
    try:
        raw = fallback_path.read_bytes()
        # Simple scan for "created_at" — avoids full JSON parse overhead.
        marker = b'"created_at"'
        idx = raw.find(marker)
        if idx < 0:
            return None
        # move past the marker and colon
        chunk = raw[idx + len(marker) :].lstrip(b": \t\"'")
        end = chunk.find(b'"')
        if end < 0:
            return None
        return chunk[:end].decode("utf-8")
    except Exception:
        return None


def _month_dir(paths: Paths, year: int, month: int) -> Path:
    return paths.entries_dir / f"{year:04d}" / f"{month:02d}"


def _list_candidate_months(paths: Paths) -> list[tuple[int, int]]:
    """Return ``(year, month)`` tuples for every month directory under
    ``data/entries/`` that has at least one JSON file."""
    candidates: list[tuple[int, int]] = []
    if not paths.entries_dir.exists():
        return candidates
    for year_dir in sorted(paths.entries_dir.iterdir()):
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        year = int(year_dir.name)
        for month_dir in sorted(year_dir.iterdir()):
            if not month_dir.is_dir() or not month_dir.name.isdigit():
                continue
            month = int(month_dir.name)
            # Ensure at least one JSON file
            if any(month_dir.rglob("*.json")):
                candidates.append((year, month))
    return candidates


def _month_stats(paths: Paths, year: int, month: int) -> dict[str, Any]:
    """Return size and file-count stats for a month directory."""
    root = _month_dir(paths, year, month)
    files = list(root.rglob("*.json"))
    total_bytes = sum(f.stat().st_size for f in files if f.is_file())
    return {
        "file_count": len(files),
        "total_bytes": total_bytes,
        "newest_mtime": max(
            (f.stat().st_mtime for f in files if f.is_file()),
            default=0,
        ),
    }


def _should_archive(stats: dict[str, Any], cfg: ArchiveConfig) -> bool:
    """Return True when the month meets all configured thresholds."""
    age_seconds = time.time() - stats["newest_mtime"]
    age_days = age_seconds / 86400
    if age_days < cfg.after_days:
        return False
    if stats["file_count"] < cfg.max_files and stats["total_bytes"] < cfg.max_bytes:
        return False  # under both thresholds → skip
    return True


# ── Archive one month -----------------------------------------------------

def archive_month(
    paths: Paths,
    year: int,
    month: int,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Pack all JSON entry files for *year*/*month* into a single archive
    and remove the originals.

    Returns a dict with the operation result.
    """
    src = _month_dir(paths, year, month)
    if not src.exists():
        return {"ok": False, "error": f"directory not found: {src}"}

    files = sorted(src.rglob("*.json"))
    if not files:
        return {"ok": False, "error": "no JSON files found in month directory"}

    dest = _archive_path(paths, year, month)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "archive": str(dest),
            "file_count": len(files),
            "total_bytes": sum(f.stat().st_size for f in files if f.is_file()),
        }

    # Build archive
    with tarfile.open(dest, "w:gz") as tar:
        for entry_file in files:
            # Preserve the relative path inside the archive
            arcname = str(entry_file.relative_to(paths.data_root))
            tar.add(entry_file, arcname=arcname)

    # Verify archive was written
    if not dest.exists() or dest.stat().st_size == 0:
        return {"ok": False, "error": "archive creation failed — file is empty or missing"}

    # Update SQLite raw_file_path to point to the archive
    _update_raw_file_paths(paths, files, year, month)

    # Remove originals
    removed = 0
    for entry_file in files:
        try:
            entry_file.unlink()
            removed += 1
        except OSError:
            pass

    # Clean up empty parent directories
    _remove_empty_parents(src, stop_at=paths.entries_dir)

    return {
        "ok": True,
        "archive": str(dest),
        "archive_bytes": dest.stat().st_size,
        "file_count": len(files),
        "removed": removed,
    }


def _update_raw_file_paths(
    paths: Paths,
    files: list[Path],
    year: int,
    month: int,
) -> None:
    """Update SQLite ``raw_file_path`` entries for the given files so they
    point to the archive URI instead of the (now-deleted) standalone path."""
    archive_uri = f"archive://{_archive_path(paths, year, month)}"
    import sqlite3

    with connect_sqlite(paths.sqlite_path) as conn:
        for entry_file in files:
            entry_id = entry_file.stem  # filename without .json
            new_path = f"{archive_uri}#{entry_file.relative_to(paths.data_root)}"
            conn.execute(
                "UPDATE entries SET raw_file_path = ? WHERE entry_id = ?",
                (new_path, entry_id),
            )
        conn.commit()


def _remove_empty_parents(path: Path, *, stop_at: Path | None = None) -> None:
    """Walk upward from *path* and remove empty dirs, stopping before *stop_at*."""
    stop = stop_at.resolve() if stop_at is not None else None
    for parent in [path] + list(path.parents):
        resolved = parent.resolve()
        if stop is not None and resolved == stop:
            break
        if parent == path.root:
            break
        try:
            parent.rmdir()
        except OSError:
            break  # not empty or permission denied


# ── Archive all stale months ----------------------------------------------

def archive_stale_entries(
    paths: Paths,
    cfg: ArchiveConfig,
) -> list[dict[str, Any]]:
    """Find all months that meet the archive thresholds and pack them.

    Returns a list of result dicts, one per month processed.
    """
    results: list[dict[str, Any]] = []
    for year, month in _list_candidate_months(paths):
        # Skip if already archived
        dest = _archive_path(paths, year, month)
        if dest.exists():
            continue

        stats = _month_stats(paths, year, month)
        if not _should_archive(stats, cfg):
            continue

        result = archive_month(paths, year, month, dry_run=cfg.dry_run)
        result["year"] = year
        result["month"] = month
        result["stats"] = stats
        results.append(result)

    return results


# ── Read entry from archive -----------------------------------------------

def read_archive_entry(
    paths: Paths,
    entry_id: str,
    created_at: str | None = None,
    internal_path: str | None = None,
) -> dict[str, Any] | None:
    """Read a single entry from the appropriate archive.

    Either *internal_path* (the archive-internal path like
    ``entries/2026/01/01/entry_id.json``) or *created_at* + *entry_id* must
    be provided to locate the entry inside the archive.

    Returns the parsed JSON dict, or ``None`` if the entry could not be
    found in any archive.
    """
    if internal_path:
        arc_path = internal_path
    elif created_at:
        arc_path = _internal_entry_path(entry_id, created_at)
    else:
        return None

    # Derive archive file from the entry path
    # internal path looks like: entries/YYYY/MM/DD/entry_id.json
    parts = arc_path.replace("\\", "/").split("/")
    if len(parts) >= 3 and parts[0] == "entries":
        try:
            year = int(parts[1])
            month = int(parts[2])
        except (ValueError, IndexError):
            return None
        archive_file = _archive_path(paths, year, month)
    else:
        return None

    if not archive_file.exists():
        return None

    try:
        with tarfile.open(archive_file, "r:gz") as tar:
            member = tar.getmember(arc_path)
            if member is None:
                return None
            f = tar.extractfile(member)
            if f is None:
                return None
            content = f.read().decode("utf-8")
            return json.loads(content)
    except (KeyError, tarfile.TarError, json.JSONDecodeError, OSError):
        return None


# ── Report ----------------------------------------------------------------

def archive_report(paths: Paths) -> dict[str, Any]:
    """Return a summary of existing archives and candidate months."""
    existing: list[dict[str, Any]] = []
    if paths.archive_dir.exists():
        for arc in sorted(paths.archive_dir.glob("*.tar.gz")):
            try:
                size = arc.stat().st_size
                with tarfile.open(arc, "r:gz") as tar:
                    member_count = len(tar.getmembers())
                existing.append({
                    "archive": str(arc),
                    "bytes": size,
                    "member_count": member_count,
                })
            except (tarfile.TarError, OSError):
                existing.append({"archive": str(arc), "error": "could not read"})

    candidates = []
    for year, month in _list_candidate_months(paths):
        dest = _archive_path(paths, year, month)
        stats = _month_stats(paths, year, month)
        candidates.append({
            "year": year,
            "month": month,
            "file_count": stats["file_count"],
            "total_bytes": stats["total_bytes"],
            "already_archived": dest.exists(),
        })

    return {
        "ok": True,
        "existing_archives": existing,
        "candidate_months": candidates,
    }