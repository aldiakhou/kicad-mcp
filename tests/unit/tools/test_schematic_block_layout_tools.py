from pathlib import Path
import shutil

import pytest

from kicad_mcp.server import create_server

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "block_layout_schematic.kicad_sch"


def _copy_fixture(tmp_path: Path) -> Path:
    schematic_path = tmp_path / "block_layout_demo.kicad_sch"
    shutil.copy2(FIXTURE_PATH, schematic_path)
    return schematic_path


def _get_block_by_symbols(blocks: list[dict], *symbols: str) -> dict:
    wanted = sorted(symbols)
    return next(block for block in blocks if block["symbols"] == wanted)


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


@pytest.mark.asyncio
async def test_block_tools_preview_and_move(patch_cli_validation: None, tmp_path: Path):
    schematic_path = _copy_fixture(tmp_path)
    original_text = schematic_path.read_text(encoding="utf-8")
    server = create_server()
    tools = await server.get_tools()

    blocks_result = tools["schematic_find_functional_blocks"].fn(str(schematic_path))
    usb_block = _get_block_by_symbols(blocks_result["blocks"], "J1", "R1")

    preview_result = tools["schematic_preview_block_move"].fn(str(schematic_path), usb_block["block_id"], 25.0, 0.0)

    assert blocks_result["success"] is True
    assert usb_block["name_hint"] == "USB-C / Connector block"
    assert preview_result["success"] is True
    assert preview_result["planned_changes"]["moved_wire_endpoints"] == ["wire-usb-boundary:0"]
    assert schematic_path.read_text(encoding="utf-8") == original_text

    move_result = await tools["schematic_move_block"].fn(
        str(schematic_path), usb_block["block_id"], 25.0, 0.0, True, None
    )

    assert move_result["success"] is True
    assert move_result["changed_objects"]["symbols"] == ["J1", "R1"]
    assert move_result["validation"]["post_write"]["block_connectivity"] == "preserved"


@pytest.mark.asyncio
async def test_block_tools_refuse_unsafe_moves_and_roll_back(
    monkeypatch: pytest.MonkeyPatch, patch_cli_validation: None, tmp_path: Path
):
    schematic_path = _copy_fixture(tmp_path)
    original_text = schematic_path.read_text(encoding="utf-8")
    server = create_server()
    tools = await server.get_tools()

    blocks_result = tools["schematic_find_functional_blocks"].fn(str(schematic_path))
    mcu_block = _get_block_by_symbols(blocks_result["blocks"], "U1")
    usb_block = _get_block_by_symbols(blocks_result["blocks"], "J1", "R1")

    refused_result = await tools["schematic_move_block"].fn(
        str(schematic_path), mcu_block["block_id"], 10.0, 0.0, True, None
    )

    assert refused_result["success"] is False
    assert "straight 2-point wire" in refused_result["error"]

    monkeypatch.setattr(
        "kicad_mcp.tools.schematic_edit_tools._build_block_validator",
        lambda *args, **kwargs: (lambda path: {"success": False, "reason": "forced block validation failure"}),
    )

    rollback_result = await tools["schematic_move_block"].fn(
        str(schematic_path), usb_block["block_id"], 25.0, 0.0, True, None
    )

    assert rollback_result["success"] is False
    assert rollback_result["rolled_back"] is True
    assert "forced block validation failure" in rollback_result["error"]
    assert schematic_path.read_text(encoding="utf-8") == original_text
