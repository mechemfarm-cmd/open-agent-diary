"""Connect step: write the agent's MCP entry, safely.

The tests use real config text, including comments, because the failure mode
that matters is a write that silently destroys the rest of someone's file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_diary.onboarding_connect import (
    ConnectError,
    _entry_matches,
    _parse_entry,
    connect_hermes,
    desired_entry,
)

BASE = """# my hermes config
model: deepseek/deepseek-v4.1-flash
mcp_servers:
  other-server:
    command: node
    args:
      - /opt/other.js

agent:
  name: test
"""


def test_adds_entry_and_preserves_everything_else(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(BASE)

    out = connect_hermes(config_path=config)
    assert out["status"] == "connected"
    assert out["changed"] is True
    assert Path(out["backup"]).exists()

    text = config.read_text()
    assert _entry_matches(text)
    # Comment and unrelated sections survive.
    assert "# my hermes config" in text
    assert "other-server" in text
    assert "model: deepseek" in text
    assert "agent:" in text
    # The original entry is untouched and still parses as the other server.
    assert text.count("agent-diary:") == 1


def test_is_idempotent(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(BASE)

    first = connect_hermes(config_path=config)
    assert first["status"] == "connected"
    after_first = config.read_text()

    second = connect_hermes(config_path=config)
    assert second["status"] == "already_connected"
    assert second["changed"] is False
    assert config.read_text() == after_first
    # No second backup was taken.
    assert len(list(tmp_path.glob("config.yaml.bak-*"))) == 1


def test_empty_flow_mapping_is_replaced(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("model: x\nmcp_servers: {}\n")

    out = connect_hermes(config_path=config)
    assert out["status"] == "connected"
    assert _entry_matches(config.read_text())
    assert "mcp_servers: {}" not in config.read_text()


def test_missing_key_is_appended(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("model: x\n")

    out = connect_hermes(config_path=config)
    assert out["status"] == "connected"
    assert _entry_matches(config.read_text())
    assert config.read_text().startswith("model: x")


def test_unrecognized_existing_entry_is_refused_not_duplicated(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "mcp_servers:\n  agent-diary:\n    transport: http\n    url: http://x\n"
    )
    before = config.read_text()

    out = connect_hermes(config_path=config)
    assert out["status"] == "existing_entry_unrecognized"
    assert out["changed"] is False
    assert config.read_text() == before  # nothing written


def test_missing_config_is_reported_not_created(tmp_path):
    out = connect_hermes(config_path=tmp_path / "nope.yaml")
    assert out["status"] == "config_missing"
    assert not (tmp_path / "nope.yaml").exists()


def test_dry_run_writes_nothing(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(BASE)
    before = config.read_text()

    out = connect_hermes(config_path=config, dry_run=True)
    assert out["status"] == "would_connect"
    assert config.read_text() == before
    assert not list(tmp_path.glob("config.yaml.bak-*"))


def test_parser_reads_a_matching_entry(tmp_path):
    entry = desired_entry()
    config = tmp_path / "config.yaml"
    config.write_text(
        "mcp_servers:\n"
        "  agent-diary:\n"
        f"    command: {entry['command']}\n"
        "    args:\n"
        f"      - \"{entry['args'][0]}\"\n"
    )
    parsed = _parse_entry(config.read_text())
    assert parsed.found and parsed.parseable
    assert parsed.command == entry["command"]
    assert parsed.args == entry["args"]
    assert _entry_matches(config.read_text())


def test_flow_style_mcp_servers_is_not_edited(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("mcp_servers: {a: {command: b}}\n")
    before = config.read_text()
    with pytest.raises(ConnectError):
        connect_hermes(config_path=config)
    assert config.read_text() == before


def test_wrapper_resolves_to_a_real_file():
    """The wrapper must be findable in an installed layout, not just a checkout."""
    from agent_diary.onboarding_connect import mcp_script_path

    path = mcp_script_path()
    assert path.is_file(), path
    assert path.name == "agent-diary-mcp.py"


def test_missing_wrapper_is_reported_not_raised(tmp_path, monkeypatch):
    """A pip install without the wrapper must produce a status, not a crash."""
    import agent_diary.onboarding_connect as oc

    config = tmp_path / "config.yaml"
    config.write_text("model: x\n")
    monkeypatch.setattr(oc, "mcp_script_path", lambda: tmp_path / "does-not-exist.py")

    out = oc.connect_hermes(config_path=config)
    assert out["status"] == "mcp_wrapper_missing"
    assert out["needs_human"] is True
    assert out["changed"] is False
    assert config.read_text() == "model: x\n"


def test_shipped_wrapper_matches_the_repository_copy():
    """Two copies of the wrapper must not drift.

    The checkout copy and the packaged copy are the same script; a divergence
    is how a fix reaches one install path and not the other.
    """
    from pathlib import Path

    packaged = Path(__file__).resolve().parents[1] / "src" / "agent_diary" / "agent-diary-mcp.py"
    root = Path(__file__).resolve().parents[1] / "agent-diary-mcp.py"
    assert packaged.read_text() == root.read_text()
