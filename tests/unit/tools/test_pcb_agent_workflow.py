from __future__ import annotations

from inspect import signature
from unittest.mock import patch

import pytest

from kicad_mcp.server import create_server
from kicad_mcp.tools import AGENT_PROFILE_TOOLS


@pytest.mark.asyncio
async def test_agent_profile_exposes_pcb_layout_job_surface():
    assert "pcb_design_intent_schema" in AGENT_PROFILE_TOOLS
    assert "pcb_preview_layout_intent" in AGENT_PROFILE_TOOLS
    assert "pcb_start_layout_job" in AGENT_PROFILE_TOOLS
    assert "pcb_get_layout_job_status" in AGENT_PROFILE_TOOLS
    assert "pcb_get_layout_job_result" in AGENT_PROFILE_TOOLS
    assert "pcb_cancel_layout_job" in AGENT_PROFILE_TOOLS
    assert "pcb_validate_layout" in AGENT_PROFILE_TOOLS
    assert "pcb_export_fabrication_package" in AGENT_PROFILE_TOOLS
    assert "pcb_add_track" not in AGENT_PROFILE_TOOLS
    assert "pcb_route_between_pads" not in AGENT_PROFILE_TOOLS


@pytest.mark.asyncio
async def test_pcb_layout_job_signature_is_simple():
    server = create_server()
    tools = await server.get_tools()

    assert list(signature(tools["pcb_start_layout_job"].fn).parameters) == [
        "project_path",
        "intent",
    ]


@pytest.mark.asyncio
async def test_pcb_start_layout_job_routes_to_job_manager(tmp_path):
    server = create_server()
    tools = await server.get_tools()

    with patch("kicad_mcp.tools.pcb_tools.start_layout_job") as mock_start:
        mock_start.return_value = {"success": True, "job_id": "pcb-test", "status": "queued"}

        tools["pcb_start_layout_job"].fn(
            str(tmp_path / "demo.kicad_pro"),
            {"board": {"width_mm": 40, "height_mm": 30}},
        )

    mock_start.assert_called_once()
    assert mock_start.call_args.args[0] == str(tmp_path / "demo.kicad_pro")
    assert mock_start.call_args.args[1]["board"]["width_mm"] == 40


@pytest.mark.asyncio
async def test_pcb_schema_recommends_async_workflow():
    server = create_server()
    tools = await server.get_tools()

    schema = tools["pcb_design_intent_schema"].fn("all")

    assert schema["success"] is True
    assert schema["recommended_preview_tool"] == "pcb_preview_layout_intent"
    assert schema["recommended_apply_tool"] == "pcb_start_layout_job"
    assert schema["recommended_status_tool"] == "pcb_get_layout_job_status"
    assert schema["recommended_result_tool"] == "pcb_get_layout_job_result"
    assert schema["schemas"]["routing"]["example"]["mode"] == "auto"
    assert "clearance_mm" in schema["schemas"]["routing"]["fields"]
