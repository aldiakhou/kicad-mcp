from pathlib import Path

from kicad_mcp.utils.kicad_s_expr import KiCadSchematic, parse_s_expression, validate_schematic_text

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "sample_schematic.kicad_sch"


def test_parse_round_trip_preserves_schematic_structure():
    content = FIXTURE_PATH.read_text(encoding="utf-8")

    root = parse_s_expression(content)
    schematic = KiCadSchematic(root)
    reparsed = KiCadSchematic.from_text(schematic.to_text())

    assert root.head() == "kicad_sch"
    assert len(reparsed.list_symbols()) == 2
    assert len(reparsed.list_labels()) == 2
    assert len(reparsed.list_wires()) == 1


def test_validate_schematic_text_reports_counts():
    content = FIXTURE_PATH.read_text(encoding="utf-8")

    result = validate_schematic_text(content)

    assert result["valid"] is True
    assert result["symbol_count"] == 2
    assert result["label_count"] == 2
    assert result["wire_count"] == 1


def test_find_overlaps_reports_label_and_property_conflicts():
    schematic = KiCadSchematic.from_file(str(FIXTURE_PATH))

    overlaps = schematic.find_overlaps()
    overlap_types = {entry["type"] for entry in overlaps}

    assert "label-vs-symbol" in overlap_types
    assert "label-vs-pin" in overlap_types
    assert "property-vs-symbol" in overlap_types


def test_auto_arrange_symbol_properties_moves_reference_block():
    schematic = KiCadSchematic.from_file(str(FIXTURE_PATH))

    result = schematic.auto_arrange_symbol_properties("R1")

    assert result["properties"]["Reference"]["position"]["y"] == 96.0
    assert result["properties"]["Value"]["position"]["y"] == 104.0
    assert result["properties"]["Footprint"]["position"]["y"] == 108.0
