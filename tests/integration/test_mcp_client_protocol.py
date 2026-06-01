"""Protocol-level MCP client smoke tests for the KiCad server."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

from fastmcp import Client
import pytest

from kicad_mcp.server import create_server


@pytest.fixture(scope="module")
def event_loop_policy():
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


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
