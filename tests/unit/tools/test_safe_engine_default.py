"""Tests for the simplified single-path schematic engine surface."""

from inspect import signature
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kicad_mcp.server import create_server
from kicad_mcp.tools import AGENT_PROFILE_TOOLS
import kicad_mcp.tools.creation_tools as creation_tools
from kicad_mcp.tools.design_intent_tools import _promote_candidate_schematic


@pytest.mark.asyncio
async def test_start_design_intent_job_signature_is_simple():
    server = create_server()
    tools = await server.get_tools()

    assert list(signature(tools["schematic_start_design_intent_job"].fn).parameters) == [
        "project_path",
        "intent",
    ]


@pytest.mark.asyncio
async def test_start_design_intent_job_routes_to_job_manager(tmp_path: Path):
    server = create_server()
    tools = await server.get_tools()

    with patch("kicad_mcp.tools.design_intent_tools.start_apply_job") as mock_start:
        mock_start.return_value = {"success": True, "job_id": "apply-test", "status": "queued"}

        tools["schematic_start_design_intent_job"].fn(
            str(tmp_path),
            {"parts": [{"ref": "U1", "value": "MCU", "lib_id": "MCU:TEST"}]},
        )

        mock_start.assert_called_once()
        assert mock_start.call_args.args[0] == str(tmp_path)
        assert mock_start.call_args.args[1] == {
            "parts": [{"ref": "U1", "value": "MCU", "lib_id": "MCU:TEST"}]
        }
        assert mock_start.call_args.kwargs == {}


@pytest.mark.asyncio
async def test_agent_profile_exposes_single_schematic_apply_surface():
    assert "schematic_apply_design_intent" not in AGENT_PROFILE_TOOLS
    assert "schematic_preview_design_intent" in AGENT_PROFILE_TOOLS
    assert "schematic_engine_status" in AGENT_PROFILE_TOOLS
    assert "schematic_validate_generated_schematic" in AGENT_PROFILE_TOOLS
    assert "schematic_apply_design_intent_safe" not in AGENT_PROFILE_TOOLS
    assert "schematic_start_design_intent_job" in AGENT_PROFILE_TOOLS
    assert "schematic_get_job_status" in AGENT_PROFILE_TOOLS
    assert "schematic_get_job_result" in AGENT_PROFILE_TOOLS
    assert "schematic_cancel_job" in AGENT_PROFILE_TOOLS
    assert "schematic_export_candidate_to_project" in AGENT_PROFILE_TOOLS
    assert "schematic_compare_netlists" in AGENT_PROFILE_TOOLS


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
    ):
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
    assert schema["recommended_apply_tool"] == "schematic_start_design_intent_job"
    assert schema["recommended_status_tool"] == "schematic_get_job_status"
    assert schema["recommended_result_tool"] == "schematic_get_job_result"


@pytest.mark.asyncio
async def test_schema_has_overview_and_full_example_sections():
    server = create_server()
    tools = await server.get_tools()

    overview = tools["schematic_design_intent_schema"].fn("overview")
    example = tools["schematic_design_intent_schema"].fn("full_example")

    assert overview["success"] is True
    assert overview["schema"]["candidate_artifacts"]["promotion_tool"] == (
        "schematic_export_candidate_to_project"
    )
    assert example["success"] is True
    assert "parts" in example["schema"]


def test_promote_candidate_schematic_copies_candidate_and_keeps_backup(tmp_path: Path, monkeypatch):
    project_path = tmp_path / "demo.kicad_pro"
    schematic_path = tmp_path / "demo.kicad_sch"
    project_path.write_text("{}", encoding="utf-8")
    schematic_path.write_text("original", encoding="utf-8")
    candidate_dir = tmp_path / ".kicad_mcp" / "engine_artifacts" / "apply-test" / "failed_schematics"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "demo.kicad_sch").write_text("candidate", encoding="utf-8")

    monkeypatch.setattr(
        "kicad_mcp.tools.creation_tools._generated_schematic_report",
        lambda schematic_path, run_erc: {"success": True, "schematic_path": schematic_path},
    )

    result = _promote_candidate_schematic(
        str(project_path),
        candidate_schematic_path=None,
        job_id="apply-test",
        run_erc=True,
        force=False,
    )

    assert result["success"] is True
    assert schematic_path.read_text(encoding="utf-8") == "candidate"
    assert result["backup_paths"]


@pytest.mark.asyncio
async def test_compare_netlists_tool_writes_full_diff_artifact(tmp_path: Path):
    server = create_server()
    tools = await server.get_tools()

    expected_path = tmp_path / "expected_netlist.json"
    actual_path = tmp_path / "actual.net"
    expected_path.write_text(
        json.dumps({
            "nets": {
                "VBUS": [{"ref": "J1", "pin": "VBUS"}],
                "GND": [{"ref": "J1", "pin": "A1"}],
            },
            "power_nets": ["VBUS", "GND"],
        }),
        encoding="utf-8",
    )
    actual_path.write_text(
        """
        (export (version "E")
          (nets
            (net (code 1) (name "VBUS")
              (node (ref "J1") (pin "A4") (pinfunction "VBUS_A4"))
            )
            (net (code 2) (name "GND")
              (node (ref "J1") (pin "A1") (pinfunction "GND_A1"))
            )
          )
        )
        """,
        encoding="utf-8",
    )

    result = tools["schematic_compare_netlists"].fn(
        str(expected_path),
        str(actual_path),
    )

    assert result["success"] is True
    assert result["diff_path"].endswith("netlist_compare.diff.json")
    assert Path(result["diff_path"]).exists()
    diff = json.loads(Path(result["diff_path"]).read_text(encoding="utf-8"))
    assert diff["netlist_compare"]["missing_endpoints"] == []


@pytest.mark.asyncio
async def test_export_candidate_tool_promotes_with_backup_and_validation(
    tmp_path: Path,
    monkeypatch,
):
    server = create_server()
    tools = await server.get_tools()
    project_path = tmp_path / "demo.kicad_pro"
    schematic_path = tmp_path / "demo.kicad_sch"
    project_path.write_text("{}", encoding="utf-8")
    schematic_path.write_text("original", encoding="utf-8")
    candidate_dir = tmp_path / ".kicad_mcp" / "engine_artifacts" / "apply-test" / "failed_schematics"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "demo.kicad_sch").write_text("candidate", encoding="utf-8")

    monkeypatch.setattr(
        "kicad_mcp.tools.creation_tools._generated_schematic_report",
        lambda schematic_path, run_erc: {"success": True, "schematic_path": schematic_path},
    )

    result = tools["schematic_export_candidate_to_project"].fn(
        str(project_path),
        job_id="apply-test",
        run_erc=True,
    )

    assert result["success"] is True
    assert result["backup_paths"]
    assert result["validation"]["success"] is True
    assert schematic_path.read_text(encoding="utf-8") == "candidate"
