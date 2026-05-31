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


def _led_resistor_intent() -> dict:
    return {
        "parts": [
            {
                "ref": "J1",
                "lib_id": "Connector_Generic:Conn_01x02",
                "value": "POWER",
                "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
            },
            {
                "ref": "R1",
                "lib_id": "Device:R",
                "value": "1k",
                "footprint": "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal",
            },
            {
                "ref": "D1",
                "lib_id": "Device:LED",
                "value": "LED",
                "footprint": "LED_THT:LED_D5.0mm",
            },
        ],
        "bulk_connections": [
            {
                "net": "+5V",
                "endpoints": [
                    {"ref": "J1", "pin": "1"},
                    {"ref": "R1", "pin": "1"},
                ],
            },
            {
                "net": "LED_A",
                "endpoints": [
                    {"ref": "R1", "pin": "2"},
                    {"ref": "D1", "pin": "A"},
                ],
            },
            {
                "net": "GND",
                "endpoints": [
                    {"ref": "J1", "pin": "2"},
                    {"ref": "D1", "pin": "K"},
                ],
            },
        ],
    }


@pytest.mark.asyncio
async def test_led_resistor_schematic_end_to_end(tmp_path: Path):
    server = create_server()
    tools = await server.get_tools()

    project = tools["create_kicad_project"].fn(str(tmp_path), "led_resistor", True, True, "A4")
    assert project["success"] is True

    preview = tools["schematic_preview_design_intent"].fn(
        project["project_path"],
        _led_resistor_intent(),
    )
    if not preview["success"]:
        pytest.skip(f"Required KiCad library item unavailable: {preview}")

    applied = tools["schematic_apply_design_intent"].fn(
        project["project_path"],
        _led_resistor_intent(),
    )
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
    assert re.search(r'\(property\s+"Reference"\s+"R1"', schematic_text)
    assert re.search(r'\(property\s+"Reference"\s+"D1"', schematic_text)
