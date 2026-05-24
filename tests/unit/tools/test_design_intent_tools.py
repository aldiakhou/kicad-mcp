from pathlib import Path

import pytest

from kicad_mcp.server import create_server


def _tool_intent() -> dict:
    return {
        "parts": [
            {
                "ref": "U1",
                "value": "MCU",
                "footprint": "Package_QFP:LQFP-48_7x7mm_P0.5mm",
                "pins": [
                    {"number": "1", "name": "VDD", "type": "power_in"},
                    {"number": "2", "name": "GND", "type": "power_in"},
                    {"number": "3", "name": "PB6", "type": "bidirectional"},
                    {"number": "4", "name": "PB7", "type": "bidirectional"},
                ],
            },
            {
                "ref": "U2",
                "value": "SENSOR",
                "footprint": "Package_LGA:LGA-4_2x2mm_P0.65mm",
                "pins": [
                    {"number": "1", "name": "SCL", "type": "bidirectional"},
                    {"number": "2", "name": "SDA", "type": "bidirectional"},
                    {"number": "3", "name": "VDD", "type": "power_in"},
                    {"number": "4", "name": "GND", "type": "power_in"},
                ],
            },
        ],
        "pin_rules": [
            {"ref": "U1", "match": {"pin": "VDD"}, "net": "+3V3"},
            {"ref": "U1", "match": {"pin": "GND"}, "net": "GND"},
            {"ref": "U2", "match": {"pin": "VDD"}, "net": "+3V3"},
            {"ref": "U2", "match": {"pin": "GND"}, "net": "GND"},
        ],
        "interfaces": [
            {
                "type": "i2c",
                "name": "SENSOR_I2C",
                "controller": {"ref": "U1", "scl": "PB6", "sda": "PB7"},
                "devices": [{"ref": "U2", "scl": "SCL", "sda": "SDA"}],
                "pullups": {"rail": "+3V3", "value": "4.7k"},
            }
        ],
    }


@pytest.mark.asyncio
async def test_schematic_preview_design_intent_returns_compact_expanded_summary(tmp_path: Path):
    server = create_server()
    tools = await server.get_tools()

    result = tools["schematic_preview_design_intent"].fn(str(tmp_path), _tool_intent())

    assert result["success"] is True
    assert result["tool"] == "schematic_preview_design_intent"
    assert result["stage"] == "preview"
    assert result["changed"] is False
    assert result["summary"]["generated_part_count"] == 2
    assert "expanded_spec" not in result
    assert "diff" not in result
    assert Path(result["expanded_spec_path"]).exists()


@pytest.mark.asyncio
async def test_schematic_apply_design_intent_dry_run_can_include_expanded_spec(tmp_path: Path):
    server = create_server()
    tools = await server.get_tools()

    result = tools["schematic_apply_design_intent"].fn(
        str(tmp_path),
        _tool_intent(),
        "update",
        True,
        False,
        "compact",
        True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["recommended_next_tool"] == "schematic_apply_design_intent"
    assert "expanded_spec" in result
    assert result["expanded_spec"]["nets"]["SENSOR_I2C_SCL"]


@pytest.mark.asyncio
async def test_schematic_apply_design_intent_reports_compile_errors_before_build(tmp_path: Path):
    server = create_server()
    tools = await server.get_tools()

    result = tools["schematic_apply_design_intent"].fn(
        str(tmp_path),
        {"parts": _tool_intent()["parts"], "pin_rules": [{"ref": "U1", "match": {"pin": "NOPE"}, "net": "X"}]},
        "update",
        False,
        False,
        "compact",
        False,
    )

    assert result["success"] is False
    assert result["stage"] == "compile_failed"
    assert result["recoverable"] is True
    assert result["errors"][0]["error"] == "selector matched zero pins"
