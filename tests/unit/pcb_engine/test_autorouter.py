from __future__ import annotations

from kicad_mcp.pcb_engine.autorouter import autoroute_pcb
from kicad_mcp.pcb_engine.intent import normalize_pcb_layout_intent
from kicad_mcp.tools import creation_tools as ct
from kicad_mcp.utils.kicad_pcb_s_expr import KiCadPcb
from kicad_mcp.utils.kicad_s_expr import SExprAtom, SExprList


def _single_pad_footprint() -> SExprList:
    return SExprList(
        [
            SExprAtom("footprint"),
            SExprAtom("Test:SinglePad", quoted=True),
            SExprList(
                [
                    SExprAtom("property"),
                    SExprAtom("Reference", quoted=True),
                    SExprAtom("REF**", quoted=True),
                    SExprList([SExprAtom("at"), SExprAtom("0"), SExprAtom("0"), SExprAtom("0")]),
                ]
            ),
            SExprList(
                [
                    SExprAtom("property"),
                    SExprAtom("Value", quoted=True),
                    SExprAtom("SinglePad", quoted=True),
                    SExprList([SExprAtom("at"), SExprAtom("0"), SExprAtom("0"), SExprAtom("0")]),
                ]
            ),
            SExprList(
                [
                    SExprAtom("pad"),
                    SExprAtom("1", quoted=True),
                    SExprAtom("thru_hole"),
                    SExprAtom("circle"),
                    SExprList([SExprAtom("at"), SExprAtom("0"), SExprAtom("0")]),
                    SExprList([SExprAtom("size"), SExprAtom("2"), SExprAtom("2")]),
                    SExprList([SExprAtom("drill"), SExprAtom("1")]),
                    SExprList(
                        [
                            SExprAtom("layers"),
                            SExprAtom("*.Cu", quoted=True),
                            SExprAtom("*.Mask", quoted=True),
                        ]
                    ),
                ]
            ),
        ]
    )


def test_autorouter_routes_around_footprint_obstacle():
    pcb = KiCadPcb.empty(board_width_mm=40, board_height_mm=22)
    footprint = _single_pad_footprint()
    pcb.add_footprint("Test:SinglePad", footprint, "J1", "A", 5, 11, net_assignments={"1": "NET"})
    pcb.add_footprint("Test:SinglePad", footprint, "J2", "B", 35, 11, net_assignments={"1": "NET"})
    pcb.add_footprint("Test:SinglePad", footprint, "U1", "BLOCK", 20, 11)

    result = autoroute_pcb(
        pcb,
        40,
        22,
        track_width_mm=0.25,
        clearance_mm=1.0,
        grid_mm=1.0,
    )

    assert result["success"] is True
    assert result["routed_count"] == 1
    assert result["failed_count"] == 0
    assert len(pcb.list_track_segments()) > 0

    blocker = pcb.find_footprint("U1")
    assert blocker is not None
    bounds = pcb.footprint_bounds(blocker)
    for segment in pcb.list_track_segments():
        for point in (segment["start"], segment["end"]):
            assert not (
                bounds["left"] <= point["x"] <= bounds["right"]
                and bounds["top"] <= point["y"] <= bounds["bottom"]
            )


def test_quality_report_treats_routed_copper_as_connected():
    pcb = KiCadPcb.empty(board_width_mm=30, board_height_mm=15)
    footprint = _single_pad_footprint()
    pcb.add_footprint("Test:SinglePad", footprint, "J1", "A", 5, 7, net_assignments={"1": "NET"})
    pcb.add_footprint("Test:SinglePad", footprint, "J2", "B", 25, 7, net_assignments={"1": "NET"})

    before = ct._build_ratsnest("demo.kicad_pro", "demo.kicad_pcb", pcb)
    assert before["connection_count"] == 1

    pcb.add_track("NET", [{"x": 5, "y": 7}, {"x": 25, "y": 7}])
    after = ct._pcb_quality_report("demo.kicad_pro", "demo.kicad_pcb", pcb)

    assert after["ratsnest_connection_count"] == 0
    assert after["routing_complete"] is True
    assert after["routing_status"] == "routed_needs_drc"


def test_pcb_intent_max_connections_zero_means_no_cap():
    normalized = normalize_pcb_layout_intent({"routing": {"mode": "auto", "max_connections": 0}})

    assert normalized["routing"]["max_connections"] is None
