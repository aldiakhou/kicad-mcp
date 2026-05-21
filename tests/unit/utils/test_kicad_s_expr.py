from pathlib import Path

from kicad_mcp.utils.kicad_s_expr import KiCadSchematic, parse_s_expression, validate_schematic_text

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "sample_schematic.kicad_sch"
EXPECTED_REFERENCE_Y = 96.0
EXPECTED_VALUE_Y = 104.0
EXPECTED_FOOTPRINT_Y = 108.0


def test_schematic_parse_and_serialize_round_trip_preserves_structure():
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


def test_auto_arrange_symbol_properties_arranges_properties_vertically():
    schematic = KiCadSchematic.from_file(str(FIXTURE_PATH))

    result = schematic.auto_arrange_symbol_properties("R1")

    assert result["properties"]["Reference"]["position"]["y"] == EXPECTED_REFERENCE_Y
    assert result["properties"]["Value"]["position"]["y"] == EXPECTED_VALUE_Y
    assert result["properties"]["Footprint"]["position"]["y"] == EXPECTED_FOOTPRINT_Y


def test_connectivity_risk_detects_attached_symbols_and_labels():
    schematic = KiCadSchematic.from_file(str(FIXTURE_PATH))

    symbol_risk = schematic.symbol_connectivity_risk("R1")
    label_risk = schematic.label_connectivity_risk("label-1")
    auto_arrange_risks = schematic.auto_arrange_label_risks()

    assert symbol_risk["attached"] is True
    assert any(attachment["type"] == "wire" for attachment in symbol_risk["attachments"])
    assert label_risk["attached"] is True
    assert any(attachment["type"] == "wire" for attachment in label_risk["attachments"])
    assert auto_arrange_risks
