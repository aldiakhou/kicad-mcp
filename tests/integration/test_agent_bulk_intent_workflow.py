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


def _bulk_stm32_sensor_intent() -> dict:
    mcu_pins = [
        {"number": "1", "name": "VDD", "type": "power_in"},
        {"number": "2", "name": "VDDA", "type": "power_in"},
        {"number": "3", "name": "VBAT", "type": "power_in"},
        {"number": "4", "name": "VSS", "type": "power_in"},
        {"number": "5", "name": "VSSA", "type": "power_in"},
        {"number": "6", "name": "PB6", "type": "bidirectional"},
        {"number": "7", "name": "PB7", "type": "bidirectional"},
        {"number": "8", "name": "PA13", "type": "bidirectional"},
        {"number": "9", "name": "PA14", "type": "bidirectional"},
        {"number": "10", "name": "NRST", "type": "input"},
        {"number": "11", "name": "PA0", "type": "bidirectional"},
        {"number": "12", "name": "PA1", "type": "bidirectional"},
        {"number": "13", "name": "PB0", "type": "bidirectional"},
        {"number": "14", "name": "PB1", "type": "bidirectional"},
    ]
    sensor_pins = [
        {"number": "1", "name": "SCL", "type": "bidirectional"},
        {"number": "2", "name": "SDA", "type": "bidirectional"},
        {"number": "3", "name": "INT", "type": "output"},
        {"number": "4", "name": "VDD", "type": "power_in"},
        {"number": "5", "name": "GND", "type": "power_in"},
    ]
    return {
        "parts": [
            {
                "ref": "U1",
                "value": "STM32F103C8T6",
                "footprint": "Package_DIP:DIP-14_W7.62mm",
                "pins": mcu_pins,
            },
            {
                "ref": "U2",
                "value": "ICM-20948",
                "footprint": "Package_DIP:DIP-8_W7.62mm",
                "pins": sensor_pins,
            },
        ],
        "pin_rules": [
            {"ref": "U1", "match": {"name_regex": "VDD|VDDA|VBAT"}, "net": "+3V3"},
            {"ref": "U1", "match": {"name_regex": "VSS|VSSA|GND"}, "net": "GND"},
            {"ref": "U2", "match": {"name_regex": "VDD"}, "net": "+3V3_SENSOR"},
            {"ref": "U2", "match": {"name_regex": "GND"}, "net": "GND"},
        ],
        "interfaces": [
            {
                "type": "i2c",
                "name": "SENSOR_I2C",
                "controller": {"ref": "U1", "scl": "PB6", "sda": "PB7"},
                "devices": [{"ref": "U2", "scl": "SCL", "sda": "SDA", "interrupts": {"INT": "IMU_INT"}}],
                "pullups": {"rail": "+3V3_SENSOR", "value": "4.7k"},
            },
            {
                "type": "swd",
                "target": "U1",
                "swdio": "PA13",
                "swclk": "PA14",
                "reset": "NRST",
                "rail": "+3V3",
                "ground": "GND",
            },
        ],
        "support_circuits": [
            {"type": "decoupling", "target": "U1", "rail": "+3V3", "ground": "GND", "capacitors": ["100n", "100n", "4.7u"]},
            {"type": "decoupling", "target": "U2", "rail": "+3V3_SENSOR", "ground": "GND", "capacitors": ["100n", "1u"]},
            {"type": "pullup", "net": "RESET_N", "rail": "+3V3", "value": "10k"},
        ],
        "no_connect_rules": [
            {
                "ref": "U1",
                "match": {"name_regex": "PA[0-9]+|PB[0-9]+"},
                "except": ["PB6", "PB7", "PA13", "PA14"],
                "action": "mark_no_connect",
            }
        ],
    }


@pytest.mark.asyncio
async def test_agent_bulk_intent_workflow_builds_from_fewer_than_twenty_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KICAD_MCP_TOOL_PROFILE", "all")
    server = create_server()
    tools = await server.get_tools()
    project = tools["create_kicad_project"].fn(str(tmp_path), "bulk_intent", True, True, "A4")
    assert project["success"] is True

    intent = _bulk_stm32_sensor_intent()
    intent_entry_count = sum(len(intent[key]) for key in ("parts", "pin_rules", "interfaces", "support_circuits", "no_connect_rules"))
    assert intent_entry_count < 20

    result = tools["schematic_apply_design_intent"].fn(
        project["project_path"],
        intent,
        "update",
        False,
        False,
        "compact",
        False,
    )

    assert result["success"] is True
    assert result["summary"]["total_part_count"] >= 10
    assert result["summary"]["connection_count"] >= 25
    assert result["verification"]["native_netlist_success"] is True
    assert result["verification"]["missing_connection_count"] == 0


@pytest.mark.asyncio
async def test_agent_design_intent_no_connect_rules_skip_hidden_nc_custom_symbol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("KICAD_MCP_TOOL_PROFILE", "all")
    server = create_server()
    tools = await server.get_tools()
    project = tools["create_kicad_project"].fn(str(tmp_path), "hidden_nc_intent", True, True, "A4")
    assert project["success"] is True

    intent = {
        "parts": [
            {
                "ref": "U2",
                "value": "HIDDEN_NC_SENSOR",
                "footprint": "Package_DIP:DIP-6_W7.62mm",
                "pins": [
                    {"number": "1", "name": "NC", "type": "no_connect", "hidden": True},
                    {"number": "2", "name": "NC", "type": "no_connect", "hidden": True},
                    {"number": "3", "name": "SCL", "type": "bidirectional"},
                    {"number": "4", "name": "SDA", "type": "bidirectional"},
                    {"number": "5", "name": "VDD", "type": "power_in"},
                    {"number": "6", "name": "GND", "type": "power_in"},
                ],
            }
        ],
        "pin_rules": [
            {"ref": "U2", "match": {"pin": "VDD"}, "net": "+3V3"},
            {"ref": "U2", "match": {"pin": "GND"}, "net": "GND"},
        ],
        "no_connect_rules": [
            {"ref": "U2", "match": {"name_regex": "NC|SCL|SDA"}, "action": "mark_no_connect"}
        ],
    }

    result = tools["schematic_apply_design_intent"].fn(
        project["project_path"],
        intent,
        "update",
        False,
        False,
        "compact",
        False,
    )

    assert result["success"] is True
    assert result["summary"]["skipped_hidden_pin_count"] == 2
    assert result["summary"]["no_connect_count"] == 2
    assert result["verification"]["native_netlist_success"] is True
    assert result["verification"]["missing_connection_count"] == 0
