"""Suite-wide isolation.

The test suite must not depend on the machine it runs on. `AGENTDIARY_DATA`
is read by `default_paths()` when no root is passed, so an ambient value — set
by a real service on the host, or inherited from a shell — makes older
subprocess-based tests read a live diary instead of their temporary roots.
That produced 13 failures on a machine where it happened to be exported.

Deleting it per test makes both the in-process and the subprocess paths see a
clean environment. Tests that need a root pass one explicitly, which is what
every test should be doing anyway.
"""
from __future__ import annotations

import pytest

_AMBIENT_DIARY_ENV = ("AGENTDIARY_DATA",)


@pytest.fixture(autouse=True)
def _isolate_agentdiary_env(monkeypatch):
    for name in _AMBIENT_DIARY_ENV:
        monkeypatch.delenv(name, raising=False)
    yield
