"""Protocol-level MCP client smoke tests for the KiCad server."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

from fastmcp import Client
import pytest

from kicad_mcp.server import create_server
from kicad_mcp.utils.kicad_cli import get_kicad_cli_path
from kicad_mcp.utils.kicad_pcb_s_expr import KiCadPcb


@pytest.fixture(scope="module")
def event_loop_policy():
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


def _kicad_cli_available() -> bool:
    return get_kicad_cli_path(required=False) is not None


@pytest.mark.asyncio
async def test_fastmcp_client_can_call_compare_and_promotion_tools(
    tmp_path: Path,
    monkeypatch,
):
    server = create_server()
    expected_path = tmp_path / "expected_netlist.json"
    actual_path = tmp_path / "actual.net"
    expected_path.write_text(
        json.dumps({
            "nets": {
                "VBUS": [{"ref": "J1", "pin": "VBUS"}],
                "GND": [{"ref": "J1", "pin": "A1"}],
            },
            "power_nets": ["VBUS", "GND"],
        }),
        encoding="utf-8",
    )
    actual_path.write_text(
        """
        (export (version "E")
          (nets
            (net (code 1) (name "VBUS")
              (node (ref "J1") (pin "A4") (pinfunction "VBUS_A4"))
            )
            (net (code 2) (name "GND")
              (node (ref "J1") (pin "A1") (pinfunction "GND_A1"))
            )
          )
        )
        """,
        encoding="utf-8",
    )

    project_path = tmp_path / "demo.kicad_pro"
    schematic_path = tmp_path / "demo.kicad_sch"
    candidate_dir = tmp_path / ".kicad_mcp" / "engine_artifacts" / "apply-test" / "failed_schematics"
    project_path.write_text("{}", encoding="utf-8")
    schematic_path.write_text("original", encoding="utf-8")
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "demo.kicad_sch").write_text("candidate", encoding="utf-8")
    monkeypatch.setattr(
        "kicad_mcp.tools.creation_tools._generated_schematic_report",
        lambda schematic_path, run_erc: {"success": True, "schematic_path": schematic_path},
    )

    async with Client(server, init_timeout=30, timeout=30) as client:
        tools = {tool.name for tool in await client.list_tools()}
        assert "schematic_compare_netlists" in tools
        assert "schematic_export_candidate_to_project" in tools

        compare = await client.call_tool(
            "schematic_compare_netlists",
            {
                "expected_netlist_path": str(expected_path),
                "actual_netlist_path": str(actual_path),
            },
        )
        assert compare.is_error is False
        assert compare.data["success"] is True
        assert Path(compare.data["diff_path"]).exists()

        promoted = await client.call_tool(
            "schematic_export_candidate_to_project",
            {
                "project_path": str(project_path),
                "job_id": "apply-test",
                "run_erc": True,
            },
        )
        assert promoted.is_error is False
        assert promoted.data["success"] is True
        assert promoted.data["backup_paths"]
        assert schematic_path.read_text(encoding="utf-8") == "candidate"


@pytest.mark.asyncio
async def test_stdio_mcp_client_can_connect_to_server_entrypoint():
    config = {
        "mcpServers": {
            "kicad": {
                "command": sys.executable,
                "args": ["-c", "from kicad_mcp.server import main; main()"],
                "env": {"KICAD_MCP_TRANSPORT": "stdio"},
            },
        },
    }

    async with Client(config, init_timeout=30, timeout=30) as client:
        tools = {tool.name for tool in await client.list_tools()}
        assert "schematic_design_intent_schema" in tools
        assert "schematic_compare_netlists" in tools

        overview = await client.call_tool(
            "schematic_design_intent_schema",
            {"section": "overview"},
        )

    assert overview.is_error is False
    assert overview.data["success"] is True
    assert (
        overview.data["schema"]["candidate_artifacts"]["promotion_tool"]
        == "schematic_export_candidate_to_project"
    )


@pytest.mark.skipif(not _kicad_cli_available(), reason="KiCad CLI not available")
@pytest.mark.asyncio
async def test_mcp_validate_schematic_reports_real_erc_violations():
    server = create_server()
    fixture = Path("tests/fixtures/messy_card_reader_like_schematic.kicad_sch").resolve()

    async with Client(server, init_timeout=30, timeout=60) as client:
        result = await client.call_tool(
            "schematic_validate_generated_schematic",
            {
                "schematic_path": str(fixture),
                "run_erc": True,
                "run_visual_lint": False,
            },
        )

    assert result.is_error is False
    assert result.data["success"] is False
    assert result.data["erc"]["total"] > 0
    assert result.data["erc"]["errors"] > 0


@pytest.mark.skipif(not _kicad_cli_available(), reason="KiCad CLI not available")
@pytest.mark.asyncio
async def test_mcp_pcb_validate_layout_reports_real_drc_violations(tmp_path: Path):
    server = create_server()

    async with Client(server, init_timeout=30, timeout=90) as client:
        created = await client.call_tool(
            "create_kicad_project",
            {
                "project_dir": str(tmp_path),
                "project_name": "drc_violation",
                "create_schematic": True,
                "create_pcb": True,
                "paper": "A4",
            },
        )
        assert created.data["success"] is True

        pcb_path = Path(created.data["created_files"]["pcb"])
        pcb = KiCadPcb.from_file(str(pcb_path))
        pcb.add_track(
            "NET_A",
            [{"x": 10, "y": 10}, {"x": 40, "y": 10}],
            width_mm=0.5,
        )
        pcb.add_track(
            "NET_B",
            [{"x": 10, "y": 10}, {"x": 40, "y": 10}],
            width_mm=0.5,
        )
        pcb_path.write_text(pcb.to_text(), encoding="utf-8")

        validation = await client.call_tool(
            "pcb_validate_layout",
            {
                "project_path": created.data["project_path"],
                "run_drc": True,
                "require_clean_drc": True,
            },
        )

    assert validation.is_error is False
    assert validation.data["success"] is False
    assert validation.data["drc"]["success"] is True
    assert validation.data["drc"]["total_violations"] > 0
    assert "DRC has" in validation.data["blocking_issues"][0]
