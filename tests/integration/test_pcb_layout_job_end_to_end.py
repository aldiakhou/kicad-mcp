from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kicad_mcp.server import create_server
from kicad_mcp.utils.kicad_cli import get_kicad_cli_path


def _kicad_cli_available() -> bool:
    return get_kicad_cli_path(required=False) is not None


pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_kicad,
    pytest.mark.skipif(not _kicad_cli_available(), reason="KiCad CLI not available"),
]


def _led_resistor_intent() -> dict:
    return {
        "parts": [
            {
                "ref": "J1",
                "lib_id": "Connector_Generic:Conn_01x02",
                "value": "POWER",
                "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
            },
            {
                "ref": "R1",
                "lib_id": "Device:R",
                "value": "1k",
                "footprint": "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal",
            },
            {
                "ref": "D1",
                "lib_id": "Device:LED",
                "value": "LED",
                "footprint": "LED_THT:LED_D5.0mm",
            },
        ],
        "bulk_connections": [
            {"net": "+5V", "endpoints": [{"ref": "J1", "pin": "1"}, {"ref": "R1", "pin": "1"}]},
            {"net": "LED_A", "endpoints": [{"ref": "R1", "pin": "2"}, {"ref": "D1", "pin": "A"}]},
            {"net": "GND", "endpoints": [{"ref": "J1", "pin": "2"}, {"ref": "D1", "pin": "K"}]},
        ],
    }


def _pcb_intent() -> dict:
    return {
        "board": {"width_mm": 50, "height_mm": 35, "shape": "rectangular"},
        "placement": {
            "style": "functional",
            "preserve_existing_placement": False,
            "components": [
                {"ref": "J1", "x": 6, "y": 18, "angle": 90},
                {"ref": "R1", "x": 24, "y": 16, "angle": 0},
                {"ref": "D1", "x": 38, "y": 16, "angle": 0},
            ],
        },
        "routing": {
            "mode": "auto",
            "layer": "F.Cu",
            "track_width_mm": 0.25,
            "clearance_mm": 0.35,
            "grid_mm": 1.27,
        },
        "validation": {"run_drc": False, "require_clean_drc": False},
        "fabrication": {"include_step": False, "include_ipc2581": False, "run_drc": False},
    }


async def _wait_job(tools: dict, status_tool: str, result_tool: str, job_id: str) -> dict:
    status = {}
    for _ in range(180):
        status = tools[status_tool].fn(job_id)
        if status["status"] in {"succeeded", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.5)
    assert status["status"] == "succeeded", status
    result = tools[result_tool].fn(job_id)
    assert result["success"] is True, result
    return result["result"]


@pytest.mark.asyncio
async def test_pcb_layout_job_end_to_end(tmp_path: Path):
    server = create_server()
    tools = await server.get_tools()
    assert "schematic_apply_design_intent" not in tools
    assert "pcb_add_track" not in tools

    project = tools["create_kicad_project"].fn(str(tmp_path), "pcb_layout", True, True, "A4")
    assert project["success"] is True

    schematic_job = tools["schematic_start_design_intent_job"].fn(
        project["project_path"],
        _led_resistor_intent(),
    )
    schematic_result = await _wait_job(
        tools,
        "schematic_get_job_status",
        "schematic_get_job_result",
        schematic_job["job_id"],
    )
    assert schematic_result["stage"] == "schematic_committed"

    preview = tools["pcb_preview_layout_intent"].fn(project["project_path"], _pcb_intent())
    assert preview["success"] is True
    assert preview["ready_to_start"] is True
    assert preview["summary"]["footprint_count"] == 3

    pcb_job = tools["pcb_start_layout_job"].fn(project["project_path"], _pcb_intent())
    pcb_result = await _wait_job(
        tools,
        "pcb_get_layout_job_status",
        "pcb_get_layout_job_result",
        pcb_job["job_id"],
    )
    assert pcb_result["stage"] == "pcb_layout_committed"
    assert pcb_result["quality"]["footprint_count"] == 3
    assert pcb_result["routing"]["changed_objects"]["routed_count"] == 3
    assert pcb_result["quality"]["track_count"] > 0
    assert pcb_result["quality"]["ratsnest_connection_count"] == 0
    assert pcb_result["quality"]["routing_complete"] is True

    validated = tools["pcb_validate_layout"].fn(project["project_path"], False, False)
    assert validated["success"] is True
    assert validated["stage"] == "layout_valid"

    exported = tools["pcb_export_fabrication_package"].fn(
        project["project_path"],
        None,
        False,
        False,
        False,
    )
    assert exported["success"] is True
    assert exported["artifact_count"] > 0
    assert Path(exported["zip_path"]).exists()
