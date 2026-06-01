import asyncio
from pathlib import Path
import re

import pytest

from kicad_mcp.server import create_server
from kicad_mcp.utils.kicad_cli import get_kicad_cli_path


def _kicad_cli_available() -> bool:
    return get_kicad_cli_path(required=False) is not None


pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_kicad,
    pytest.mark.skipif(not _kicad_cli_available(), reason="KiCad CLI not available"),
]


async def _apply_intent_async(tools: dict, project_path: str, intent: dict) -> dict:
    started = tools["schematic_start_design_intent_job"].fn(project_path, intent)
    assert started["success"] is True
    job_id = started["job_id"]

    status = started
    for _ in range(180):
        status = tools["schematic_get_job_status"].fn(job_id)
        if status["status"] in {"succeeded", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.5)

    assert status["status"] == "succeeded", status
    result = tools["schematic_get_job_result"].fn(job_id)
    assert result["success"] is True, result
    return result["result"]


def _medium_ic_intent() -> dict:
    return {
        "parts": [
            {
                "ref": "U1",
                "lib_id": "MCU_ST_STM32G4:STM32G431KBTx",
                "value": "STM32G431KBTx",
                "footprint": "Package_QFP:LQFP-32_7x7mm_P0.8mm",
                "block": "mcu",
            },
            {
                "ref": "J1",
                "lib_id": "Connector_Generic:Conn_01x06",
                "value": "DEBUG_I2C",
                "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical",
                "block": "connectors",
            },
            {
                "ref": "R1",
                "lib_id": "Device:R",
                "value": "10k",
                "footprint": "Resistor_SMD:R_0603_1608Metric",
                "block": "mcu",
            },
        ],
        "bulk_connections": [
            {
                "net": "+3V3",
                "endpoints": [
                    {"ref": "J1", "pin": "1"},
                    {"ref": "U1", "pin": "VDD"},
                    {"ref": "U1", "pin": "VDDA"},
                    {"ref": "R1", "pin": "1"},
                ],
            },
            {
                "net": "GND",
                "endpoints": [
                    {"ref": "J1", "pin": "2"},
                    {"ref": "U1", "pin": "VSS"},
                    {"ref": "U1", "pin": "VSSA"},
                ],
            },
            {
                "net": "I2C_SCL",
                "endpoints": [
                    {"ref": "J1", "pin": "3"},
                    {"ref": "U1", "pin": "PB6"},
                ],
            },
            {
                "net": "I2C_SDA",
                "endpoints": [
                    {"ref": "J1", "pin": "4"},
                    {"ref": "U1", "pin": "PB7"},
                ],
            },
            {
                "net": "GPIO_TEST",
                "endpoints": [
                    {"ref": "J1", "pin": "5"},
                    {"ref": "U1", "pin": "PB8"},
                    {"ref": "R1", "pin": "2"},
                ],
            },
        ],
        "support_circuits": [
            {
                "type": "decoupling",
                "target": "U1",
                "rail": "+3V3",
                "ground": "GND",
                "capacitors": ["100n", "1u"],
            },
            {"type": "power_flag", "net": "+3V3"},
            {"type": "power_flag", "net": "GND"},
        ],
        "no_connect_rules": [
            {"ref": "U1", "match": {"pin_type": "bidirectional"}, "action": "mark_no_connect"},
            {"ref": "J1", "pin": "6", "action": "mark_no_connect"},
        ],
    }


@pytest.mark.asyncio
async def test_medium_ic_schematic_end_to_end(tmp_path: Path):
    server = create_server()
    tools = await server.get_tools()

    project = tools["create_kicad_project"].fn(str(tmp_path), "medium_ic", True, True, "A4")
    assert project["success"] is True

    intent = _medium_ic_intent()
    preview = tools["schematic_preview_design_intent"].fn(project["project_path"], intent)
    if not preview["success"]:
        pytest.skip(f"Required KiCad library item unavailable: {preview}")
    assert preview["ready_to_apply"] is True

    applied = await _apply_intent_async(tools, project["project_path"], intent)
    assert applied["success"] is True
    assert applied["stage"] == "schematic_committed"
    assert applied["expected_netlist_match"] is True
    assert applied["erc"]["errors"] == 0

    validated = tools["schematic_validate_generated_schematic"].fn(
        project_path=project["project_path"],
        expected_netlist_path=applied["expected_netlist_path"],
    )
    assert validated["success"] is True

    exported = await tools["export_schematic_preview"].fn(project["project_path"], None)
    assert exported["success"] is True
    assert Path(exported["svg_path"]).exists()

    schematic_path = Path(project["created_files"]["schematic"])
    schematic_text = schematic_path.read_text(encoding="utf-8")
    for ref in ("U1", "J1", "R1", "C1", "C2"):
        assert re.search(rf'\(property\s+"Reference"\s+"{ref}"', schematic_text)
    assert "C_U1" not in schematic_text
