from pathlib import Path

import pytest

from kicad_mcp.server import create_server
from kicad_mcp.utils.kicad_cli import get_kicad_cli_path
from tests.integration.test_agent_bulk_intent_workflow import _bulk_stm32_sensor_intent


def _kicad_cli_available() -> bool:
    return get_kicad_cli_path(required=False) is not None


pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_kicad,
    pytest.mark.skipif(not _kicad_cli_available(), reason="KiCad CLI not available"),
]


@pytest.mark.asyncio
async def test_agent_visual_layout_readability_has_no_blocking_visual_overlaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("KICAD_MCP_TOOL_PROFILE", "all")
    server = create_server()
    tools = await server.get_tools()
    project = tools["create_kicad_project"].fn(str(tmp_path), "visual_layout", True, True, "A3")

    result = tools["schematic_apply_design_intent"].fn(
        project["project_path"],
        _bulk_stm32_sensor_intent(),
        "update",
        False,
        False,
        "full",
        False,
        True,
        "readable",
    )

    assert result["success"] is True
    assert result["verification"]["native_netlist_success"] is True
    assert result["verification"]["missing_connection_count"] == 0
    visual = result["quality_report"]["visual_quality"]
    assert visual["symbol_overlap_count"] == 0
    assert visual["label_inside_symbol_count"] == 0
    assert visual["unreadable_label_orientation_count"] == 0
    assert visual["blocking_count"] == 0
