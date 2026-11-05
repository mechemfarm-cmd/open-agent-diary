"""Data-root resolution must be explicit and predictable.

A wrong data root is indistinguishable from lost memory, so the precedence
chain is tested in order: flag, then environment, then config file, then the
project-local default. A missing or corrupt config file must never raise.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_diary.config import config_file_path, resolve_data_root


def _write_config(path: Path, data_root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"data_root": str(data_root)}), encoding="utf-8")


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    """Point XDG_CONFIG_HOME at the temp dir so the real config is never read."""
    cfg_home = tmp_path / "cfg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.delenv("AGENTDIARY_DATA", raising=False)
    return cfg_home / "agent-diary" / "config.json"


def test_explicit_flag_beats_env(tmp_path, isolated_config, monkeypatch):
    monkeypatch.setenv("AGENTDIARY_DATA", str(tmp_path / "env"))
    _write_config(isolated_config, tmp_path / "file")
    got = resolve_data_root(explicit=tmp_path / "flag", cwd=tmp_path)
    assert got == (tmp_path / "flag").resolve()


def test_env_beats_config_file(tmp_path, isolated_config, monkeypatch):
    monkeypatch.setenv("AGENTDIARY_DATA", str(tmp_path / "env"))
    _write_config(isolated_config, tmp_path / "file")
    got = resolve_data_root(cwd=tmp_path)
    assert got == (tmp_path / "env").resolve()


def test_config_file_beats_default(tmp_path, isolated_config):
    _write_config(isolated_config, tmp_path / "file")
    got = resolve_data_root(cwd=tmp_path)
    assert got == (tmp_path / "file").resolve()


def test_default_when_nothing_set(tmp_path, isolated_config):
    got = resolve_data_root(cwd=tmp_path)
    assert got == (tmp_path / "data").resolve()


def test_missing_config_file_is_ignored(tmp_path, isolated_config):
    assert not isolated_config.exists()
    got = resolve_data_root(cwd=tmp_path)
    assert got == (tmp_path / "data").resolve()


def test_corrupt_config_file_is_ignored(tmp_path, isolated_config):
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text("{not json at all", encoding="utf-8")
    got = resolve_data_root(cwd=tmp_path)
    assert got == (tmp_path / "data").resolve()


def test_config_file_without_data_root_is_ignored(tmp_path, isolated_config):
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text(json.dumps({"something": "else"}), encoding="utf-8")
    got = resolve_data_root(cwd=tmp_path)
    assert got == (tmp_path / "data").resolve()


def test_tilde_is_expanded(tmp_path, isolated_config, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_config(isolated_config, Path("~/diary-data"))
    got = resolve_data_root(cwd=tmp_path)
    assert got == (tmp_path / "diary-data").resolve()


def test_config_file_path_honours_xdg(tmp_path, isolated_config):
    assert config_file_path() == isolated_config


def test_default_paths_ignores_user_config(tmp_path, isolated_config, monkeypatch):
    """Library callers must not be affected by ambient user configuration.

    Regression: letting default_paths() read the config file made the whole
    test suite depend on whether the developer happened to have one set.
    Precedence belongs in the CLI, which passes the resolved root in.
    """
    from agent_diary.config import default_paths

    _write_config(isolated_config, tmp_path / "configured")
    monkeypatch.chdir(tmp_path)
    paths = default_paths()
    assert paths.data_root == (tmp_path / "data").resolve()
    assert paths.data_root != (tmp_path / "configured").resolve()


def test_default_paths_still_honours_explicit_data_root(tmp_path, isolated_config):
    from agent_diary.config import default_paths

    _write_config(isolated_config, tmp_path / "configured")
    explicit = tmp_path / "chosen"
    paths = default_paths(data_root=explicit)
    assert paths.data_root == explicit.resolve()
