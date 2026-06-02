from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.pcb_engine.backends import get_board_backend
from kicad_mcp.pcb_engine.intent import normalize_pcb_layout_intent
from kicad_mcp.utils.pcbnew_runtime import pcbnew_runtime_status


def test_sexpr_backend_can_be_selected_explicitly():
    backend = get_board_backend("sexpr")

    assert backend.name == "sexpr"
    assert backend.status()["available"] is True


def test_pcbnew_runtime_status_has_clear_shape():
    status = pcbnew_runtime_status(force_refresh=True)

    assert status["backend"] == "pcbnew"
    assert "python_version" in status
    assert "python_executable" in status
    assert "available" in status
    if not status["available"]:
        assert status.get("error")


def test_normalize_pcb_layout_intent_supports_vias_and_zones():
    normalized = normalize_pcb_layout_intent(
        {
            "board": {"width_mm": 40, "height_mm": 25},
            "routing": {
                "mode": "auto",
                "engine": "internal",
                "vias": {"enabled": True, "drill_mm": 0.25, "diameter_mm": 0.55},
            },
            "zones": [{"net": "GND", "layer": "B.Cu", "margin_mm": 0.75}],
        }
    )

    assert normalized["routing"]["engine"] == "internal"
    assert normalized["routing"]["vias"] == {
        "enabled": True,
        "drill_mm": 0.25,
        "diameter_mm": 0.55,
    }
    assert normalized["zones"][0]["net"] == "GND"
    assert normalized["zones"][0]["outline"] == [
        {"x": 0.75, "y": 0.75},
        {"x": 39.25, "y": 0.75},
        {"x": 39.25, "y": 24.25},
        {"x": 0.75, "y": 24.25},
    ]


@pytest.mark.skipif(
    not pcbnew_runtime_status().get("available"),
    reason="pcbnew is not importable in this Python runtime",
)
def test_pcbnew_backend_round_trip_tracks_vias_and_zones(tmp_path: Path):
    backend = get_board_backend("pcbnew")
    board = backend.empty(35, 22)
    board.ensure_net("GND")
    board.add_footprint(
        "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal",
        None,
        "R1",
        "1k",
        10,
        10,
    )
    board.assign_footprint_pad_nets("R1", {"1": "GND"})
    board.add_track("GND", [{"x": 10, "y": 10}, {"x": 15, "y": 10}], "F.Cu", 0.25)
    board.add_via("GND", 15, 10)
    board.add_zone(
        "GND",
        "B.Cu",
        [
            {"x": 0.5, "y": 0.5},
            {"x": 34.5, "y": 0.5},
            {"x": 34.5, "y": 21.5},
            {"x": 0.5, "y": 21.5},
        ],
    )

    pcb_path = tmp_path / "round_trip.kicad_pcb"
    board.save_to(str(pcb_path))
    loaded = backend.from_file(str(pcb_path))

    assert loaded.backend_name == "pcbnew"
    assert loaded.list_footprints()[0]["reference"] == "R1"
    assert loaded.footprint_pad_positions()[0]["net_name"] == "GND"
    assert len(loaded.list_track_segments()) == 1
    assert len(loaded.list_vias()) == 1
    cleanup = loaded.clear_routing(include_zones=True)
    assert cleanup["removed_segments"] == 1
    assert cleanup["removed_vias"] == 1
    assert cleanup["removed_zones"] == 1
