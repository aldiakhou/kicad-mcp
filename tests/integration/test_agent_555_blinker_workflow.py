from pathlib import Path

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


def _555_blinker_spec() -> dict:
    return {
        "name": "555_led_blinker",
        "paper": "A4",
        "parts": [
            {
                "ref": "J1",
                "symbol": "Connector_Generic:Conn_01x02",
                "value": "POWER",
                "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
            },
            {
                "ref": "U1",
                "symbol": "Timer:LM555xN",
                "value": "LM555",
                "footprint": "Package_DIP:DIP-8_W7.62mm",
            },
            {
                "ref": "R1",
                "symbol": "Device:R",
                "value": "47k",
                "footprint": "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal",
            },
            {
                "ref": "R2",
                "symbol": "Device:R",
                "value": "100k",
                "footprint": "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal",
            },
            {
                "ref": "R3",
                "symbol": "Device:R",
                "value": "330",
                "footprint": "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal",
            },
            {
                "ref": "C1",
                "symbol": "Device:C",
                "value": "10u",
                "footprint": "Capacitor_THT:C_Disc_D5.0mm_W2.5mm_P2.50mm",
            },
            {
                "ref": "C2",
                "symbol": "Device:C",
                "value": "10n",
                "footprint": "Capacitor_THT:C_Disc_D5.0mm_W2.5mm_P2.50mm",
            },
            {
                "ref": "D1",
                "symbol": "Device:LED",
                "value": "LED",
                "footprint": "LED_THT:LED_D5.0mm",
            },
            {
                "ref": "#FLG01",
                "symbol": "power:PWR_FLAG",
                "value": "PWR_FLAG",
            },
            {
                "ref": "#FLG02",
                "symbol": "power:PWR_FLAG",
                "value": "PWR_FLAG",
            },
        ],
        "nets": {
            "+5V": [["J1", "1"], ["U1", "8"], ["U1", "4"], ["R1", "1"], ["#FLG01", "1"]],
            "GND": [["J1", "2"], ["U1", "1"], ["C1", "2"], ["C2", "2"], ["D1", "2"], ["#FLG02", "1"]],
            "RC_TOP": [["R1", "2"], ["R2", "1"], ["U1", "7"]],
            "RC_NODE": [["R2", "2"], ["C1", "1"], ["U1", "2"], ["U1", "6"]],
            "LED_A": [["U1", "3"], ["R3", "1"]],
            "LED_K": [["R3", "2"], ["D1", "1"]],
            "CV": [["U1", "5"], ["C2", "1"]],
        },
        "layout_hints": {
            "functional_blocks": [
                {"name": "power_input", "parts": ["J1", "#FLG01", "#FLG02"]},
                {"name": "timer", "parts": ["U1", "R1", "R2", "C1", "C2"]},
                {"name": "output_led", "parts": ["R3", "D1"]},
            ]
        },
    }


@pytest.mark.asyncio
async def test_agent_555_blinker_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KICAD_MCP_TOOL_PROFILE", "all")
    server = create_server()
    tools = await server.get_tools()

    project = tools["create_kicad_project"].fn(str(tmp_path), "agent_555", True, True, "A4")
    assert project["success"] is True

    preview = tools["schematic_preview_build_from_spec_v2"].fn(
        project["project_path"], _555_blinker_spec()
    )
    if not preview["success"]:
        pytest.skip(f"Required KiCad library item unavailable: {preview}")

    built = tools["schematic_build_from_spec_v2"].fn(
        project["project_path"],
        _555_blinker_spec(),
        "replace",
        True,
        True,
        True,
        "full",
        True,
        True,
        True,
        True,
    )
    assert built["success"] is True
    assert built["native_netlist"]["success"] is True

    quality = tools["schematic_quality_report"].fn(project["project_path"], True)
    assert quality["success"] is True
    assert quality["native_netlist"]["success"] is True
    assert quality["dangling_label_count"] == 0
    assert quality["isolated_label_count"] == 0

    erc = await tools["run_erc_check"].fn(project["project_path"], None, None)
    assert erc["success"] is True
    accepted_erc_types = {"lib_symbol_mismatch"}
    unacceptable = [
        violation
        for violation in erc.get("violations", [])
        if violation.get("type") not in accepted_erc_types
    ]
    assert unacceptable == []

    native = built["native_netlist"]
    assert native["connectivity_complete"] is True
    for net_name in ["+5V", "GND", "RC_TOP", "RC_NODE", "LED_A"]:
        assert net_name in native["nets"]

    completed = await tools["pcb_sync_place_and_report"].fn(
        project["project_path"], 100.0, 80.0, "functional", None, False
    )
    assert completed["success"] is True
    assert completed["quality"]["footprint_count"] >= 8
    assert completed["quality"]["assigned_pad_count"] > 0

    pcb_quality = tools["pcb_quality_report"].fn(project["project_path"])
    assert pcb_quality["success"] is True
    assert pcb_quality["footprint_count"] >= 8
    assert pcb_quality["assigned_pad_count"] > 0
