from pathlib import Path
import shutil

import pytest

from kicad_mcp.server import create_server

FIXTURE_PATH = Path("/home/runner/work/kicad-mcp/kicad-mcp/tests/fixtures/sample_schematic.kicad_sch")


def _copy_fixture(tmp_path: Path) -> Path:
    schematic_path = tmp_path / "tool_demo.kicad_sch"
    shutil.copy2(FIXTURE_PATH, schematic_path)
    return schematic_path


@pytest.mark.asyncio
async def test_schematic_read_and_write_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
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

    symbols_result = tools["schematic_list_symbols"].fn(str(schematic_path))
    labels_result = tools["schematic_list_labels"].fn(str(schematic_path))
    overlaps_result = tools["schematic_find_overlaps"].fn(str(schematic_path))

    assert symbols_result["success"] is True
    assert len(symbols_result["symbols"]) == 2
    assert labels_result["success"] is True
    assert len(labels_result["labels"]) == 2
    assert overlaps_result["success"] is True
    assert overlaps_result["overlaps"]

    arrange_labels_result = await tools["schematic_auto_arrange_labels"].fn(str(schematic_path), None)
    assert arrange_labels_result["success"] is True
    assert arrange_labels_result["changed_objects"]["labels"]

    move_result = await tools["schematic_move_symbol"].fn(str(schematic_path), "R1", 130.0, 140.0, None, None)
    assert move_result["success"] is True
    assert move_result["changed_objects"]["symbol"]["position"]["x"] == 130.0

    property_result = await tools["schematic_move_symbol_property"].fn(
        str(schematic_path), "R1", "Reference", 130.0, 136.0, None, None
    )
    assert property_result["success"] is True
    assert property_result["changed_objects"]["property"]["position"]["y"] == 136.0

    set_property_result = await tools["schematic_set_property"].fn(
        str(schematic_path), "R1", "MPN", "ABC123", None
    )
    assert set_property_result["success"] is True
    assert set_property_result["changed_objects"]["property"]["text"] == "ABC123"


@pytest.mark.asyncio
async def test_export_schematic_svg_uses_secure_cli_runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    schematic_path = _copy_fixture(tmp_path)
    output_path = tmp_path / "preview.svg"
    server = create_server()
    tools = await server.get_tools()

    def fake_run(self, command_args, input_files=None, output_files=None, working_dir=None, timeout=None, capture_output=True):
        assert command_args[:3] == ["sch", "export", "svg"]
        assert output_files == [str(output_path)]
        output_path.write_text("<svg/>", encoding="utf-8")
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(
        "kicad_mcp.tools.schematic_edit_tools.validate_schematic_with_cli_export",
        lambda path: {"success": True, "skipped": False, "stdout": "", "stderr": ""},
    )
    monkeypatch.setattr(
        "kicad_mcp.tools.schematic_edit_tools.SecureSubprocessRunner.run_kicad_command",
        fake_run,
    )

    result = await tools["export_schematic_svg"].fn(str(schematic_path), str(output_path), None)

    assert result["success"] is True
    assert result["svg_path"] == str(output_path)
    assert result["preview"]._format == "svg"
