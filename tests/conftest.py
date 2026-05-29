"""Shared test configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.utils.path_validator import TRUSTED_ROOTS_ENV_VAR


@pytest.fixture(autouse=True)
def trust_test_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests create KiCad fixtures in pytest temp dirs; trust only that dir explicitly."""
    monkeypatch.setenv(TRUSTED_ROOTS_ENV_VAR, str(tmp_path))
