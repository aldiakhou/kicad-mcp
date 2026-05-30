"""Tests to verify the netlist-first safe engine is the default production path."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_mcp.server import create_server
from kicad_mcp.tools import AGENT_PROFILE_TOOLS


@pytest.mark.asyncio
async def test_apply_design_intent_defaults_to_safe_engine(tmp_path: Path):
    """schematic_apply_design_intent routes through netlist-first engine by default."""
    server = create_server()
    tools = await server.get_tools()

    # Call with valid intent that will hit normalize_design_intent (and fail with missing lib_id)
    # This confirms the new engine path is taken (normalize_failed comes from the new pipeline)
    result = tools["schematic_apply_design_intent"].fn(
        str(tmp_path),
        {"parts": [{"ref": "U1", "value": "MCU", "pins": [{"number": "1", "name": "VDD", "type": "power_in"}]}]},
    )

    assert result["success"] is False
    # The key assertion: normalize_failed means the netlist-first engine was invoked
    assert result["stage"] == "normalize_failed"
    assert "lib_id" in result.get("error", "")


@pytest.mark.asyncio
async def test_apply_design_intent_legacy_env_routes_old_engine(tmp_path: Path, monkeypatch):
    """KICAD_MCP_SCHEMATIC_ENGINE=legacy routes through the old compile path."""
    monkeypatch.setenv("KICAD_MCP_SCHEMATIC_ENGINE", "legacy")
    server = create_server()
    tools = await server.get_tools()

    # Same intent without lib_id — the legacy compiler is more permissive
    result = tools["schematic_apply_design_intent"].fn(
        str(tmp_path),
        {"parts": [{"ref": "U1", "value": "MCU", "pins": [{"number": "1", "name": "VDD", "type": "power_in"}]}]},
    )

    # Legacy engine uses compile_design_intent which will succeed and try to build
    # It won't hit normalize_failed since legacy doesn't call normalize_design_intent
    assert result.get("stage") != "normalize_failed"


@pytest.mark.asyncio
async def test_start_design_intent_job_defaults_to_safe_engine(tmp_path: Path):
    """schematic_start_design_intent_job routes through netlist-first job worker."""
    server = create_server()
    tools = await server.get_tools()

    with patch(
        "kicad_mcp.tools.creation_tools._start_netlist_first_design_job"
    ) as mock_netlist_job:
        mock_netlist_job.return_value = {
            "success": True,
            "job_id": "test_123",
            "status": "pending",
        }
        result = tools["schematic_start_design_intent_job"].fn(
            str(tmp_path),
            {"parts": [{"ref": "U1", "value": "MCU", "lib_id": "MCU:MCU_TEST"}]},
        )
        mock_netlist_job.assert_called_once()
        assert result["success"] is True
        assert result["job_id"] == "test_123"


@pytest.mark.asyncio
async def test_start_design_intent_job_legacy_env_routes_old_engine(tmp_path: Path, monkeypatch):
    """KICAD_MCP_SCHEMATIC_ENGINE=legacy routes through the old job worker."""
    monkeypatch.setenv("KICAD_MCP_SCHEMATIC_ENGINE", "legacy")
    server = create_server()
    tools = await server.get_tools()

    with patch(
        "kicad_mcp.tools.creation_tools._start_design_intent_job"
    ) as mock_legacy_job:
        mock_legacy_job.return_value = {
            "success": True,
            "job_id": "test_456",
            "status": "pending",
        }
        result = tools["schematic_start_design_intent_job"].fn(
            str(tmp_path),
            {"parts": [{"ref": "U1", "value": "MCU"}]},
        )
        mock_legacy_job.assert_called_once()
        assert result["success"] is True
        assert result["job_id"] == "test_456"


@pytest.mark.asyncio
async def test_agent_profile_does_not_expose_legacy_build_tools():
    """The default agent profile should not include legacy build/connection tools."""
    assert "schematic_apply_expanded_spec" not in AGENT_PROFILE_TOOLS
    assert "schematic_build_from_spec_v2" not in AGENT_PROFILE_TOOLS
    assert "schematic_apply_connection_plan" not in AGENT_PROFILE_TOOLS
    assert "schematic_connect_pin_to_net" not in AGENT_PROFILE_TOOLS
    assert "schematic_connect_pins" not in AGENT_PROFILE_TOOLS

    # Safe tools ARE in the default profile
    assert "schematic_apply_design_intent" in AGENT_PROFILE_TOOLS
    assert "schematic_apply_design_intent_safe" in AGENT_PROFILE_TOOLS
    assert "schematic_preview_design_intent" in AGENT_PROFILE_TOOLS
    assert "schematic_start_design_intent_job" in AGENT_PROFILE_TOOLS
    assert "schematic_engine_status" in AGENT_PROFILE_TOOLS
    assert "schematic_validate_generated_schematic" in AGENT_PROFILE_TOOLS


@pytest.mark.asyncio
async def test_safe_engine_requires_netlist_match():
    """The safe apply path passes require_netlist_match=True and require_kicad_cli_verification=True."""
    server = create_server()
    tools = await server.get_tools()

    with patch(
        "kicad_mcp.tools.creation_tools._apply_via_netlist_first_engine"
    ) as mock_engine:
        mock_engine.return_value = {"success": True, "engine": "skidl_kiutils_kicad_cli"}
        result = tools["schematic_apply_design_intent"].fn(
            "/tmp/test",
            {"parts": [{"ref": "U1", "value": "MCU", "lib_id": "MCU:TEST"}]},
        )
        mock_engine.assert_called_once()
        call_kwargs = mock_engine.call_args[1]
        assert call_kwargs["require_netlist_match"] is True
        assert call_kwargs["require_kicad_cli_verification"] is True


@pytest.mark.asyncio
async def test_partial_write_blocked_without_env_var():
    """allow_partial_write=True is rejected unless KICAD_MCP_ALLOW_PARTIAL_WRITE=1."""
    server = create_server()
    tools = await server.get_tools()

    result = tools["schematic_apply_design_intent"].fn(
        "/tmp/test",
        {"parts": [{"ref": "U1", "value": "MCU", "lib_id": "MCU:TEST"}]},
        allow_partial_write=True,
    )
    assert result["success"] is False
    assert "KICAD_MCP_ALLOW_PARTIAL_WRITE" in result["error"]
    assert result["recoverable"] is True


@pytest.mark.asyncio
async def test_partial_write_allowed_with_env_var(monkeypatch):
    """allow_partial_write=True proceeds when KICAD_MCP_ALLOW_PARTIAL_WRITE=1."""
    monkeypatch.setenv("KICAD_MCP_ALLOW_PARTIAL_WRITE", "1")
    server = create_server()
    tools = await server.get_tools()

    with patch(
        "kicad_mcp.tools.creation_tools._apply_via_netlist_first_engine"
    ) as mock_engine:
        mock_engine.return_value = {"success": True}
        result = tools["schematic_apply_design_intent"].fn(
            "/tmp/test",
            {"parts": [{"ref": "U1", "value": "MCU", "lib_id": "MCU:TEST"}]},
            allow_partial_write=True,
        )
        mock_engine.assert_called_once()


@pytest.mark.asyncio
async def test_engine_status_tool_reports_readiness():
    """schematic_engine_status returns engine availability information."""
    server = create_server()
    tools = await server.get_tools()

    result = tools["schematic_engine_status"].fn()

    assert "engine" in result
    assert result["engine"] == "safe"
    assert "kicad_cli_available" in result
    assert "skidl_available" in result
    assert "kiutils_available" in result
    assert "safe_apply_ready" in result


@pytest.mark.asyncio
async def test_preview_defaults_to_safe_engine(tmp_path: Path):
    """schematic_preview_design_intent uses netlist-first preview by default."""
    server = create_server()
    tools = await server.get_tools()

    result = tools["schematic_preview_design_intent"].fn(
        str(tmp_path),
        {"parts": [{"ref": "U1", "value": "MCU", "pins": [{"number": "1", "name": "VDD", "type": "power_in"}]}]},
    )

    # New engine returns normalize_failed for missing lib_id
    assert result["success"] is False
    assert result["stage"] == "normalize_failed"
    assert result["tool"] == "schematic_preview_design_intent"


@pytest.mark.asyncio
async def test_schema_includes_recommended_apply_tool():
    """design_intent_schema output includes recommended_apply_tool."""
    server = create_server()
    tools = await server.get_tools()

    schema = tools["schematic_design_intent_schema"].fn("all")

    assert schema["success"] is True
    assert schema["recommended_apply_tool"] == "schematic_apply_design_intent_safe"
