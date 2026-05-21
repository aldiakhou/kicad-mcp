from pathlib import Path

import pytest

from kicad_mcp.server import create_server


def _write_fixture_libraries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    symbol_dir = tmp_path / "symbols"
    footprint_dir = tmp_path / "footprints"
    symbol_dir.mkdir()
    footprint_library = footprint_dir / "Resistor_SMD.pretty"
    footprint_library.mkdir(parents=True)

    (symbol_dir / "Device.kicad_sym").write_text(
        """
(kicad_symbol_lib
  (version 20240108)
  (generator "pytest")
  (symbol "R"
    (in_bom yes)
    (on_board yes)
    (property "Reference" "R" (at 0 0 0))
    (property "Value" "R" (at 0 2.54 0))
    (property "Footprint" "" (at 0 5.08 0))
  )
)
""",
        encoding="utf-8",
    )
    (footprint_library / "R_0603_1608Metric.kicad_mod").write_text(
        """
(footprint "R_0603_1608Metric"
  (version 20240108)
  (generator "pytest")
  (layer "F.Cu")
  (property "Reference" "REF**" (at 0 -1 0) (layer "F.SilkS"))
  (property "Value" "R_0603_1608Metric" (at 0 1 0) (layer "F.Fab"))
  (pad "1" smd rect (at -0.8 0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask"))
  (pad "2" smd rect (at 0.8 0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask"))
)
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("KICAD_SYMBOL_DIR", str(symbol_dir))
    monkeypatch.setenv("KICAD_FOOTPRINT_DIR", str(footprint_dir))


def _skip_cli_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kicad_mcp.tools.creation_tools.validate_schematic_with_cli_export",
        lambda path: {"success": True, "skipped": True, "reason": "test"},
    )
    monkeypatch.setattr(
        "kicad_mcp.utils.transactional_edit.validate_schematic_with_cli_export",
        lambda path: {"success": True, "skipped": True, "reason": "test"},
    )


@pytest.mark.asyncio
async def test_creation_tools_register_and_create_project_author_schematic_and_pcb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_fixture_libraries(tmp_path, monkeypatch)
    _skip_cli_validation(monkeypatch)
    server = create_server()
    tools = await server.get_tools()

    for name in [
        "create_kicad_project",
        "create_schematic_file",
        "create_pcb_file",
        "schematic_add_symbol",
        "schematic_add_wire",
        "schematic_add_label",
        "schematic_connect_points",
        "schematic_delete_item",
        "pcb_add_footprint",
        "pcb_move_footprint",
        "pcb_create_board_outline",
        "pcb_add_track",
        "pcb_add_via",
        "pcb_generate_basic_layout",
        "list_symbol_libraries",
        "list_footprint_libraries",
        "resolve_symbol",
        "resolve_footprint",
    ]:
        assert name in tools

    project = tools["create_kicad_project"].fn(str(tmp_path), "demo", True, True, "A4")
    assert project["success"] is True
    assert Path(project["created_files"]["project"]).exists()
    assert Path(project["created_files"]["schematic"]).exists()
    assert Path(project["created_files"]["pcb"]).exists()

    duplicate = tools["create_schematic_file"].fn(project["project_path"], False, "A4")
    assert duplicate["success"] is False
    assert "already exists" in duplicate["error"]

    schematic_path = project["created_files"]["schematic"]
    pcb_path = project["created_files"]["pcb"]

    symbol = await tools["schematic_add_symbol"].fn(
        schematic_path,
        "Device:R",
        "R1",
        "10k",
        30.0,
        30.0,
        0.0,
        "Resistor_SMD:R_0603_1608Metric",
        {"MPN": "ABC123"},
        None,
    )
    assert symbol["success"] is True
    assert symbol["changed_objects"]["symbol"]["reference"] == "R1"

    duplicate_symbol = await tools["schematic_add_symbol"].fn(
        schematic_path,
        "Device:R",
        "R1",
        "10k",
        35.0,
        35.0,
        0.0,
        None,
        None,
        None,
    )
    assert duplicate_symbol["success"] is False
    assert duplicate_symbol["rolled_back"] is True

    wire = await tools["schematic_add_wire"].fn(
        schematic_path,
        [{"x": 30.0, "y": 30.0}, {"x": 45.0, "y": 30.0}],
        "NET1",
        None,
    )
    assert wire["success"] is True
    label = await tools["schematic_add_label"].fn(
        schematic_path, "NET2", 50.0, 30.0, "global", 0.0, None
    )
    assert label["success"] is True
    connection = await tools["schematic_connect_points"].fn(
        schematic_path,
        {"x": 45.0, "y": 30.0},
        {"x": 50.0, "y": 35.0},
        "orthogonal",
        None,
        None,
    )
    assert connection["success"] is True
    assert len(connection["changed_objects"]["connection"]["segments"]) == 2
    assert all(
        len(segment["points"]) == 2
        for segment in connection["changed_objects"]["connection"]["segments"]
    )
    deleted = await tools["schematic_delete_item"].fn(
        schematic_path,
        "label",
        label["changed_objects"]["label"]["uuid"],
        None,
    )
    assert deleted["success"] is True

    footprint = await tools["pcb_add_footprint"].fn(
        pcb_path,
        "Resistor_SMD:R_0603_1608Metric",
        "R1",
        "10k",
        20.0,
        20.0,
        0.0,
        {"1": "NET1", "2": "NET2"},
        None,
    )
    assert footprint["success"] is True
    moved = await tools["pcb_move_footprint"].fn(pcb_path, "R1", 25.0, 25.0, None, None)
    assert moved["success"] is True
    outline = await tools["pcb_create_board_outline"].fn(pcb_path, 60.0, 40.0, 0.0, 0.0, None)
    assert outline["success"] is True
    track = await tools["pcb_add_track"].fn(
        pcb_path,
        "NET1",
        [{"x": 25.0, "y": 25.0}, {"x": 35.0, "y": 25.0}],
        "F.Cu",
        0.25,
        None,
    )
    assert track["success"] is True
    via = await tools["pcb_add_via"].fn(pcb_path, "NET1", 35.0, 25.0, 0.3, 0.6, None)
    assert via["success"] is True


@pytest.mark.asyncio
async def test_library_resolution_reports_missing_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_fixture_libraries(tmp_path, monkeypatch)
    server = create_server()
    tools = await server.get_tools()

    # Access through the synchronous functions already registered on FastMCP.
    resolved_symbol = tools["resolve_symbol"].fn("Device:R")
    resolved_footprint = tools["resolve_footprint"].fn("Resistor_SMD:R_0603_1608Metric")
    missing_symbol = tools["resolve_symbol"].fn("Device:Missing")
    missing_footprint = tools["resolve_footprint"].fn("Resistor_SMD:Missing")

    assert resolved_symbol["success"] is True
    assert resolved_footprint["success"] is True
    assert missing_symbol["success"] is False
    assert missing_footprint["success"] is False
