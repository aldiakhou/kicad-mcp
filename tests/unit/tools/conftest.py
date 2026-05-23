"""Tool tests use the full advanced/debug MCP tool surface."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def advanced_tool_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KICAD_MCP_TOOL_PROFILE", "advanced")
