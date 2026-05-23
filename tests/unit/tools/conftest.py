"""Tool tests use the full MCP tool surface."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def all_tool_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KICAD_MCP_TOOL_PROFILE", "all")
