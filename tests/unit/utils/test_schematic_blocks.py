from pathlib import Path

import pytest

from kicad_mcp.utils.kicad_s_expr import (
    KiCadSchematic,
    compare_block_connectivity_snapshots,
)

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "block_layout_schematic.kicad_sch"


def _write_safe_auto_spread_fixture(tmp_path: Path) -> Path:
    schematic_path = tmp_path / "safe_block_layout_schematic.kicad_sch"
    text = FIXTURE_PATH.read_text(encoding="utf-8")
    text = text.replace(
        '(pts (xy 35 100) (xy 20 100) (xy 20 115))',
        '(pts (xy 35 100) (xy 20 100))',
    )
    text = text.replace('  (junction\n    (xy 135 100)\n  )\n', "")
    schematic_path.write_text(text, encoding="utf-8")
    return schematic_path


def _get_block_by_symbols(schematic: KiCadSchematic, *symbols: str) -> dict:
    wanted = sorted(symbols)
    return next(block for block in schematic.find_functional_blocks() if block["symbols"] == wanted)


def test_find_functional_blocks_assigns_name_hints_and_bounds():
    schematic = KiCadSchematic.from_file(str(FIXTURE_PATH))

    blocks = schematic.find_functional_blocks()
    usb_block = _get_block_by_symbols(schematic, "J1", "R1")
    lcd_block = _get_block_by_symbols(schematic, "DS1", "R2")

    assert len(blocks) >= 4
    assert usb_block["name_hint"] == "USB-C / Connector block"
    assert lcd_block["name_hint"] == "Display block"
    assert usb_block["bounds"]["left"] <= 20.0
    assert usb_block["bounds"]["right"] >= 60.0
    assert "VBUS" in usb_block["external_connections"]


def test_preview_block_move_does_not_mutate_and_reports_boundary_wire_move():
    schematic = KiCadSchematic.from_file(str(FIXTURE_PATH))
    original_text = schematic.to_text()
    usb_block = _get_block_by_symbols(schematic, "J1", "R1")

    preview = schematic.preview_block_move(usb_block["block_id"], 25.0, 0.0)

    assert preview["success"] is True
    assert preview["planned_changes"]["translated_wires"] == ["wire-usb-internal"]
    assert preview["planned_changes"]["moved_wire_endpoints"] == ["wire-usb-boundary:0"]
    assert schematic.to_text() == original_text


def test_move_block_moves_symbols_labels_and_wires():
    schematic = KiCadSchematic.from_file(str(FIXTURE_PATH))
    usb_block = _get_block_by_symbols(schematic, "J1", "R1")

    result = schematic.move_block(usb_block["block_id"], 25.0, 0.0)

    assert result["symbols"] == ["J1", "R1"]
    assert result["translated_wires"] == ["wire-usb-internal"]
    assert result["moved_wire_endpoints"] == ["wire-usb-boundary:0"]
    assert schematic.get_symbol("J1")["position"]["x"] == 65.0
    assert next(label for label in schematic.list_labels() if label["uuid"] == "label-usb-dp")["position"]["x"] == 70.0
    assert next(wire for wire in schematic.list_wires() if wire["uuid"] == "wire-usb-internal")["points"] == [
        {"x": 70.0, "y": 40.0},
        {"x": 80.0, "y": 40.0},
    ]
    assert next(wire for wire in schematic.list_wires() if wire["uuid"] == "wire-usb-boundary")["points"] == [
        {"x": 60.0, "y": 40.0},
        {"x": 20.0, "y": 40.0},
    ]


def test_preview_block_move_refuses_complex_boundary_wire():
    schematic = KiCadSchematic.from_file(str(FIXTURE_PATH))
    mcu_block = _get_block_by_symbols(schematic, "U1")

    preview = schematic.preview_block_move(mcu_block["block_id"], 10.0, 0.0)

    assert preview["success"] is False
    assert "straight 2-point wire" in preview["planned_changes"]["refusals"][0]


def test_preview_block_move_refuses_boundary_junction():
    schematic = KiCadSchematic.from_file(str(FIXTURE_PATH))
    nfc_block = _get_block_by_symbols(schematic, "U2")

    preview = schematic.preview_block_move(nfc_block["block_id"], 10.0, 0.0)

    assert preview["success"] is False
    assert "touches a junction" in preview["planned_changes"]["refusals"][0]


def test_block_connectivity_snapshot_comparison_reports_preserved():
    schematic = KiCadSchematic.from_file(str(FIXTURE_PATH))
    usb_block = _get_block_by_symbols(schematic, "J1", "R1")
    before = schematic.block_connectivity_snapshot(usb_block["block_id"])

    schematic.move_block(usb_block["block_id"], 25.0, 0.0)
    after = schematic.block_connectivity_snapshot(symbol_refs=["J1", "R1"])
    comparison = compare_block_connectivity_snapshots(before, after)

    assert comparison["preserved"] is True
    assert after["boundary_wire_count"] == 1
    assert "VBUS" in after["external_connections"]


def test_move_block_refuses_when_preview_is_unsafe():
    schematic = KiCadSchematic.from_file(str(FIXTURE_PATH))
    mcu_block = _get_block_by_symbols(schematic, "U1")

    with pytest.raises(ValueError, match="straight 2-point wire"):
        schematic.move_block(mcu_block["block_id"], 10.0, 0.0)


def test_preview_auto_spread_blocks_refuses_partial_spread_without_mutation():
    schematic = KiCadSchematic.from_file(str(FIXTURE_PATH))
    original_text = schematic.to_text()

    preview = schematic.preview_auto_spread_blocks()

    assert preview["success"] is False
    assert any("block_003" in refusal for refusal in preview["refusals"])
    assert any("block_004" in refusal for refusal in preview["refusals"])
    assert schematic.to_text() == original_text


def test_auto_spread_blocks_raises_before_partial_mutation():
    schematic = KiCadSchematic.from_file(str(FIXTURE_PATH))
    original_text = schematic.to_text()

    with pytest.raises(ValueError, match="block_003"):
        schematic.auto_spread_blocks()

    assert schematic.to_text() == original_text


def test_auto_spread_blocks_moves_the_planned_safe_blocks(tmp_path: Path):
    schematic = KiCadSchematic.from_file(str(_write_safe_auto_spread_fixture(tmp_path)))
    before_positions = {
        reference: schematic.get_symbol(reference)["position"].copy()
        for reference in ("J1", "DS1", "U1", "U2")
    }

    preview = schematic.preview_auto_spread_blocks()
    result = schematic.auto_spread_blocks()
    moved_symbols = [tuple(move["symbols"]) for move in result["moved_blocks"]]

    assert preview["success"] is True
    assert [tuple(move["symbols"]) for move in preview["moves"]] == [("DS1", "R2"), ("U1",), ("U2",)]
    assert moved_symbols == [("DS1", "R2"), ("U1",), ("U2",)]
    assert schematic.get_symbol("J1")["position"] == before_positions["J1"]
    assert schematic.get_symbol("DS1")["position"] != before_positions["DS1"]
    assert schematic.get_symbol("U1")["position"] != before_positions["U1"]
    assert schematic.get_symbol("U2")["position"] != before_positions["U2"]
