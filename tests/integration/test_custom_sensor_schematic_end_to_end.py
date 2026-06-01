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


@pytest.mark.asyncio
async def test_custom_sensor_schematic_end_to_end(tmp_path: Path):
    server = create_server()
    tools = await server.get_tools()

    project = tools["create_kicad_project"].fn(str(tmp_path), "custom_sensor", True, True, "A4")
    assert project["success"] is True

    intent = {
        "parts": [
            {
                "ref": "U2",
                "value": "DPS310",
                "footprint": "Package_LGA:LGA-8_2x2mm_P0.5mm",
                "block": "sensors",
                "pins": [
                    {"number": "1", "name": "SCL", "pintype": "input"},
                    {"number": "2", "name": "SDA", "pintype": "bidirectional"},
                    {"number": "3", "name": "VDD", "pintype": "power_in"},
                    {"number": "4", "name": "GND", "pintype": "power_in"},
                    {"number": "5", "name": "INT", "pintype": "output"},
                ],
            },
            {
                "ref": "J1",
                "lib_id": "Connector_Generic:Conn_01x05",
                "value": "SENSOR_HOST",
                "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x05_P2.54mm_Vertical",
                "block": "interfaces",
            },
        ],
        "rails": {
            "+3V3": {"pins": [["J1", "1"], ["U2", "VDD"]]},
            "GND": {"pins": [["J1", "2"], ["U2", "GND"]]},
        },
        "bulk_connections": [
            {"net": "I2C_SCL", "pins": [["J1", "3"], ["U2", "SCL"]]},
            {"net": "I2C_SDA", "pins": [["J1", "4"], ["U2", "SDA"]]},
            {"net": "DPS310_INT", "pins": [["J1", "5"], ["U2", "INT"]]},
        ],
        "support_circuits": {
            "decoupling": [
                {"target": "U2", "rail": "+3V3", "ground": "GND", "capacitors": ["100n"]}
            ],
            "power_flag": [
                {"net": "+3V3"},
                {"net": "GND"},
            ],
            "pullup": [
                {"net": "I2C_SCL", "rail": "+3V3", "value": "4.7k"},
                {"net": "I2C_SDA", "rail": "+3V3", "value": "4.7k"},
            ],
        },
    }

    preview = tools["schematic_preview_design_intent"].fn(project["project_path"], intent)
    assert preview["success"] is True
    assert preview["ready_to_apply"] is True

    applied = await _apply_intent_async(tools, project["project_path"], intent)
    assert applied["success"] is True
    assert applied["stage"] == "schematic_committed"
    assert applied["expected_netlist_match"] is True
    assert applied["output_symbol_count"] >= 5

    schematic_text = Path(project["created_files"]["schematic"]).read_text(encoding="utf-8")
    for ref in ("U2", "J1", "C1", "R1", "R2"):
        assert re.search(rf'\(property\s+"Reference"\s+"{ref}"', schematic_text)
    assert '"kicad_mcp:DPS310"' in schematic_text
