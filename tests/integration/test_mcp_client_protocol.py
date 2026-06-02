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
from kicad_mcp.utils.pcbnew_runtime import pcbnew_runtime_status


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
        assert "pcb_layout_engine_status" in tools

        pcb_status = await client.call_tool("pcb_layout_engine_status", {})
        assert pcb_status.is_error is False
        assert pcb_status.data["success"] is True
        assert pcb_status.data["selected_backend"] in {"pcbnew", "sexpr"}
        if pcbnew_runtime_status().get("available"):
            assert pcb_status.data["selected_backend"] == "pcbnew"

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


@pytest.mark.asyncio
async def test_mcp_preview_grouped_interfaces_and_project_directory_alias(tmp_path: Path):
    server = create_server()

    async with Client(server, init_timeout=30, timeout=60) as client:
        created = await client.call_tool(
            "create_kicad_project",
            {
                "project_directory": str(tmp_path),
                "project_name": "interface_preview",
                "create_schematic": True,
                "create_pcb": False,
            },
        )
        assert created.is_error is False
        assert created.data["success"] is True

        preview = await client.call_tool(
            "schematic_preview_design_intent",
            {
                "project_path": created.data["project_path"],
                "intent": {
                    "parts": [
                        {
                            "ref": "U1",
                            "value": "MCU",
                            "pins": [
                                {"number": "1", "name": "PB6", "pintype": "bidirectional"},
                                {"number": "2", "name": "PB7", "pintype": "bidirectional"},
                                {"number": "3", "name": "PA9", "pintype": "output"},
                                {"number": "4", "name": "PA10", "pintype": "input"},
                                {"number": "5", "name": "PA13", "pintype": "bidirectional"},
                                {"number": "6", "name": "PA14", "pintype": "bidirectional"},
                                {"number": "7", "name": "NRST", "pintype": "input"},
                            ],
                        },
                        {
                            "ref": "U2",
                            "value": "SENSOR",
                            "pins": [
                                {"number": "1", "name": "SCL", "pintype": "input"},
                                {"number": "2", "name": "SDA", "pintype": "bidirectional"},
                            ],
                        },
                        {
                            "ref": "J2",
                            "value": "UART",
                            "pins": [
                                {"number": "2", "name": "RX", "pintype": "input"},
                                {"number": "3", "name": "TX", "pintype": "output"},
                            ],
                        },
                    ],
                    "interfaces": {
                        "i2c": [
                            {
                                "name": "SENSOR_I2C",
                                "controller": {"ref": "U1", "scl": "PB6", "sda": "PB7"},
                                "devices": [{"ref": "U2", "scl": "SCL", "sda": "SDA"}],
                                "pullups": {"rail": "+3V3"},
                            }
                        ],
                        "uart": [
                            {
                                "name": "DEBUG_UART",
                                "controller": {"ref": "U1", "tx": "PA9", "rx": "PA10"},
                                "device": {"ref": "J2", "rx": "2", "tx": "3"},
                            }
                        ],
                        "swd": [
                            {
                                "target": "U1",
                                "swdio": "PA13",
                                "swclk": "PA14",
                                "reset": "NRST",
                                "rail": "+3V3",
                                "ground": "GND",
                            }
                        ],
                    },
                },
            },
        )

    assert preview.is_error is False
    assert preview.data["success"] is True
    assert preview.data["summary"]["net_count"] >= 7
    assert preview.data["summary"]["generated_part_count"] == 6


@pytest.mark.asyncio
async def test_mcp_preview_support_circuit_regressions_and_pcb_clean_start(tmp_path: Path):
    server = create_server()

    async with Client(server, init_timeout=30, timeout=60) as client:
        created = await client.call_tool(
            "create_kicad_project",
            {
                "project_dir": str(tmp_path),
                "project_name": "support_preview",
                "create_schematic": True,
                "create_pcb": True,
            },
        )
        assert created.is_error is False
        assert created.data["success"] is True

        preview = await client.call_tool(
            "schematic_preview_design_intent",
            {
                "project_path": created.data["project_path"],
                "intent": {
                    "parts": [
                        {
                            "ref": "U1",
                            "value": "MCU",
                            "pins": [
                                {"number": "1", "name": "PH0", "pintype": "input"},
                                {"number": "2", "name": "PH1", "pintype": "output"},
                                {"number": "3", "name": "NRST", "pintype": "input"},
                                {"number": "4", "name": "PA13", "pintype": "bidirectional"},
                                {"number": "5", "name": "PA14", "pintype": "bidirectional"},
                            ],
                        },
                        {
                            "ref": "J2",
                            "lib_id": "Connector_Generic:Conn_01x05",
                            "value": "DEBUG",
                        },
                        {
                            "ref": "U5",
                            "value": "LDO",
                            "pins": [
                                {"number": "1", "name": "OUT", "pintype": "power_out"},
                                {"number": "2", "name": "GND", "pintype": "power_in"},
                            ],
                        },
                    ],
                    "bulk_connections": [{"net": "+3V3", "pins": [["U5", "OUT"]]}],
                    "interfaces": [
                        {
                            "type": "swd",
                            "target": "U1",
                            "header_ref": "J2",
                            "swdio": "PA13",
                            "swclk": "PA14",
                            "reset": "NRST",
                        }
                    ],
                    "support_circuits": [
                        {
                            "type": "crystal",
                            "target": "U1",
                            "lib_id": "Device:Crystal_GND24_Small",
                            "pins": ["PH0", "PH1"],
                            "pin_map": {"xin": "1", "xout": "3", "ground": ["2", "4"]},
                            "load_capacitance": "22pF",
                        },
                        {
                            "type": "reset_button",
                            "target": "U1",
                            "pin": "NRST",
                            "net": "RESET_N",
                            "pullup": "10k",
                            "rail": "+3V3",
                        },
                        {"type": "ferrite_filter", "in_net": "+3V3", "out_net": "+3V3A"},
                        {"type": "power_flag", "net": "+3V3"},
                    ],
                },
            },
        )
        pcb_preview = await client.call_tool(
            "pcb_preview_layout_intent",
            {
                "project_path": created.data["project_path"],
                "intent": {
                    "placement": {"preserve_existing_placement": False},
                    "routing": {"mode": "none"},
                },
            },
        )

    assert preview.is_error is False
    assert preview.data["success"] is True
    assert preview.data["summary"]["generated_part_count"] == 9
    assert pcb_preview.is_error is False
    assert pcb_preview.data["success"] is True
    assert pcb_preview.data["summary"]["routing"]["clean_start"] is True


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
