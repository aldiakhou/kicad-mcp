from pathlib import Path

import pytest

from kicad_mcp.utils.kicad_s_expr import (
    KiCadSchematic,
    compare_connectivity_snapshots,
    parse_s_expression,
    validate_schematic_text,
)

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "sample_schematic.kicad_sch"
CONNECTED_FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "connected_move_schematic.kicad_sch"
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


def test_connection_helpers_detect_symbol_endpoint_attachment():
    schematic = KiCadSchematic.from_file(str(CONNECTED_FIXTURE_PATH))

    points = schematic.get_symbol_connection_points("R1")
    intersecting_wires = schematic.find_wires_intersecting_symbol("R1")

    assert points == [
        {
            "wire_uuid": "wire-r1",
            "endpoint_index": 0,
            "point": {"x": 100.0, "y": 100.0},
        }
    ]
    assert intersecting_wires[0]["uuid"] == "wire-r1"
    assert intersecting_wires[0]["endpoints"][0]["inside_symbol"] is True


def test_move_symbol_with_connections_moves_attached_endpoint_and_label():
    schematic = KiCadSchematic.from_file(str(CONNECTED_FIXTURE_PATH))

    result = schematic.move_symbol_with_connections("R1", 110.0, 100.0)

    assert result["symbol"]["position"]["x"] == 110.0
    assert result["moved_wire_endpoints"][0]["new_point"] == {"x": 110.0, "y": 100.0}
    updated_wire = next(wire for wire in schematic.list_wires() if wire["uuid"] == "wire-r1")
    assert updated_wire["points"] == [{"x": 110.0, "y": 100.0}, {"x": 120.0, "y": 100.0}]
    moved_label = next(label for label in schematic.list_labels() if label["uuid"] == "label-r1")
    assert moved_label["position"]["x"] == 110.0


def test_move_symbol_with_connections_refuses_ambiguous_intersection():
    schematic = KiCadSchematic.from_file(str(CONNECTED_FIXTURE_PATH))

    with pytest.raises(ValueError, match="intersecting wire segments"):
        schematic.move_symbol_with_connections("R2", 210.0, 100.0)


def test_move_symbol_with_connections_refuses_junction_endpoint():
    schematic = KiCadSchematic.from_file(str(CONNECTED_FIXTURE_PATH))

    assert schematic.list_junctions() == [{"position": {"x": 300.0, "y": 100.0}, "uuid": None}]
    assert schematic.find_junctions_touching_point(300.0, 100.0) == [
        {"position": {"x": 300.0, "y": 100.0}, "uuid": None}
    ]

    with pytest.raises(ValueError, match="connection point has a junction"):
        schematic.move_symbol_with_connections("R3", 310.0, 100.0)


def test_move_symbol_with_connections_refuses_missing_wire_uuid():
    schematic = KiCadSchematic.from_file(str(CONNECTED_FIXTURE_PATH))

    with pytest.raises(ValueError, match="attached wire has no UUID"):
        schematic.move_symbol_with_connections("R4", 370.0, 100.0)


def test_move_label_with_wire_moves_only_attached_endpoint():
    schematic = KiCadSchematic.from_file(str(CONNECTED_FIXTURE_PATH))

    result = schematic.move_label_with_wire("label-sda", 160.0, 100.0)

    assert result["label"]["position"]["x"] == 160.0
    assert result["moved_wire_endpoints"][0]["new_point"] == {"x": 160.0, "y": 100.0}
    updated_wire = next(wire for wire in schematic.list_wires() if wire["uuid"] == "wire-sda")
    assert updated_wire["points"] == [{"x": 140.0, "y": 100.0}, {"x": 160.0, "y": 100.0}]


def test_move_label_with_wire_refuses_mid_segment_label():
    schematic = KiCadSchematic.from_file(str(CONNECTED_FIXTURE_PATH))

    with pytest.raises(ValueError, match="wire endpoint, not mid-segment"):
        schematic.move_label_with_wire("label-mid", 245.0, 100.0)


def test_connectivity_snapshot_and_comparison_report_preserved_connections():
    schematic = KiCadSchematic.from_file(str(CONNECTED_FIXTURE_PATH))
    before = schematic.target_connectivity_snapshot("symbol", "R1")

    schematic.move_symbol_with_connections("R1", 110.0, 100.0)
    after = schematic.target_connectivity_snapshot("symbol", "R1")
    comparison = compare_connectivity_snapshots("symbol", before, after)

    assert before["nearby_wires"] == ["wire-r1"]
    assert before["nearby_labels"][0]["text"] == "NET_R1"
    assert comparison["preserved"] is True
