"""Point an agent runtime at the diary so recall starts working.

Hermes is the only runtime we detect and write for today. Writing someone's
config is the most invasive thing onboarding does, so the rules are strict:

- Back up before touching anything, restore on any verification failure.
- Edit text rather than round-tripping the document, so comments, ordering and
  unrelated settings survive.
- Verify by re-reading and re-parsing what actually landed on disk.
- Idempotent: an entry that already matches produces no write at all.

This module deliberately does not import a YAML library. The project ships with
zero dependencies, and the shape edited here is narrow: one top-level key whose
children are two-space indented mappings. If the document does not match that
shape, connect refuses rather than guessing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import shutil
import urllib.error
import urllib.request

SERVER_NAME = "agent-diary"
DEFAULT_PORT = 8041


class ConnectError(RuntimeError):
    """Raised when the config cannot be written or verified."""


@dataclass
class ParsedEntry:
    found: bool = False
    parseable: bool = False
    command: str | None = None
    args: list[str] = field(default_factory=list)
    duplicates: int = 0


def mcp_script_path() -> Path:
    """The wrapper shipped at the repository root."""
    return Path(__file__).resolve().parents[2] / "agent-diary-mcp.py"


def hermes_config_path() -> Path:
    home = os.environ.get("HERMES_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".hermes"
    return base / "config.yaml"


def desired_entry() -> dict:
    return {"command": "python3", "args": [str(mcp_script_path())]}


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _scalar(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _parse_entry(text: str) -> ParsedEntry:
    """Read `mcp_servers -> agent-diary` from the narrow shape we support."""
    lines = text.splitlines()
    result = ParsedEntry()

    start = None
    for index, line in enumerate(lines):
        if line.startswith("mcp_servers:") and _indent(line) == 0:
            start = index
            break
    if start is None:
        return result

    inline = lines[start][len("mcp_servers:") :].strip()
    if inline not in {"", "{}", "null", "~"}:
        # Flow-style or otherwise unusual; we do not edit what we cannot read.
        raise ConnectError("mcp_servers is not in block form; refusing to edit it")

    # Children are the following lines indented deeper than the key.
    child_indent = None
    block: list[str] = []
    for line in lines[start + 1 :]:
        if not line.strip():
            block.append(line)
            continue
        if _indent(line) == 0:
            break
        if child_indent is None:
            child_indent = _indent(line)
        if _indent(line) < child_indent:
            break
        block.append(line)

    if child_indent is None:
        return result

    # Split children into server blocks keyed at child_indent.
    servers: list[tuple[str, list[str]]] = []
    current_name = None
    current: list[str] = []
    for line in block:
        if not line.strip():
            continue
        if _indent(line) == child_indent and line.rstrip().endswith(":"):
            if current_name is not None:
                servers.append((current_name, current))
            current_name = line.strip()[:-1].strip()
            current = []
        elif current_name is not None:
            current.append(line)
    if current_name is not None:
        servers.append((current_name, current))

    matches = [body for name, body in servers if name.strip("'\"") == SERVER_NAME]
    result.found = bool(matches)
    result.duplicates = len(matches)
    if len(matches) != 1:
        return result

    body = matches[0]
    body_indent = min((_indent(l) for l in body if l.strip()), default=child_indent + 2)
    args: list[str] = []
    in_args = False
    for line in body:
        stripped = line.strip()
        if _indent(line) == body_indent and stripped.startswith("command:"):
            result.command = _scalar(stripped[len("command:") :])
            in_args = False
        elif _indent(line) == body_indent and stripped.startswith("args:"):
            in_args = True
        elif in_args and stripped.startswith("- ") and _indent(line) > body_indent:
            args.append(_scalar(stripped[2:]))
        elif stripped and _indent(line) == body_indent:
            in_args = False
    result.args = args
    result.parseable = result.command is not None and bool(args)
    return result


def _entry_matches(text: str) -> bool:
    parsed = _parse_entry(text)
    want = desired_entry()
    return (
        parsed.found
        and parsed.duplicates == 1
        and parsed.command == want["command"]
        and parsed.args == want["args"]
    )


def _render_block(entry: dict) -> str:
    lines = [f"  {SERVER_NAME}:", f"    command: {entry['command']}", "    args:"]
    for arg in entry["args"]:
        lines.append(f"      - {json.dumps(arg)}")
    return "\n".join(lines) + "\n"


def _insert_entry(text: str) -> str:
    """Insert the server block without disturbing the rest of the document."""
    block = _render_block(desired_entry())
    lines = text.splitlines(keepends=True)

    for index, line in enumerate(lines):
        if not line.startswith("mcp_servers:") or _indent(line) != 0:
            continue
        inline = line[len("mcp_servers:") :].strip()
        if inline in {"{}", "null", "~"}:
            return "".join(lines[:index]) + "mcp_servers:\n" + block + "".join(lines[index + 1 :])
        return "".join(lines[: index + 1]) + block + "".join(lines[index + 1 :])

    separator = "" if text.endswith("\n") or not text else "\n"
    return f"{text}{separator}mcp_servers:\n{block}"


def _server_status(port: int = DEFAULT_PORT) -> dict:
    url = f"http://127.0.0.1:{port}/status"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            body = response.read(500).decode("utf-8", "replace")
            return {
                "server": "running",
                "url": url,
                "http_status": response.status,
                "body": body[:200],
            }
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        return {"server": "not_running", "url": url, "detail": str(exc)}


def connect_hermes(*, config_path: Path | str | None = None, dry_run: bool = False) -> dict:
    """Write the MCP entry for Hermes, verifying before and after the write."""
    path = Path(config_path).expanduser() if config_path else hermes_config_path()
    entry = desired_entry()

    if not mcp_script_path().is_file():
        raise ConnectError(f"MCP wrapper not found at {mcp_script_path()}")

    if not path.is_file():
        return {
            "status": "config_missing",
            "detail": (
                f"No config at {path}. Install the agent runtime first, or create the "
                "file and re-run connect."
            ),
            "config_path": str(path),
            "entry": entry,
            "http_equivalent": {
                "url": f"http://127.0.0.1:{DEFAULT_PORT}",
                "auth": "none (loopback)",
            },
            "needs_human": True,
        }

    original = path.read_text(encoding="utf-8")
    parsed = _parse_entry(original)

    if parsed.found and not parsed.parseable:
        # Something is there under our name but not in a shape we understand.
        # Refuse rather than insert a duplicate key.
        return {
            "status": "existing_entry_unrecognized",
            "detail": (
                f"mcp_servers already contains '{SERVER_NAME}', but not in a form this "
                "step can read. Edit it by hand using the entry below."
            ),
            "config_path": str(path),
            "entry": entry,
            "changed": False,
            "needs_human": True,
        }

    if _entry_matches(original):
        return {
            "status": "already_connected",
            "config_path": str(path),
            "entry": entry,
            "changed": False,
            "verification": _server_status(),
            "http_equivalent": {
                "url": f"http://127.0.0.1:{DEFAULT_PORT}",
                "auth": "none (loopback)",
            },
            "needs_human": False,
            "next": "done",
        }

    updated = _insert_entry(original)
    if not _entry_matches(updated):
        raise ConnectError("edited config does not contain the intended entry; refusing to write")

    if dry_run:
        return {
            "status": "would_connect",
            "config_path": str(path),
            "entry": entry,
            "changed": False,
            "dry_run": True,
            "verification": _server_status(),
        }

    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)

    try:
        path.write_text(updated, encoding="utf-8")
        if not _entry_matches(path.read_text(encoding="utf-8")):
            raise ConnectError("written config failed verification")
    except BaseException:
        shutil.copy2(backup, path)
        raise

    return {
        "status": "connected",
        "config_path": str(path),
        "backup": str(backup),
        "entry": entry,
        "changed": True,
        "verification": _server_status(),
        "http_equivalent": {"url": f"http://127.0.0.1:{DEFAULT_PORT}", "auth": "none (loopback)"},
        "needs_human": False,
        "next": "done",
        "note": "Restart the agent for the new MCP server to load.",
    }
