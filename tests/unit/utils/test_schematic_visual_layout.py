from pathlib import Path

from kicad_mcp.utils import schematic_builder
from kicad_mcp.utils.kicad_s_expr import KiCadSchematic
from kicad_mcp.utils.schematic_builder import (
    _build_in_memory_schematic,
    build_schematic_from_spec_v2,
    normalize_build_spec_v2,
    schematic_quality_report,
)
from kicad_mcp.utils.schematic_pins import (
    _external_stub_points,
    _place_external_label_endpoint,
    _readable_label_angle,
    _rects_intersect,
    _text_rect,
)
from kicad_mcp.utils.schematic_visual_layout import (
    _candidate_papers,
    apply_visual_layout_to_v2_spec,
)


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


def _sensor_part(index: int) -> dict:
    return {
        "ref": f"U{index}",
        "value": f"SENSOR_{index}",
        "footprint": "Package_LGA:LGA-8_2.0x2.5mm_P0.65mm",
        "pins": [
            {"number": "1", "name": "SCL", "type": "bidirectional"},
            {"number": "2", "name": "SDA", "type": "bidirectional"},
            {"number": "3", "name": "INT", "type": "output"},
            {"number": "4", "name": "CS", "type": "input"},
            {"number": "5", "name": "VDD", "type": "power_in"},
            {"number": "6", "name": "GND", "type": "power_in"},
        ],
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


def test_auto_paper_candidates_include_us_sizes_and_a0():
    assert "USLetter" in _candidate_papers("USLetter", "auto", "A0")
    assert "USLegal" in _candidate_papers("USLetter", "auto", "A0")
    assert _candidate_papers("A1", "auto", "A0") == ["A1", "A0"]


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


def test_external_stub_label_endpoint_shifts_away_from_existing_label():
    schematic = KiCadSchematic.empty()
    schematic.add_label("GPIO_A", 10.16, 10.16, "local", 0)

    endpoint = _place_external_label_endpoint(
        schematic,
        {"x": 2.54, "y": 10.16},
        0,
        "GPIO_B",
        "auto",
        7.62,
    )

    assert endpoint != {"x": 10.16, "y": 10.16}
    existing_rect = _text_rect({"x": 10.16, "y": 10.16}, "GPIO_A", 0)
    new_rect = _text_rect(endpoint, "GPIO_B", 0)
    assert not _rects_intersect(existing_rect, new_rect, padding=0.5)


def test_large_generated_sensor_layout_stays_inside_page_without_symbol_overlaps(tmp_path: Path):
    primary = [_large_mcu_part(), *[_sensor_part(index) for index in range(2, 13)]]
    generated = []
    for target_index in range(1, 13):
        generated.extend(
            [
                {
                    "ref": f"C{target_index}A",
                    "lib_id": "Device:C",
                    "value": "100n",
                    "footprint": "Capacitor_SMD:C_0603_1608Metric",
                    "generated_by": "decoupling",
                    "target": f"U{target_index}",
                },
                {
                    "ref": f"R{target_index}A",
                    "lib_id": "Device:R",
                    "value": "10k",
                    "footprint": "Resistor_SMD:R_0603_1608Metric",
                    "generated_by": "pullup",
                    "target": f"U{target_index}",
                },
            ]
        )
    spec = apply_visual_layout_to_v2_spec(
        {
            "parts": [*primary, *generated],
            "nets": {},
            "layout_hints": {"paper_strategy": "auto", "max_paper": "A1"},
        },
        page="A4",
    )
    assert len(spec["parts"]) == 36
    assert spec["layout_hints"]["visual_layout"]["enabled"] is True
    assert spec["paper"] in {"A4", "A3", "A2", "A1"}

    schematic_path = tmp_path / "sensor_fusion.kicad_sch"
    schematic = _build_in_memory_schematic(str(schematic_path), normalize_build_spec_v2(spec))
    schematic_path.write_text(schematic.to_text(), encoding="utf-8")
    quality = schematic_quality_report(str(schematic_path), run_erc=False)

    visual = quality["visual_quality"]
    assert quality["outside_page_count"] == 0
    assert visual["symbol_overlap_count"] == 0
    assert visual["label_inside_symbol_count"] == 0


def test_fixed_paper_reports_layout_failure_when_design_cannot_fit():
    spec = apply_visual_layout_to_v2_spec(
        {
            "paper": "A4",
            "parts": [_large_mcu_part() | {"ref": f"U{index}"} for index in range(1, 12)],
            "nets": {},
            "layout_hints": {"fixed_paper": True, "paper_strategy": "fixed"},
        },
        page="A4",
    )

    visual = spec["layout_hints"]["visual_layout"]
    assert spec["paper"] == "A4"
    assert visual["enabled"] is False
    assert visual["layout_failed"] is True
    assert visual["unplaced_refs"]


def test_strict_visual_gate_blocks_build_when_fixed_page_layout_fails(monkeypatch):
    def fail_build(*_args, **_kwargs):
        raise AssertionError("build_schematic_from_spec should not be called after layout failure")

    monkeypatch.setattr("kicad_mcp.utils.schematic_builder.build_schematic_from_spec", fail_build)

    result = build_schematic_from_spec_v2(
        "demo.kicad_pro",
        {
            "paper": "A4",
            "parts": [_large_mcu_part() | {"ref": f"U{index}"} for index in range(1, 12)],
            "nets": {},
            "layout_hints": {
                "fixed_paper": True,
                "paper_strategy": "fixed",
                "visual_gate": "strict",
            },
        },
        apply_default_visual_layout=True,
    )

    assert result["success"] is False
    assert result["stage"] == "layout_failed"
    assert result["recommended_next_arguments"]["paper"] == "A3"


def test_prewrite_visual_gate_resolves_real_project_schematic_path(tmp_path: Path, monkeypatch):
    project_path = tmp_path / "sensor_fusion.kicad_pro"
    schematic_path = tmp_path / "sensor_fusion.kicad_sch"
    project_path.write_text("(kicad_project)\n", encoding="utf-8")
    schematic_path.write_text(KiCadSchematic.empty().to_text(), encoding="utf-8")
    captured: dict[str, str] = {}

    def fake_in_memory(path, normalized_spec):
        captured["schematic_path"] = path
        return _build_in_memory_schematic(path, normalized_spec)

    monkeypatch.setattr(schematic_builder, "_build_in_memory_schematic", fake_in_memory)
    monkeypatch.setattr(
        schematic_builder,
        "build_schematic_from_spec",
        lambda *_args, **_kwargs: {"success": True},
    )

    result = build_schematic_from_spec_v2(
        str(project_path),
        {
            "custom_parts": [
                {
                    "ref": "U1",
                    "value": "IC",
                    "pins": [{"number": "1", "name": "A"}, {"number": "2", "name": "B"}],
                }
            ],
            "nets": {},
            "layout_hints": {"visual_gate": "strict"},
        },
    )

    assert result["success"] is True
    assert captured["schematic_path"] == str(schematic_path)


def test_top_bottom_pin_labels_use_horizontal_dogleg_strategy():
    assert _readable_label_angle(90) == 0.0
    assert _readable_label_angle(270) == 0.0

    start = {"x": 20.32, "y": 20.32}
    end = {"x": 30.48, "y": 27.94}
    points = _external_stub_points(start, end, 90, "auto", 7.62)

    assert points == [start, {"x": 20.32, "y": 27.94}, end]
