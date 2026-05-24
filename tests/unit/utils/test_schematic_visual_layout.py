from pathlib import Path

from kicad_mcp.utils.kicad_s_expr import KiCadSchematic
from kicad_mcp.utils.schematic_builder import (
    _build_in_memory_schematic,
    normalize_build_spec_v2,
    schematic_quality_report,
)
from kicad_mcp.utils.schematic_visual_layout import apply_visual_layout_to_v2_spec


def _large_mcu_part() -> dict:
    pins = []
    for index in range(1, 25):
        pins.append({"number": str(index), "name": f"PA{index - 1}", "type": "bidirectional"})
    for index in range(25, 49):
        pins.append({"number": str(index), "name": f"PB{index - 25}", "type": "bidirectional"})
    pins.extend(
        [
            {"number": "49", "name": "VDD", "type": "power_in"},
            {"number": "50", "name": "VSS", "type": "power_in"},
        ]
    )
    return {
        "ref": "U1",
        "value": "MCU48",
        "footprint": "Package_QFP:LQFP-48_7x7mm_P0.5mm",
        "pins": pins,
    }


def test_visual_layout_assigns_explicit_non_overlapping_positions():
    spec = {
        "parts": [
            _large_mcu_part(),
            {
                "ref": "U2",
                "value": "SENSOR",
                "footprint": "Package_DIP:DIP-8_W7.62mm",
                "pins": [
                    {"number": "1", "name": "SCL", "type": "bidirectional"},
                    {"number": "2", "name": "SDA", "type": "bidirectional"},
                    {"number": "3", "name": "VDD", "type": "power_in"},
                    {"number": "4", "name": "GND", "type": "power_in"},
                ],
            },
            {
                "ref": "C1",
                "lib_id": "Device:C",
                "value": "100n",
                "footprint": "Capacitor_SMD:C_0603_1608Metric",
                "generated_by": "decoupling",
                "target": "U1",
            },
        ],
        "nets": {},
    }

    laid_out = apply_visual_layout_to_v2_spec(spec)
    by_ref = {part["ref"]: part for part in laid_out["parts"]}

    assert laid_out["paper"] == "A3"
    assert by_ref["U1"]["x"] != by_ref["U2"]["x"]
    assert by_ref["C1"]["x"] > by_ref["U1"]["x"]
    assert laid_out["layout_hints"]["label_strategy"] == "external_stubs"
    assert laid_out["layout_hints"]["visual_layout"]["estimated_overlap_count"] == 0
    assert laid_out["layout_hints"]["generated_groups"]["U1"]["parts"][0]["ref"] == "C1"


def test_v2_normalizer_uses_visual_layout_positions():
    laid_out = apply_visual_layout_to_v2_spec({"parts": [_large_mcu_part()], "nets": {}})
    normalized = normalize_build_spec_v2(laid_out)

    assert normalized["symbols"][0]["x"] == laid_out["parts"][0]["x"]
    assert normalized["symbols"][0]["y"] == laid_out["parts"][0]["y"]


def test_external_stub_labels_are_outside_symbol_body(tmp_path: Path, monkeypatch):
    spec = apply_visual_layout_to_v2_spec(
        {
            "parts": [_large_mcu_part()],
            "nets": {
                "GPIO_A": [["U1", "PA0"]],
                "GPIO_B": [["U1", "PB0"]],
            },
        }
    )
    normalized = normalize_build_spec_v2(spec)
    schematic_path = tmp_path / "visual.kicad_sch"
    schematic = _build_in_memory_schematic(str(schematic_path), normalized)
    schematic_path.write_text(schematic.to_text(), encoding="utf-8")
    monkeypatch.setattr(
        "kicad_mcp.utils.schematic_builder.export_native_netlist",
        lambda path: {"success": True, "nets": {}, "component_count": 1, "net_count": 2},
    )

    quality = schematic_quality_report(str(schematic_path), run_erc=False)

    assert len(KiCadSchematic.from_file(str(schematic_path)).list_wires()) == 2
    assert quality["visual_quality"]["label_inside_symbol_count"] == 0
    assert quality["visual_quality"]["unreadable_label_orientation_count"] == 0
