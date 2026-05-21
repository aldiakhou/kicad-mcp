from pathlib import Path

from kicad_mcp.utils.kicad_s_expr import KiCadSchematic

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "messy_card_reader_like_schematic.kicad_sch"
UNSAFE_FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "block_layout_schematic.kicad_sch"


def test_cleanup_report_returns_blocks_overlaps_and_recommendations():
    schematic = KiCadSchematic.from_file(str(FIXTURE_PATH))

    report = schematic.schematic_cleanup_report()

    assert report["success"] is True
    assert report["symbols"] == 7
    assert report["labels"] >= 7
    assert report["wires"] >= 5
    assert report["blocks"]
    assert report["overlaps"]
    assert "Auto-arrange symbol properties" in report["recommendations"]
    assert "Export SVG preview" in report["recommendations"]


def test_preview_cleanup_does_not_mutate_and_plans_ordered_block_moves():
    schematic = KiCadSchematic.from_file(str(FIXTURE_PATH))
    original_text = schematic.to_text()

    preview = schematic.preview_cleanup()
    move_names = [move["name_hint"] for move in preview["cleanup_plan"]["block_moves"]]
    block_names = [block["name_hint"] for block in preview["cleanup_plan"]["blocks"]]

    assert preview["success"] is True
    assert "USB-C / Connector block" in block_names
    assert move_names[0] == "Power block"
    assert "MCU block" in move_names
    assert move_names[-1] == "Display block"
    assert preview["cleanup_plan"]["property_moves"]
    assert schematic.to_text() == original_text


def test_preview_cleanup_refuses_unsafe_block_moves():
    schematic = KiCadSchematic.from_file(str(UNSAFE_FIXTURE_PATH))
    original_text = schematic.to_text()

    preview = schematic.preview_cleanup()

    assert preview["success"] is False
    assert any("straight 2-point wire" in refusal for refusal in preview["cleanup_plan"]["refusals"])
    assert schematic.to_text() == original_text


def test_auto_arrange_symbol_properties_all_moves_reference_and_value():
    schematic = KiCadSchematic.from_file(str(FIXTURE_PATH))

    preview = schematic.preview_auto_arrange_symbol_properties_all()
    result = schematic.auto_arrange_symbol_properties_all()

    assert preview["property_moves"]
    j1_reference = next(
        move
        for move in result["properties_arranged"]
        if move["reference"] == "J1" and move["property_name"] == "Reference"
    )
    j1_value = next(
        move
        for move in result["properties_arranged"]
        if move["reference"] == "J1" and move["property_name"] == "Value"
    )
    assert j1_reference["to"]["y"] < schematic.get_symbol("J1")["position"]["y"]
    assert j1_value["to"]["y"] > schematic.get_symbol("J1")["position"]["y"]


def test_apply_cleanup_uses_preview_move_symbols_without_block_id_drift():
    schematic = KiCadSchematic.from_file(str(FIXTURE_PATH))

    preview = schematic.preview_cleanup()
    result = schematic.apply_cleanup()

    assert preview["success"] is True
    assert [tuple(move["symbols"]) for move in preview["cleanup_plan"]["block_moves"]] == [
        tuple(move["symbols"]) for move in result["blocks_moved"]
    ]
    assert ("J1", "R1") not in [tuple(move["symbols"]) for move in result["blocks_moved"]]
