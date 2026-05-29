import os
from pathlib import Path

import pytest

from kicad_mcp.utils import schematic_builder
from kicad_mcp.utils.kicad_cli import get_kicad_cli_path
from kicad_mcp.utils.kicad_s_expr import KiCadSchematic
from kicad_mcp.utils.library_resolver import resolve_symbol
from kicad_mcp.utils.native_netlist import export_native_netlist
from kicad_mcp.utils.schematic_intent import connect_pins
from kicad_mcp.utils.schematic_builder import (
    _apply_spec_to_existing_schematic,
    _build_in_memory_schematic,
    _resolve_symbol_embed_chain,
    card_reader_v1_spec,
    normalize_build_spec_v2,
    validate_connection_plan_membership,
)
from kicad_mcp.utils.schematic_pins import (
    _resolve_symbol_pins_cached,
    add_no_connect_to_pin,
    attach_net_to_pin,
    get_symbol_pin_map_from_schematic,
)


def test_alias_symbol_embedding_is_flattened_for_native_netlist():
    lib_id, node = _resolve_symbol_embed_chain("Regulator_Linear:AMS1117-3.3")[-1]

    assert lib_id == "Regulator_Linear:AMS1117-3.3"
    assert node.items[1].value == "Regulator_Linear:AMS1117-3.3"
    assert node.first_child("extends") is None
    assert any(child.head() == "symbol" for child in node.child_lists())


def test_update_mode_applies_spec_paper_to_existing_schematic(tmp_path: Path):
    schematic_path = tmp_path / "demo.kicad_sch"
    schematic = KiCadSchematic.empty(paper="A3")

    updated = _apply_spec_to_existing_schematic(
        schematic,
        str(schematic_path),
        {"paper": "A1", "symbols": [], "connections": [], "no_connects": []},
        "update",
    )

    assert schematic_builder._paper(updated) == "A1"


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


def test_external_stub_preserves_requested_global_label_type(tmp_path: Path):
    schematic_path = tmp_path / "demo.kicad_sch"
    schematic = KiCadSchematic.empty()
    schematic.add_symbol(
        "Device:R",
        "R1",
        "10k",
        50.8,
        50.8,
        0,
        "Resistor_SMD:R_0603_1608Metric",
        lib_symbol=resolve_symbol("Device:R")["node"],
    )

    attach_net_to_pin(
        schematic,
        str(schematic_path),
        "R1",
        "1",
        "GLOBAL_NET",
        label_type="global",
        label_placement="external_stubs",
    )

    labels = schematic.list_labels()
    assert labels[0]["text"] == "GLOBAL_NET"
    assert labels[0]["type"] == "global"


def test_connect_pins_wire_style_routes_wire_instead_of_labels(tmp_path: Path):
    schematic_path = tmp_path / "demo.kicad_sch"
    schematic = KiCadSchematic.empty()
    resistor = resolve_symbol("Device:R")["node"]
    schematic.add_symbol(
        "Device:R",
        "R1",
        "10k",
        50.8,
        50.8,
        0,
        "Resistor_SMD:R_0603_1608Metric",
        lib_symbol=resistor,
    )
    schematic.add_symbol(
        "Device:R",
        "R2",
        "10k",
        70.8,
        50.8,
        180,
        "Resistor_SMD:R_0603_1608Metric",
        lib_symbol=resistor,
    )

    result = connect_pins(schematic, str(schematic_path), "R1", "2", "R2", "2", style="wire")

    assert result["style"] == "wire"
    assert len(schematic.list_wires()) >= 1


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


def test_v2_normalizer_accepts_lib_id_alias_and_rsplit_string_endpoint():
    normalized = normalize_build_spec_v2(
        {
            "parts": [
                {
                    "ref": "R10",
                    "lib_id": "Device:R",
                    "symbol": "R_1_1",
                    "value": "10k",
                }
            ],
            "nets": {"GND": ["R10_1"]},
        }
    )

    assert normalized["normalization_errors"] == []
    assert normalized["symbols"][0]["lib_id"] == "Device:R"
    assert normalized["connections"][0]["ref"] == "R10"
    assert normalized["connections"][0]["pin"] == "1"
    assert normalized["normalization_warnings"]


def test_v2_builder_applies_readable_defaults_without_explicit_layout(monkeypatch):
    captured: dict = {}

    def fake_build(project_path, spec, **kwargs):
        captured["spec"] = spec
        return {"success": True, "project_path": project_path, **kwargs}

    monkeypatch.setattr(schematic_builder, "build_schematic_from_spec", fake_build)

    result = schematic_builder.build_schematic_from_spec_v2(
        "demo.kicad_pro",
        {
            "custom_parts": [
                {
                    "ref": "U1",
                    "value": "IC",
                    "pins": [{"number": "1", "name": "A"}, {"number": "2", "name": "B"}],
                }
            ],
            "nets": {"SIG": [["U1", "A"]]},
        },
    )

    assert result["success"] is True
    assert captured["spec"]["paper"] == "A3"
    assert captured["spec"]["connections"][0]["label_placement"] == "external_stubs"
    assert captured["spec"]["connections"][0]["connection_style"] == "auto"


def test_v2_preview_applies_same_readable_defaults_as_build(monkeypatch):
    captured: dict = {}

    def fake_preview(project_path, spec):
        captured["spec"] = spec
        return {"success": True, "project_path": project_path}

    monkeypatch.setattr(schematic_builder, "preview_build_from_spec", fake_preview)

    result = schematic_builder.preview_build_from_spec_v2(
        "demo.kicad_pro",
        {
            "custom_parts": [
                {
                    "ref": "U1",
                    "value": "IC",
                    "pins": [{"number": "1", "name": "A"}, {"number": "2", "name": "B"}],
                }
            ],
            "nets": {"SIG": [["U1", "A"]]},
        },
    )

    assert result["success"] is True
    assert captured["spec"]["paper"] == "A3"
    assert captured["spec"]["connections"][0]["label_placement"] == "external_stubs"
    assert captured["spec"]["connections"][0]["connection_style"] == "auto"


def test_v2_builder_preserves_explicit_pin_anchor_layout(monkeypatch):
    captured: dict = {}

    def fake_build(project_path, spec, **kwargs):
        captured["spec"] = spec
        return {"success": True, "project_path": project_path, **kwargs}

    monkeypatch.setattr(schematic_builder, "build_schematic_from_spec", fake_build)

    schematic_builder.build_schematic_from_spec_v2(
        "demo.kicad_pro",
        {
            "custom_parts": [
                {
                    "ref": "U1",
                    "value": "IC",
                    "pins": [{"number": "1", "name": "A"}, {"number": "2", "name": "B"}],
                }
            ],
            "nets": {"SIG": [["U1", "A"]]},
            "layout_hints": {"label_strategy": "pin_anchor", "connection_style": "label"},
        },
    )

    assert captured["spec"]["paper"] == "A4"
    assert captured["spec"]["connections"][0]["label_placement"] == "pin_anchor"
    assert captured["spec"]["connections"][0]["connection_style"] == "label"


def test_v2_builder_forwards_cli_validation_toggle(monkeypatch):
    captured: dict = {}

    def fake_build(project_path, spec, **kwargs):
        captured["kwargs"] = kwargs
        return {"success": True, "project_path": project_path}

    monkeypatch.setattr(schematic_builder, "build_schematic_from_spec", fake_build)

    schematic_builder.build_schematic_from_spec_v2(
        "demo.kicad_pro",
        {"parts": [], "nets": {}},
        run_cli_validation=False,
    )

    assert captured["kwargs"]["run_cli_validation"] is False


def test_v2_builder_visual_gate_preview_error_returns_structured_failure(monkeypatch):
    def fail_preview_build(*_args, **_kwargs):
        raise RuntimeError("preview failed")

    def fail_write(*_args, **_kwargs):
        raise AssertionError("build_schematic_from_spec should not write after visual gate failure")

    monkeypatch.setattr(schematic_builder, "_build_in_memory_schematic", fail_preview_build)
    monkeypatch.setattr(schematic_builder, "build_schematic_from_spec", fail_write)

    result = schematic_builder.build_schematic_from_spec_v2(
        "demo.kicad_pro",
        {
            "custom_parts": [
                {
                    "ref": "U1",
                    "value": "IC",
                    "pins": [{"number": "1", "name": "A"}, {"number": "2", "name": "B"}],
                }
            ],
            "nets": {},
            "layout_hints": {"visual_gate": "strict", "visual_layout": {"enabled": True}},
        },
    )

    assert result["success"] is False
    assert result["stage"] == "visual_gate_error"
    assert result["visual_gate"]["passed"] is False


def test_v2_normalizer_rejects_unit_name_without_lib_id():
    normalized = normalize_build_spec_v2(
        {"parts": [{"ref": "R1", "symbol": "R_1_1"}], "nets": {}}
    )

    assert normalized["normalization_errors"]
    assert "full KiCad library ID" in normalized["normalization_errors"][0]["error"]


def test_custom_part_pins_are_available_from_embedded_symbol(tmp_path: Path):
    schematic_path = tmp_path / "custom.kicad_sch"
    spec = normalize_build_spec_v2(
        {
            "custom_parts": [
                {
                    "ref": "U6",
                    "value": "DPS310",
                    "footprint": "Package_LGA:LGA-8_2.0x2.5mm_P0.65mm",
                    "pins": [
                        {"number": "1", "name": "SCL", "type": "bidirectional"},
                        {"number": "2", "name": "SDA", "type": "bidirectional"},
                        {"number": "3", "name": "GND", "type": "power_in"},
                    ],
                }
            ],
            "nets": {},
        }
    )
    schematic = _build_in_memory_schematic(str(schematic_path), spec)

    pin_map = get_symbol_pin_map_from_schematic(schematic, str(schematic_path), "U6")

    assert pin_map["success"] is True
    assert {pin["name"] for pin in pin_map["pins"]} == {"SCL", "SDA", "GND"}


def _hidden_pin_test_schematic(tmp_path: Path) -> tuple[KiCadSchematic, str]:
    schematic_path = tmp_path / "hidden.kicad_sch"
    spec = normalize_build_spec_v2(
        {
            "custom_parts": [
                {
                    "ref": "U2",
                    "value": "HIDDEN_PINS",
                    "footprint": "Package_DIP:DIP-4_W7.62mm",
                    "pins": [
                        {"number": "1", "name": "NC", "type": "no_connect", "hidden": True},
                        {"number": "2", "name": "VDD", "type": "power_in", "hidden": True},
                        {"number": "3", "name": "SCL", "type": "bidirectional"},
                    ],
                }
            ],
            "nets": {},
        }
    )
    return _build_in_memory_schematic(str(schematic_path), spec), str(schematic_path)


def test_explicit_hidden_nc_no_connect_is_noop(tmp_path: Path):
    schematic, schematic_path = _hidden_pin_test_schematic(tmp_path)

    result = add_no_connect_to_pin(schematic, schematic_path, "U2", "1")

    assert result["skipped"] is True
    assert result["reason"] == "hidden NC pin does not require a no-connect marker"
    assert schematic.list_no_connects() == []


def test_hidden_power_no_connect_requires_explicit_allow(tmp_path: Path):
    schematic, schematic_path = _hidden_pin_test_schematic(tmp_path)

    with pytest.raises(ValueError, match="allow_hidden_no_connect=True"):
        add_no_connect_to_pin(schematic, schematic_path, "U2", "2")

    result = add_no_connect_to_pin(
        schematic,
        schematic_path,
        "U2",
        "2",
        allow_hidden_no_connect=True,
    )
    assert result.get("skipped") is not True
    assert len(schematic.list_no_connects()) == 1


def test_update_removes_existing_no_connect_before_connecting_pin(tmp_path: Path):
    schematic_path = tmp_path / "update_nc.kicad_sch"
    initial_spec = normalize_build_spec_v2(
        {
            "custom_parts": [
                {
                    "ref": "U1",
                    "value": "I2C_DEVICE",
                    "footprint": "Package_DIP:DIP-4_W7.62mm",
                    "pins": [
                        {"number": "1", "name": "SCL", "type": "bidirectional"},
                        {"number": "2", "name": "SDA", "type": "bidirectional"},
                    ],
                }
            ],
            "nets": {},
            "no_connects": [["U1", "SCL"]],
        }
    )
    schematic = _build_in_memory_schematic(str(schematic_path), initial_spec)
    assert len(schematic.list_no_connects()) == 1

    update_spec = normalize_build_spec_v2({"nets": {"I2C_SCL": [["U1", "SCL"]]}})
    edit_summary: dict = {}
    _apply_spec_to_existing_schematic(
        schematic,
        str(schematic_path),
        update_spec,
        "update",
        edit_summary=edit_summary,
    )

    assert schematic.list_no_connects() == []
    assert any(label["text"] == "I2C_SCL" for label in schematic.list_labels())
    assert edit_summary["removed_conflicting_no_connects"] == [
        {"ref": "U1", "pin": "SCL", "removed_count": 1}
    ]


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
