import os
from pathlib import Path

import pytest

from kicad_mcp.utils.kicad_cli import get_kicad_cli_path
from kicad_mcp.utils.kicad_s_expr import KiCadSchematic
from kicad_mcp.utils.library_resolver import resolve_symbol
from kicad_mcp.utils.native_netlist import export_native_netlist
from kicad_mcp.utils.schematic_builder import (
    _build_in_memory_schematic,
    _resolve_symbol_embed_chain,
    card_reader_v1_spec,
    validate_connection_plan_membership,
)
from kicad_mcp.utils.schematic_pins import (
    _resolve_symbol_pins_cached,
    get_symbol_pin_map_from_schematic,
)


def test_alias_symbol_embedding_is_flattened_for_native_netlist():
    lib_id, node = _resolve_symbol_embed_chain("Regulator_Linear:AMS1117-3.3")[-1]

    assert lib_id == "Regulator_Linear:AMS1117-3.3"
    assert node.items[1].value == "Regulator_Linear:AMS1117-3.3"
    assert node.first_child("extends") is None
    assert any(child.head() == "symbol" for child in node.child_lists())


def test_pin_map_rotation_matches_kicad_sheet_coordinates(tmp_path: Path):
    _resolve_symbol_pins_cached.cache_clear()
    schematic_path = tmp_path / "demo.kicad_sch"
    schematic = KiCadSchematic.empty()
    schematic.add_symbol(
        "Device:R",
        "R1",
        "10k",
        50.8,
        50.8,
        90,
        "Resistor_SMD:R_0603_1608Metric",
        lib_symbol=resolve_symbol("Device:R")["node"],
    )

    pin_map = get_symbol_pin_map_from_schematic(schematic, str(schematic_path), "R1")
    pins = {pin["number"]: pin for pin in pin_map["pins"]}

    assert pins["1"]["connection_point"] == {"x": 46.99, "y": 50.8}
    assert pins["1"]["position"]["angle"] == 180
    assert pins["2"]["connection_point"] == {"x": 54.61, "y": 50.8}
    assert pins["2"]["position"]["angle"] == 0


def test_validate_connection_plan_ignores_power_flag_symbols(monkeypatch):
    monkeypatch.setattr(
        "kicad_mcp.utils.schematic_builder.export_native_netlist",
        lambda _: {
            "success": True,
            "nets": {"GND": {"nodes": [{"ref": "R1", "pin": "1", "pinfunction": ""}]}},
        },
    )

    result = validate_connection_plan_membership(
        "demo.kicad_sch",
        [
            {"ref": "R1", "pin": "1", "net": "GND"},
            {"ref": "#FLG01", "pin": "1", "net": "GND"},
        ],
    )

    assert result["success"] is True
    assert result["checked_count"] == 1


def test_card_reader_spec_native_netlist_has_required_members(tmp_path: Path):
    if os.environ.get("KICAD_MCP_RUN_LIVE_TESTS") != "1":
        pytest.skip("Live KiCad CLI schematic-builder test is opt-in")
    if not get_kicad_cli_path():
        pytest.skip("KiCad CLI is not available")

    schematic_path = tmp_path / "Card_Reader_clean.kicad_sch"
    spec = card_reader_v1_spec()
    schematic = _build_in_memory_schematic(str(schematic_path), spec)
    schematic_path.write_text(schematic.to_text(), encoding="utf-8")

    native = export_native_netlist(str(schematic_path), timeout_seconds=60)
    assert native["success"] is True
    assert native["connectivity_complete"] is True
    assert native["nets"]["+5V"]["nodes"]
    assert native["nets"]["GND"]["nodes"]
    assert native["nets"]["USB_D+"]["nodes"]

    verified = validate_connection_plan_membership(str(schematic_path), spec["connections"])
    assert verified["success"] is True
    assert verified["missing"] == []
