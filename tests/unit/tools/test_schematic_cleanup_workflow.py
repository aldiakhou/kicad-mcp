import asyncio
from pathlib import Path
import shutil

import pytest

from kicad_mcp.server import create_server

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "messy_card_reader_like_schematic.kicad_sch"
UNSAFE_FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "block_layout_schematic.kicad_sch"


def _copy_fixture(tmp_path: Path, source: Path = FIXTURE_PATH) -> Path:
    schematic_path = tmp_path / source.name
    shutil.copy2(source, schematic_path)
    return schematic_path


@pytest.fixture
def patch_cli_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kicad_mcp.tools.schematic_edit_tools.validate_schematic_with_cli_export",
        lambda path: {"success": True, "skipped": True, "reason": "test"},
    )
    monkeypatch.setattr(
        "kicad_mcp.utils.transactional_edit.validate_schematic_with_cli_export",
        lambda path: {"success": True, "skipped": True, "reason": "test"},
    )


def test_cleanup_workflow_preview_is_read_only(patch_cli_validation: None, tmp_path: Path):
    schematic_path = _copy_fixture(tmp_path)
    original_text = schematic_path.read_text(encoding="utf-8")
    server = create_server()
    tools = asyncio.run(server.get_tools())

    result = tools["schematic_preview_cleanup"].fn(str(schematic_path))

    assert result["success"] is True
    assert result["cleanup_plan"]["property_moves"]
    assert schematic_path.read_text(encoding="utf-8") == original_text


def test_cleanup_workflow_refuses_unsafe_moves(patch_cli_validation: None, tmp_path: Path):
    schematic_path = _copy_fixture(tmp_path, UNSAFE_FIXTURE_PATH)
    server = create_server()
    tools = asyncio.run(server.get_tools())

    result = tools["schematic_preview_cleanup"].fn(str(schematic_path))

    assert result["success"] is False
    assert any("straight 2-point wire" in refusal for refusal in result["cleanup_plan"]["refusals"])


@pytest.mark.asyncio
async def test_apply_cleanup_creates_backup_returns_diff_and_svg(
    monkeypatch: pytest.MonkeyPatch, patch_cli_validation: None, tmp_path: Path
):
    schematic_path = _copy_fixture(tmp_path)
    output_path = tmp_path / "cleanup.svg"
    server = create_server()
    tools = await server.get_tools()

    def fake_export(schematic_path: str, requested_output: str | None = None) -> dict[str, str | bool]:
        target = Path(requested_output or output_path)
        target.write_text("<svg/>", encoding="utf-8")
        return {"success": True, "schematic_path": schematic_path, "svg_path": str(target), "stdout": "", "stderr": ""}

    monkeypatch.setattr("kicad_mcp.utils.transactional_edit.export_schematic_svg_file", fake_export)

    result = await tools["schematic_apply_cleanup"].fn(
        str(schematic_path), "left_to_right", 35.0, 25.0, True, True, str(output_path), None
    )

    assert result["success"] is True
    assert result["backup_path"]
    assert result["diff"]
    assert result["svg_preview"] == str(output_path)
    assert result["changed_objects"]["blocks_moved"]
    assert result["changed_objects"]["properties_arranged"]
    assert result["validation"]["connectivity"] == "preserved"


@pytest.mark.asyncio
async def test_apply_cleanup_rolls_back_on_forced_validation_failure(
    monkeypatch: pytest.MonkeyPatch, patch_cli_validation: None, tmp_path: Path
):
    schematic_path = _copy_fixture(tmp_path)
    original_text = schematic_path.read_text(encoding="utf-8")
    server = create_server()
    tools = await server.get_tools()

    monkeypatch.setattr(
        "kicad_mcp.utils.transactional_edit.validate_block_connectivity_snapshots",
        lambda path, snapshots: {
            "success": False,
            "reason": "forced cleanup validation failure",
            "block_connectivity": "changed",
        },
    )
    monkeypatch.setattr(
        "kicad_mcp.utils.transactional_edit.export_schematic_svg_file",
        lambda schematic_path, output_path=None: {
            "success": True,
            "schematic_path": schematic_path,
            "svg_path": str(tmp_path / "unused.svg"),
            "stdout": "",
            "stderr": "",
        },
    )

    result = await tools["schematic_apply_cleanup"].fn(str(schematic_path), "left_to_right", 35.0, 25.0, True, True, None, None)

    assert result["success"] is False
    assert result["rolled_back"] is True
    assert "forced cleanup validation failure" in result["error"]
    assert schematic_path.read_text(encoding="utf-8") == original_text
