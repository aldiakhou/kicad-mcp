from pathlib import Path
import shutil

import pytest

from kicad_mcp.server import create_server

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "connected_move_schematic.kicad_sch"


def _copy_fixture(tmp_path: Path) -> Path:
    schematic_path = tmp_path / "connected_tool_demo.kicad_sch"
    shutil.copy2(FIXTURE_PATH, schematic_path)
    return schematic_path


@pytest.mark.asyncio
async def test_connectivity_move_preview_does_not_mutate_and_move_persists_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    schematic_path = _copy_fixture(tmp_path)
    original_text = schematic_path.read_text(encoding="utf-8")
    server = create_server()
    tools = await server.get_tools()

    monkeypatch.setattr(
        "kicad_mcp.tools.schematic_edit_tools.validate_schematic_with_cli_export",
        lambda path: {"success": True, "skipped": True, "reason": "test"},
    )
    monkeypatch.setattr(
        "kicad_mcp.utils.transactional_edit.validate_schematic_with_cli_export",
        lambda path: {"success": True, "skipped": True, "reason": "test"},
    )

    snapshot_result = tools["schematic_connectivity_snapshot"].fn(str(schematic_path))
    preview_result = tools["schematic_preview_connectivity_move"].fn(
        str(schematic_path), "symbol", "R1", 110.0, 100.0, None
    )

    assert snapshot_result["success"] is True
    assert snapshot_result["snapshot"]["symbols"]["R1"]["nearby_wires"] == ["wire-r1"]
    assert preview_result["success"] is True
    assert preview_result["preview"]["changed_objects"]["moved_wire_endpoints"][0]["new_point"] == {
        "x": 110.0,
        "y": 100.0,
    }
    assert schematic_path.read_text(encoding="utf-8") == original_text

    move_symbol_result = await tools["schematic_move_symbol_with_connections"].fn(
        str(schematic_path), "R1", 110.0, 100.0, None, True, None
    )
    assert move_symbol_result["success"] is True
    assert move_symbol_result["changed_objects"]["symbol"]["position"]["x"] == 110.0
    assert move_symbol_result["validation"]["post_write"]["connectivity_snapshot"] == "preserved"

    move_label_result = await tools["schematic_move_label_with_wire"].fn(
        str(schematic_path), "label-sda", 160.0, 100.0, None, True, None
    )
    assert move_label_result["success"] is True
    assert move_label_result["changed_objects"]["moved_wire_endpoints"][0]["new_point"] == {
        "x": 160.0,
        "y": 100.0,
    }


@pytest.mark.asyncio
async def test_connectivity_move_tools_refuse_unsupported_cases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    schematic_path = _copy_fixture(tmp_path)
    server = create_server()
    tools = await server.get_tools()

    monkeypatch.setattr(
        "kicad_mcp.tools.schematic_edit_tools.validate_schematic_with_cli_export",
        lambda path: {"success": True, "skipped": True, "reason": "test"},
    )
    monkeypatch.setattr(
        "kicad_mcp.utils.transactional_edit.validate_schematic_with_cli_export",
        lambda path: {"success": True, "skipped": True, "reason": "test"},
    )

    move_symbol_result = await tools["schematic_move_symbol_with_connections"].fn(
        str(schematic_path), "R2", 210.0, 100.0, None, True, None
    )
    move_label_result = await tools["schematic_move_label_with_wire"].fn(
        str(schematic_path), "label-mid", 245.0, 100.0, None, True, None
    )

    assert move_symbol_result["success"] is False
    assert "intersecting wire segments" in move_symbol_result["error"]
    assert move_label_result["success"] is False
    assert "wire endpoint, not mid-segment" in move_label_result["error"]


@pytest.mark.asyncio
async def test_connectivity_move_rolls_back_when_post_write_validation_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    schematic_path = _copy_fixture(tmp_path)
    original_text = schematic_path.read_text(encoding="utf-8")
    server = create_server()
    tools = await server.get_tools()

    monkeypatch.setattr(
        "kicad_mcp.tools.schematic_edit_tools.validate_schematic_with_cli_export",
        lambda path: {"success": True, "skipped": True, "reason": "test"},
    )
    monkeypatch.setattr(
        "kicad_mcp.utils.transactional_edit.validate_schematic_with_cli_export",
        lambda path: {"success": True, "skipped": True, "reason": "test"},
    )
    monkeypatch.setattr(
        "kicad_mcp.tools.schematic_edit_tools._build_connectivity_validator",
        lambda *args, **kwargs: (lambda path: {"success": False, "reason": "forced connectivity failure"}),
    )

    result = await tools["schematic_move_symbol_with_connections"].fn(
        str(schematic_path), "R1", 110.0, 100.0, None, True, None
    )

    assert result["success"] is False
    assert result["rolled_back"] is True
    assert "forced connectivity failure" in result["error"]
    assert schematic_path.read_text(encoding="utf-8") == original_text
