"""Tests for KiCad CLI DRC report parsing."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from kicad_mcp.tools.creation_tools import _run_pcb_drc_sync
from kicad_mcp.tools.drc_impl.cli_drc import _drc_report_violations, run_drc_via_cli


def test_drc_report_violations_include_unconnected_items():
    report = {
        "violations": [{"type": "clearance", "severity": "error"}],
        "unconnected_items": [{"net": "USB_D+", "pos": {"x": 1, "y": 2}}],
    }

    violations = _drc_report_violations(report)

    assert len(violations) == 2
    assert violations[1]["type"] == "unconnected_item"
    assert violations[1]["severity"] == "warning"


@pytest.mark.asyncio
async def test_run_drc_via_cli_parses_report_even_with_nonzero_exit(
    monkeypatch,
    tmp_path: Path,
):
    board = tmp_path / "board.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")

    def fake_run(cmd, capture_output=True, text=True, timeout=None, **kwargs):
        output_path = Path(cmd[cmd.index("--output") + 1])
        output_path.write_text(
            json.dumps({"unconnected_items": [{"net": "NET_A"}], "violations": []}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, 5, "", "DRC found issues")

    monkeypatch.setattr(
        "kicad_mcp.utils.secure_subprocess.get_kicad_cli_path",
        lambda required=True: str(tmp_path / "kicad-cli.exe"),
    )
    monkeypatch.setattr("kicad_mcp.utils.secure_subprocess.subprocess.run", fake_run)

    result = await run_drc_via_cli(str(board), None, timeout_seconds=12)

    assert result["success"] is True
    assert result["total_violations"] == 1
    assert result["violation_categories"] == {"unconnected_item": 1}


def test_sync_pcb_drc_parses_unconnected_items(monkeypatch, tmp_path: Path):
    board = tmp_path / "board.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")

    def fake_run(cmd, capture_output=True, text=True, timeout=None, **kwargs):
        output_path = Path(cmd[cmd.index("--output") + 1])
        output_path.write_text(
            json.dumps({"unconnected_items": [{"net": "NET_B"}], "violations": []}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(
        "kicad_mcp.tools.creation_tools.get_kicad_cli_path",
        lambda required=False: str(tmp_path / "kicad-cli.exe"),
    )
    monkeypatch.setattr(
        "kicad_mcp.utils.secure_subprocess.get_kicad_cli_path",
        lambda required=True: str(tmp_path / "kicad-cli.exe"),
    )
    monkeypatch.setattr("kicad_mcp.utils.secure_subprocess.subprocess.run", fake_run)

    result = _run_pcb_drc_sync(str(board))

    assert result["success"] is True
    assert result["total_violations"] == 1
    assert result["violations"][0]["type"] == "unconnected_item"
