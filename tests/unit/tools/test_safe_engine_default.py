"""Tests for the simplified single-path schematic engine surface."""

from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import kicad_mcp.tools.creation_tools as creation_tools
from kicad_mcp.server import create_server
from kicad_mcp.tools import AGENT_PROFILE_TOOLS


@pytest.mark.asyncio
async def test_apply_design_intent_signature_is_simple():
    server = create_server()
    tools = await server.get_tools()

    assert list(signature(tools["schematic_apply_design_intent"].fn).parameters) == [
        "project_path",
        "intent",
    ]


@pytest.mark.asyncio
async def test_apply_design_intent_routes_to_required_engine(tmp_path: Path):
    server = create_server()
    tools = await server.get_tools()

    with patch("kicad_mcp.tools.creation_tools._apply_via_netlist_first_engine") as mock_engine:
        mock_engine.return_value = {"success": True, "engine": "skidl_kiutils_kicad_cli"}

        tools["schematic_apply_design_intent"].fn(
            str(tmp_path),
            {"parts": [{"ref": "U1", "value": "MCU", "lib_id": "MCU:TEST"}]},
        )

        mock_engine.assert_called_once()
        assert mock_engine.call_args.args[0] == str(tmp_path)
        assert mock_engine.call_args.kwargs["strict"] is True
        assert mock_engine.call_args.kwargs["visual_style"] == "professional_blocks"
        assert mock_engine.call_args.kwargs["require_netlist_match"] is True
        assert mock_engine.call_args.kwargs["require_kicad_cli_verification"] is True


@pytest.mark.asyncio
async def test_agent_profile_exposes_single_schematic_apply_surface():
    assert "schematic_apply_design_intent" in AGENT_PROFILE_TOOLS
    assert "schematic_preview_design_intent" in AGENT_PROFILE_TOOLS
    assert "schematic_engine_status" in AGENT_PROFILE_TOOLS
    assert "schematic_validate_generated_schematic" in AGENT_PROFILE_TOOLS
    assert "schematic_apply_design_intent_safe" not in AGENT_PROFILE_TOOLS
    assert "schematic_start_design_intent_job" not in AGENT_PROFILE_TOOLS
    assert "schematic_get_job_status" not in AGENT_PROFILE_TOOLS
    assert "schematic_get_job_result" not in AGENT_PROFILE_TOOLS
    assert "schematic_cancel_job" not in AGENT_PROFILE_TOOLS


@pytest.mark.asyncio
async def test_engine_status_tool_reports_required_runtime():
    server = create_server()
    tools = await server.get_tools()

    result = tools["schematic_engine_status"].fn()

    assert result["engine"] == "skidl_kiutils_kicad_cli"
    assert result["skidl"] == "installed"
    assert result["kiutils"] == "installed"
    assert result["kicad_skip"] == "installed"
    assert "kicad_cli_available" in result
    assert "ready" in result


def test_preview_returns_ready_to_apply_false_on_blocking_generation_issue(monkeypatch):
    canonical = SimpleNamespace(parts=[], endpoints=[])
    compile_result = SimpleNamespace(success=True, net_count=1)
    sheet_plan = SimpleNamespace(sheets={"root": []})
    issue = SimpleNamespace(
        type="unplaced_symbol",
        ref="U1",
        message="Symbol U1 has no placement",
        severity="blocking",
    )
    lint_result = SimpleNamespace(blocking_count=1, warning_count=0, issues=[issue])

    with patch(
        "kicad_mcp.schematic_engine.normalize.normalize_design_intent",
        return_value=canonical,
    ), patch(
        "kicad_mcp.schematic_engine.sheet_planner.plan_sheets",
        return_value=sheet_plan,
    ), patch(
        "kicad_mcp.schematic_engine.visual_lint.visual_lint",
        return_value=lint_result,
    ), patch("kicad_mcp.schematic_engine.skidl_compiler.SkidlCompiler") as mock_compiler:
        mock_compiler.return_value.compile.return_value = compile_result

        result = creation_tools._preview_design_intent_netlist_first(
            "/tmp/test.kicad_pro",
            {"parts": []},
        )

    assert result["success"] is True
    assert result["ready_to_apply"] is False
    assert result["blocking_issue_count"] == 1
    assert result["issues"][0]["category"] == "blocking_generation_issue"


@pytest.mark.asyncio
async def test_schema_includes_recommended_apply_tool():
    server = create_server()
    tools = await server.get_tools()

    schema = tools["schematic_design_intent_schema"].fn("all")

    assert schema["success"] is True
    assert schema["recommended_apply_tool"] == "schematic_apply_design_intent"
