from pathlib import Path

import pytest

from kicad_mcp.server import create_server
from kicad_mcp.utils import library_resolver
from kicad_mcp.utils.kicad_s_expr import KiCadSchematic


def _write_fixture_libraries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    symbol_dir = tmp_path / "symbols"
    footprint_dir = tmp_path / "footprints"
    symbol_dir.mkdir()
    footprint_library = footprint_dir / "Resistor_SMD.pretty"
    capacitor_footprint_library = footprint_dir / "Capacitor_SMD.pretty"
    package_footprint_library = footprint_dir / "Package_QFP.pretty"
    footprint_library.mkdir(parents=True)
    capacitor_footprint_library.mkdir(parents=True)
    package_footprint_library.mkdir(parents=True)

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
    (property "Footprint" "Resistor_SMD:R_0603_1608Metric" (at 0 5.08 0))
    (property "ki_description" "Generic resistor" (at 0 7.62 0))
    (property "ki_keywords" "resistor resistance" (at 0 10.16 0))
    (property "ki_fp_filters" "R_*" (at 0 12.7 0))
    (pin passive line (at -2.54 0 180) (length 2.54)
      (name "~" (effects (font (size 1.27 1.27))))
      (number "1" (effects (font (size 1.27 1.27))))
    )
    (pin passive line (at 2.54 0 0) (length 2.54)
      (name "~" (effects (font (size 1.27 1.27))))
      (number "2" (effects (font (size 1.27 1.27))))
    )
  )
  (symbol "C"
    (in_bom yes)
    (on_board yes)
    (property "Reference" "C" (at 0 0 0))
    (property "Value" "C" (at 0 2.54 0))
    (property "Footprint" "" (at 0 5.08 0))
    (property "ki_description" "Generic capacitor" (at 0 7.62 0))
    (property "ki_keywords" "capacitor capacitance" (at 0 10.16 0))
    (property "ki_fp_filters" "C_*" (at 0 12.7 0))
    (pin passive line (at -2.54 0 180) (length 2.54)
      (name "~" (effects (font (size 1.27 1.27))))
      (number "1" (effects (font (size 1.27 1.27))))
    )
    (pin passive line (at 2.54 0 0) (length 2.54)
      (name "~" (effects (font (size 1.27 1.27))))
      (number "2" (effects (font (size 1.27 1.27))))
    )
  )
)
""",
        encoding="utf-8",
    )
    (symbol_dir / "MCU_Test.kicad_sym").write_text(
        """
(kicad_symbol_lib
  (version 20240108)
  (generator "pytest")
  (symbol "MCU48"
    (in_bom yes)
    (on_board yes)
    (property "Reference" "U" (at 0 0 0))
    (property "Value" "MCU48" (at 0 2.54 0))
    (property "Footprint" "Package_QFP:LQFP-48_7x7mm_P0.5mm" (at 0 5.08 0))
    (property "ki_description" "USB capable microcontroller" (at 0 7.62 0))
    (property "ki_keywords" "MCU USB Micro B SPI I2C" (at 0 10.16 0))
    (pin power_in line (at -2.54 0 180) (length 2.54)
      (name "VDD" (effects (font (size 1.27 1.27))))
      (number "1" (effects (font (size 1.27 1.27))))
    )
    (pin power_in line (at 2.54 0 0) (length 2.54)
      (name "GND" (effects (font (size 1.27 1.27))))
      (number "2" (effects (font (size 1.27 1.27))))
    )
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
    (capacitor_footprint_library / "C_0603_1608Metric.kicad_mod").write_text(
        """
(footprint "C_0603_1608Metric"
  (version 20240108)
  (generator "pytest")
  (layer "F.Cu")
  (property "Reference" "REF**" (at 0 -1 0) (layer "F.SilkS"))
  (property "Value" "C_0603_1608Metric" (at 0 1 0) (layer "F.Fab"))
  (pad "1" smd rect (at -0.8 0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask"))
  (pad "2" smd rect (at 0.8 0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask"))
)
""",
        encoding="utf-8",
    )
    (package_footprint_library / "LQFP-48_7x7mm_P0.5mm.kicad_mod").write_text(
        """
(footprint "LQFP-48_7x7mm_P0.5mm"
  (version 20240108)
  (generator "pytest")
  (layer "F.Cu")
  (property "Reference" "REF**" (at 0 -1 0) (layer "F.SilkS"))
  (property "Value" "LQFP-48_7x7mm_P0.5mm" (at 0 1 0) (layer "F.Fab"))
  (pad "1" smd rect (at -0.8 0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask"))
)
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("KICAD_SYMBOL_DIR", str(symbol_dir))
    monkeypatch.setenv("KICAD_FOOTPRINT_DIR", str(footprint_dir))
    library_resolver.find_symbols.cache_clear()
    library_resolver.find_footprints.cache_clear()


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
        "schematic_get_pin_map",
        "schematic_attach_net_to_pin",
        "schematic_apply_connection_plan",
        "schematic_add_no_connect",
        "schematic_assign_footprints",
        "schematic_assign_default_footprints",
        "schematic_footprint_report",
        "schematic_design_intent_schema",
        "schematic_explain_erc",
        "schematic_plan_erc_fixes",
        "schematic_apply_functional_layout",
        "project_completion_report",
        "project_next_actions",
        "schematic_apply_safe_erc_fixes",
        "schematic_delete_item",
        "pcb_add_footprint",
        "pcb_move_footprint",
        "pcb_create_board_outline",
        "pcb_add_track",
        "pcb_add_via",
        "pcb_generate_basic_layout",
        "pcb_sync_from_schematic",
        "pcb_complete_from_schematic",
        "pcb_apply_functional_placement",
        "pcb_get_ratsnest",
        "pcb_quality_report",
        "pcb_route_net_manhattan",
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
    pin_map = tools["schematic_get_pin_map"].fn(schematic_path, "R1")
    assert pin_map["success"] is True
    assert {pin["number"] for pin in pin_map["pins"]} == {"1", "2"}

    symbol_matches = tools["find_symbols"].fn("resistor", 10, None, 1, "Device")
    assert symbol_matches["success"] is True
    assert symbol_matches["count"] == 1
    assert symbol_matches["matches"][0]["lib_id"] == "Device:R"

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
async def test_attach_net_to_pin_and_sync_from_native_netlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_fixture_libraries(tmp_path, monkeypatch)
    _skip_cli_validation(monkeypatch)
    server = create_server()
    tools = await server.get_tools()
    project = tools["create_kicad_project"].fn(str(tmp_path), "sync_demo", True, True, "A4")
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
        None,
        None,
    )
    assert symbol["success"] is True

    def fake_native_netlist(path: str):
        return {
            "success": True,
            "components": {
                "R1": {
                    "reference": "R1",
                    "value": "10k",
                    "footprint": "Resistor_SMD:R_0603_1608Metric",
                }
            },
            "nets": {
                "NET1": {
                    "name": "NET1",
                    "nodes": [{"ref": "R1", "pin": "1", "pinfunction": "~_1", "pintype": "passive"}],
                },
                "NET2": {
                    "name": "NET2",
                    "nodes": [{"ref": "R1", "pin": "2", "pinfunction": "~_2", "pintype": "passive"}],
                },
            },
            "component_count": 1,
            "net_count": 2,
            "connectivity_complete": True,
            "netlist_quality": "native",
        }

    monkeypatch.setattr("kicad_mcp.utils.schematic_pins.export_native_netlist", fake_native_netlist)
    attach = await tools["schematic_attach_net_to_pin"].fn(
        schematic_path,
        "R1",
        "1",
        "NET1",
        "global",
        5.08,
        False,
        None,
    )
    assert attach["success"] is True
    assert attach["validation"]["post_write"]["success"] is True

    monkeypatch.setattr("kicad_mcp.tools.creation_tools.export_native_netlist", fake_native_netlist)
    sync = await tools["pcb_sync_from_schematic"].fn(
        project["project_path"],
        60.0,
        40.0,
        "functional",
        True,
        None,
    )
    assert sync["success"] is True
    pcb_text = Path(pcb_path).read_text(encoding="utf-8")
    assert '(net 1 "NET1")' in pcb_text
    assert '(net 2 "NET2")' in pcb_text
    assert '(net 1 "NET1")' in pcb_text
    assert '(net 2 "NET2")' in pcb_text

    quality = tools["pcb_quality_report"].fn(project["project_path"])
    assert quality["success"] is True
    assert quality["footprint_count"] == 1
    assert quality["net_count"] == 2
    assert quality["assigned_pad_count"] == 2
    assert quality["routing_complete"] is False

    ratsnest = tools["pcb_get_ratsnest"].fn(project["project_path"])
    assert ratsnest["success"] is True
    assert ratsnest["connection_count"] == 0

    route = await tools["pcb_route_net_manhattan"].fn(
        pcb_path,
        "NET1",
        [{"x": 10.0, "y": 10.0}, {"x": 20.0, "y": 15.0}],
        "F.Cu",
        0.25,
        None,
    )
    assert route["success"] is True
    routed_text = Path(pcb_path).read_text(encoding="utf-8")
    assert "(segment" in routed_text


@pytest.mark.asyncio
async def test_apply_connection_plan_batches_connections_no_connects_and_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_fixture_libraries(tmp_path, monkeypatch)
    _skip_cli_validation(monkeypatch)
    server = create_server()
    tools = await server.get_tools()
    project = tools["create_kicad_project"].fn(str(tmp_path), "plan_demo", True, True, "A4")
    schematic_path = project["created_files"]["schematic"]

    for ref, x in [("R1", 25.4), ("R2", 38.1)]:
        symbol = await tools["schematic_add_symbol"].fn(
            schematic_path,
            "Device:R",
            ref,
            "10k",
            x,
            25.4,
            0.0,
            "Resistor_SMD:R_0603_1608Metric",
            None,
            None,
        )
        assert symbol["success"] is True

    native_calls = {"count": 0}

    def fake_native_netlist(_path: str):
        native_calls["count"] += 1
        return {
            "success": True,
            "components": {},
            "nets": {
                "NET1": {"nodes": [{"ref": "R1", "pin": "1", "pinfunction": "~_1"}]},
                "NET2": {"nodes": [{"ref": "R1", "pin": "2", "pinfunction": "~_2"}]},
            },
            "component_count": 2,
            "net_count": 2,
            "connectivity_complete": True,
        }

    monkeypatch.setattr("kicad_mcp.utils.schematic_builder.export_native_netlist", fake_native_netlist)
    result = await tools["schematic_apply_connection_plan"].fn(
        schematic_path,
        [
            {"ref": "R1", "pin": "1", "net": "NET1"},
            {"ref": "R1", "pin": "2", "net": "NET2"},
        ],
        [{"ref": "R2", "pin": "1"}],
        True,
        True,
        None,
    )

    assert result["success"] is True
    assert result["changed_objects"]["plan_summary"]["required_connection_count"] == 2
    assert result["changed_objects"]["plan_summary"]["no_connect_count"] == 1
    assert result["validation"]["post_write"]["success"] is True
    assert native_calls["count"] == 1
    assert "NET1" in Path(schematic_path).read_text(encoding="utf-8")
    assert "(no_connect" in Path(schematic_path).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_apply_connection_plan_rolls_back_failed_required_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_fixture_libraries(tmp_path, monkeypatch)
    _skip_cli_validation(monkeypatch)
    server = create_server()
    tools = await server.get_tools()
    project = tools["create_kicad_project"].fn(str(tmp_path), "rollback_demo", True, True, "A4")
    schematic_path = project["created_files"]["schematic"]
    symbol = await tools["schematic_add_symbol"].fn(
        schematic_path,
        "Device:R",
        "R1",
        "10k",
        25.4,
        25.4,
        0.0,
        "Resistor_SMD:R_0603_1608Metric",
        None,
        None,
    )
    assert symbol["success"] is True

    monkeypatch.setattr(
        "kicad_mcp.utils.schematic_builder.export_native_netlist",
        lambda _path: {
            "success": True,
            "components": {},
            "nets": {"OTHER": {"nodes": [{"ref": "R1", "pin": "1", "pinfunction": "~_1"}]}},
            "component_count": 1,
            "net_count": 1,
            "connectivity_complete": True,
        },
    )
    result = await tools["schematic_apply_connection_plan"].fn(
        schematic_path,
        [{"ref": "R1", "pin": "1", "net": "NET_BAD"}],
        None,
        True,
        True,
        None,
    )

    assert result["success"] is False
    assert result["rolled_back"] is True
    assert "NET_BAD" not in Path(schematic_path).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_apply_connection_plan_replace_existing_rewires_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_fixture_libraries(tmp_path, monkeypatch)
    _skip_cli_validation(monkeypatch)
    server = create_server()
    tools = await server.get_tools()
    project = tools["create_kicad_project"].fn(str(tmp_path), "replace_demo", True, True, "A4")
    schematic_path = project["created_files"]["schematic"]
    symbol = await tools["schematic_add_symbol"].fn(
        schematic_path,
        "Device:R",
        "R1",
        "10k",
        25.4,
        25.4,
        0.0,
        "Resistor_SMD:R_0603_1608Metric",
        None,
        None,
    )
    assert symbol["success"] is True
    first = await tools["schematic_apply_connection_plan"].fn(
        schematic_path,
        [{"ref": "R1", "pin": "1", "net": "OLD_NET"}],
        None,
        False,
        True,
        False,
        False,
        None,
    )
    assert first["success"] is True

    blocked = await tools["schematic_apply_connection_plan"].fn(
        schematic_path,
        [{"ref": "R1", "pin": "1", "net": "NEW_NET"}],
        None,
        False,
        True,
        False,
        False,
        None,
    )
    assert blocked["success"] is False
    assert "replace_existing=True" in blocked["error"]

    monkeypatch.setattr(
        "kicad_mcp.utils.schematic_builder.export_native_netlist",
        lambda _path: {
            "success": True,
            "components": {},
            "nets": {"NEW_NET": {"nodes": [{"ref": "R1", "pin": "1", "pinfunction": "~_1"}]}},
            "component_count": 1,
            "net_count": 1,
            "connectivity_complete": True,
        },
    )
    replaced = await tools["schematic_apply_connection_plan"].fn(
        schematic_path,
        [{"ref": "R1", "pin": "1", "net": "NEW_NET"}],
        None,
        True,
        True,
        False,
        True,
        None,
    )

    assert replaced["success"] is True
    assert replaced["removed_conflicting_connections"] == [
        {"ref": "R1", "pin": "1", "old_net": "OLD_NET", "new_net": "NEW_NET"}
    ]
    schematic_text = Path(schematic_path).read_text(encoding="utf-8")
    assert "NEW_NET" in schematic_text
    assert "OLD_NET" not in schematic_text


@pytest.mark.asyncio
async def test_apply_connection_plan_skips_same_net_reapplication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_fixture_libraries(tmp_path, monkeypatch)
    _skip_cli_validation(monkeypatch)
    monkeypatch.setattr(
        "kicad_mcp.utils.schematic_intent.run_erc_via_cli",
        lambda _path: {"success": True, "total_violations": 0, "violation_categories": {}},
    )
    server = create_server()
    tools = await server.get_tools()
    project = tools["create_kicad_project"].fn(str(tmp_path), "idempotent_demo", True, True, "A4")
    schematic_path = project["created_files"]["schematic"]
    symbol = await tools["schematic_add_symbol"].fn(
        schematic_path,
        "Device:R",
        "R1",
        "10k",
        25.4,
        25.4,
        0.0,
        "Resistor_SMD:R_0603_1608Metric",
        None,
        None,
    )
    assert symbol["success"] is True

    first = await tools["schematic_apply_connection_plan"].fn(
        schematic_path,
        [{"ref": "R1", "pin": "1", "net": "NET1"}],
        None,
        False,
        True,
        False,
        False,
        None,
    )
    second = await tools["schematic_apply_connection_plan"].fn(
        schematic_path,
        [{"ref": "R1", "pin": "1", "net": "NET1"}],
        None,
        False,
        True,
        False,
        False,
        None,
    )

    labels = [
        label
        for label in KiCadSchematic.from_file(schematic_path).list_labels()
        if label["text"] == "NET1"
    ]
    assert first["success"] is True
    assert first["planned_connections"] == first["applied_connections"]
    assert first["applied_connection_count"] == 1
    assert first["skipped_existing_connection_count"] == 0
    assert first["skipped_existing_connections"] == []
    assert second["success"] is True
    assert len(second["planned_connections"]) == 1
    assert second["planned_connections"][0]["ref"] == "R1"
    assert second["planned_connections"][0]["pin"] == "1"
    assert second["planned_connections"][0]["net"] == "NET1"
    assert second["applied_connections"] == []
    assert second["applied_connection_count"] == 0
    assert second["skipped_existing_connection_count"] == 1
    assert second["skipped_existing_connections"] == [
        {
            "ref": "R1",
            "pin": "1",
            "net": "NET1",
            "reason": "already connected to requested net",
        }
    ]
    assert "_already_connected" not in second["planned_connections"][0]
    assert second["changed_objects"]["plan_summary"]["applied_connection_count"] == 0
    assert second["changed_objects"]["plan_summary"]["skipped_existing_connection_count"] == 1
    assert len(labels) == 1


@pytest.mark.asyncio
async def test_apply_connection_plan_can_strictly_roll_back_on_erc_violations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_fixture_libraries(tmp_path, monkeypatch)
    _skip_cli_validation(monkeypatch)
    server = create_server()
    tools = await server.get_tools()
    project = tools["create_kicad_project"].fn(str(tmp_path), "erc_strict_demo", True, True, "A4")
    schematic_path = project["created_files"]["schematic"]
    symbol = await tools["schematic_add_symbol"].fn(
        schematic_path,
        "Device:R",
        "R1",
        "10k",
        25.4,
        25.4,
        0.0,
        "Resistor_SMD:R_0603_1608Metric",
        None,
        None,
    )
    assert symbol["success"] is True

    monkeypatch.setattr(
        "kicad_mcp.utils.schematic_builder.export_native_netlist",
        lambda _path: {
            "success": True,
            "components": {},
            "nets": {"NET1": {"nodes": [{"ref": "R1", "pin": "1", "pinfunction": "~_1"}]}},
            "component_count": 1,
            "net_count": 1,
            "connectivity_complete": True,
        },
    )
    monkeypatch.setattr(
        "kicad_mcp.utils.schematic_intent.run_erc_via_cli",
        lambda _path: {
            "success": True,
            "total_violations": 1,
            "violation_categories": {"label_dangling": 1},
            "violations": [{"type": "label_dangling"}],
        },
    )

    non_strict = await tools["schematic_apply_connection_plan"].fn(
        schematic_path,
        [{"ref": "R1", "pin": "1", "net": "NET1"}],
        None,
        True,
        True,
        False,
        None,
    )
    assert non_strict["success"] is True
    assert non_strict["erc"]["total_violations"] == 1

    strict = await tools["schematic_apply_connection_plan"].fn(
        schematic_path,
        [{"ref": "R1", "pin": "1", "net": "NET1"}],
        None,
        True,
        True,
        True,
        None,
    )
    assert strict["success"] is False
    assert strict["rolled_back"] is True
    assert strict["recoverable"] is True


@pytest.mark.asyncio
async def test_schematic_quality_report_detects_generic_authoring_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_fixture_libraries(tmp_path, monkeypatch)
    _skip_cli_validation(monkeypatch)
    server = create_server()
    tools = await server.get_tools()
    project = tools["create_kicad_project"].fn(str(tmp_path), "quality_demo", True, True, "A4")
    schematic_path = project["created_files"]["schematic"]
    label = await tools["schematic_add_label"].fn(
        schematic_path, "FLOATING", 50.8, 50.8, "global", 0.0, None
    )
    assert label["success"] is True

    monkeypatch.setattr(
        "kicad_mcp.utils.schematic_builder.export_native_netlist",
        lambda _path: {
            "success": True,
            "components": {},
            "nets": {
                "+3.3V": {
                    "nodes": [
                        {
                            "ref": "U1",
                            "pin": "1",
                            "pinfunction": "GND_1",
                            "pintype": "power_in",
                        }
                    ]
                }
            },
            "component_count": 1,
            "net_count": 1,
            "connectivity_complete": True,
        },
    )
    report = tools["schematic_quality_report"].fn(schematic_path, False)

    assert report["success"] is True
    assert report["dangling_label_count"] == 1
    assert report["isolated_label_count"] == 1
    assert report["power_ground_mismatch_count"] == 1
    assert report["quality_gate"]["passed"] is False


@pytest.mark.asyncio
async def test_schematic_erc_explanation_accepts_library_mismatch_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_fixture_libraries(tmp_path, monkeypatch)
    _skip_cli_validation(monkeypatch)
    server = create_server()
    tools = await server.get_tools()
    project = tools["create_kicad_project"].fn(str(tmp_path), "erc_warn_demo", True, True, "A4")

    monkeypatch.setattr(
        "kicad_mcp.tools.creation_tools.run_erc_via_cli",
        lambda _path, timeout_seconds=None: {
            "success": True,
            "total_violations": 1,
            "violation_categories": {"lib_symbol_mismatch": 1},
            "severity_counts": {"warning": 1},
            "violations": [
                {
                    "type": "lib_symbol_mismatch",
                    "severity": "warning",
                    "description": "Symbol differs from library",
                    "items": [{"description": "Symbol U1 [AMS1117-3.3]"}],
                }
            ],
        },
    )

    explanation = tools["schematic_explain_erc"].fn(project["project_path"], True, None)
    plan = tools["schematic_plan_erc_fixes"].fn(project["project_path"], None)

    assert explanation["success"] is True
    assert explanation["blocking_count"] == 0
    assert explanation["accepted_warning_count"] == 1
    assert explanation["findings"][0]["classification"] == "accepted_warning"
    assert plan["success"] is True
    assert plan["blocked"] is False
    assert plan["accepted_warning_count"] == 1
    assert plan["manual_decision_count"] == 0


@pytest.mark.asyncio
async def test_schematic_erc_plan_blocks_dangling_label_and_unconnected_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_fixture_libraries(tmp_path, monkeypatch)
    _skip_cli_validation(monkeypatch)
    server = create_server()
    tools = await server.get_tools()
    project = tools["create_kicad_project"].fn(str(tmp_path), "erc_block_demo", True, True, "A4")

    monkeypatch.setattr(
        "kicad_mcp.tools.creation_tools.run_erc_via_cli",
        lambda _path, timeout_seconds=None: {
            "success": True,
            "total_violations": 2,
            "violation_categories": {"label_dangling": 1, "pin_not_connected": 1},
            "severity_counts": {"error": 2},
            "violations": [
                {
                    "type": "label_dangling",
                    "severity": "error",
                    "description": "Label not connected",
                    "items": [{"description": "Label Global 'SDA'"}],
                },
                {
                    "type": "pin_not_connected",
                    "severity": "error",
                    "description": "Pin not connected",
                    "items": [{"description": "Symbol U2 Pin IO13 [Input, Line]"}],
                },
            ],
        },
    )

    plan = tools["schematic_plan_erc_fixes"].fn(project["project_path"], None)

    assert plan["success"] is True
    assert plan["blocked"] is True
    assert plan["manual_decision_count"] == 2
    assert plan["manual_decisions"][0]["suggested_action"]["kind"] == "reattach_label_to_pin_or_wire"
    assert plan["manual_decisions"][1]["refs"] == ["U2"]
    assert plan["manual_decisions"][1]["suggested_action"]["kind"] == "connect_pin_or_add_no_connect"


@pytest.mark.asyncio
async def test_schematic_functional_layout_moves_symbols_and_pin_attached_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_fixture_libraries(tmp_path, monkeypatch)
    _skip_cli_validation(monkeypatch)
    server = create_server()
    tools = await server.get_tools()
    project = tools["create_kicad_project"].fn(str(tmp_path), "layout_demo", True, True, "A4")
    schematic_path = project["created_files"]["schematic"]

    symbol = await tools["schematic_add_symbol"].fn(
        schematic_path,
        "Device:R",
        "R1",
        "10k",
        25.4,
        25.4,
        0.0,
        "Resistor_SMD:R_0603_1608Metric",
        None,
        None,
    )
    assert symbol["success"] is True
    monkeypatch.setattr(
        "kicad_mcp.utils.schematic_builder.export_native_netlist",
        lambda _path: {
            "success": True,
            "components": {},
            "nets": {"NET1": {"nodes": [{"ref": "R1", "pin": "1", "pinfunction": "~_1"}]}},
            "component_count": 1,
            "net_count": 1,
            "connectivity_complete": True,
        },
    )
    plan = await tools["schematic_apply_connection_plan"].fn(
        schematic_path,
        [{"ref": "R1", "pin": "1", "net": "NET1"}],
        [{"ref": "R1", "pin": "2"}],
        True,
        True,
        None,
    )
    assert plan["success"] is True

    layout = await tools["schematic_apply_functional_layout"].fn(
        schematic_path,
        True,
        True,
        False,
        {"references": {"R1": {"x": 76.2, "y": 50.8, "angle": 0.0}}},
        None,
    )

    assert layout["success"] is True
    assert layout["changed_objects"]["moved_symbol_count"] == 1
    assert layout["changed_objects"]["moved_label_count"] == 1
    assert layout["changed_objects"]["moved_no_connect_count"] == 1
    schematic = KiCadSchematic.from_file(schematic_path)
    assert schematic.get_symbol("R1")["position"]["x"] == 76.2
    pin_points = {
        (pin["connection_point"]["x"], pin["connection_point"]["y"])
        for pin in tools["schematic_get_pin_map"].fn(schematic_path, "R1")["pins"]
    }
    label_positions = {
        (label["position"]["x"], label["position"]["y"]) for label in schematic.list_labels()
    }
    no_connect_positions = {
        (marker["position"]["x"], marker["position"]["y"])
        for marker in schematic.list_no_connects()
    }
    assert label_positions.intersection(pin_points)
    assert no_connect_positions.intersection(pin_points)


@pytest.mark.asyncio
async def test_project_completion_report_combines_generic_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_fixture_libraries(tmp_path, monkeypatch)
    _skip_cli_validation(monkeypatch)
    server = create_server()
    tools = await server.get_tools()
    project = tools["create_kicad_project"].fn(str(tmp_path), "report_demo", True, True, "A4")

    fake_native = {
        "success": True,
        "components": {
            "R1": {
                "reference": "R1",
                "value": "10k",
                "footprint": "Resistor_SMD:R_0603_1608Metric",
            }
        },
        "nets": {
            "NET1": {"nodes": [{"ref": "R1", "pin": "1", "pinfunction": "~_1"}]},
            "GND": {"nodes": [{"ref": "R1", "pin": "2", "pinfunction": "~_2"}]},
        },
        "component_count": 1,
        "net_count": 2,
        "connectivity_complete": True,
    }
    monkeypatch.setattr("kicad_mcp.utils.schematic_builder.export_native_netlist", lambda _path: fake_native)
    monkeypatch.setattr("kicad_mcp.tools.creation_tools.export_native_netlist", lambda _path: fake_native)
    report = await tools["project_completion_report"].fn(
        project["project_path"],
        False,
        False,
        None,
        None,
    )

    assert report["success"] is True
    assert report["status"]["drc_clean_or_skipped"] is True
    assert report["native_netlist"]["connectivity_complete"] is True
    assert report["drc"]["skipped"] is True


@pytest.mark.asyncio
async def test_project_next_actions_prioritizes_routing_for_synced_unrouted_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_fixture_libraries(tmp_path, monkeypatch)
    _skip_cli_validation(monkeypatch)
    server = create_server()
    tools = await server.get_tools()
    project = tools["create_kicad_project"].fn(str(tmp_path), "next_demo", True, True, "A4")
    pcb_path = project["created_files"]["pcb"]
    added = await tools["pcb_add_footprint"].fn(
        pcb_path,
        "Resistor_SMD:R_0603_1608Metric",
        "R1",
        "10k",
        10.0,
        10.0,
        0.0,
        {"1": "NET1", "2": "GND"},
        None,
    )
    assert added["success"] is True
    added = await tools["pcb_add_footprint"].fn(
        pcb_path,
        "Resistor_SMD:R_0603_1608Metric",
        "R2",
        "10k",
        20.0,
        10.0,
        0.0,
        {"1": "NET1", "2": "GND"},
        None,
    )
    assert added["success"] is True

    fake_native = {
        "success": True,
        "components": {
            "R1": {
                "reference": "R1",
                "value": "10k",
                "footprint": "Resistor_SMD:R_0603_1608Metric",
            },
            "R2": {
                "reference": "R2",
                "value": "10k",
                "footprint": "Resistor_SMD:R_0603_1608Metric",
            }
        },
        "nets": {
            "NET1": {
                "nodes": [
                    {"ref": "R1", "pin": "1", "pinfunction": "~_1"},
                    {"ref": "R2", "pin": "1", "pinfunction": "~_1"},
                ]
            },
            "GND": {
                "nodes": [
                    {"ref": "R1", "pin": "2", "pinfunction": "~_2"},
                    {"ref": "R2", "pin": "2", "pinfunction": "~_2"},
                ]
            },
        },
        "component_count": 2,
        "net_count": 2,
        "connectivity_complete": True,
    }
    monkeypatch.setattr("kicad_mcp.utils.schematic_builder.export_native_netlist", lambda _path: fake_native)
    monkeypatch.setattr("kicad_mcp.tools.creation_tools.export_native_netlist", lambda _path: fake_native)
    monkeypatch.setattr(
        "kicad_mcp.tools.creation_tools.run_erc_via_cli",
        lambda _path, timeout_seconds=None: {
            "success": True,
            "total_violations": 0,
            "violation_categories": {},
            "severity_counts": {},
            "violations": [],
        },
    )

    actions = await tools["project_next_actions"].fn(
        project["project_path"],
        True,
        False,
        None,
        None,
    )

    assert actions["success"] is True
    assert actions["top_action"]["id"] == "route_unrouted_nets"
    assert actions["top_action"]["details"]["ratsnest_connection_count"] == 2


@pytest.mark.asyncio
async def test_schematic_apply_safe_erc_fixes_deletes_explicit_dangling_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_fixture_libraries(tmp_path, monkeypatch)
    _skip_cli_validation(monkeypatch)
    server = create_server()
    tools = await server.get_tools()
    project = tools["create_kicad_project"].fn(str(tmp_path), "safe_fix_demo", True, True, "A4")
    schematic_path = project["created_files"]["schematic"]
    label = await tools["schematic_add_label"].fn(
        schematic_path, "FLOATING", 50.8, 50.8, "global", 0.0, None
    )
    label_uuid = label["changed_objects"]["label"]["uuid"]
    fix = {
        "kind": "delete_dangling_label",
        "label_uuid": label_uuid,
        "action": {"kind": "delete_dangling_label", "auto_safe": True},
    }
    monkeypatch.setattr(
        "kicad_mcp.tools.creation_tools.run_erc_via_cli",
        lambda _path, timeout_seconds=None: {
            "success": True,
            "total_violations": 0,
            "violation_categories": {},
            "severity_counts": {},
            "violations": [],
        },
    )

    dry_run = await tools["schematic_apply_safe_erc_fixes"].fn(
        schematic_path,
        [fix],
        True,
        None,
        None,
    )
    assert dry_run["success"] is True
    assert dry_run["dry_run"] is True
    assert dry_run["planned_fix_count"] == 1
    assert "FLOATING" in Path(schematic_path).read_text(encoding="utf-8")

    applied = await tools["schematic_apply_safe_erc_fixes"].fn(
        schematic_path,
        [fix],
        False,
        None,
        None,
    )
    assert applied["success"] is True
    assert "FLOATING" not in Path(schematic_path).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_functional_placement_preserves_non_overlapping_positions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_fixture_libraries(tmp_path, monkeypatch)
    _skip_cli_validation(monkeypatch)
    server = create_server()
    tools = await server.get_tools()
    project = tools["create_kicad_project"].fn(str(tmp_path), "place_demo", True, True, "A4")
    pcb_path = project["created_files"]["pcb"]
    for index, ref in enumerate(["R1", "R2"]):
        added = await tools["pcb_add_footprint"].fn(
            pcb_path,
            "Resistor_SMD:R_0603_1608Metric",
            ref,
            "10k",
            10.0 + index,
            10.0,
            0.0,
            {"1": f"NET{index}", "2": "GND"},
            None,
        )
        assert added["success"] is True

    placement = await tools["pcb_apply_functional_placement"].fn(
        project["project_path"], 80.0, 50.0, None
    )
    assert placement["success"] is True
    assert placement["changed_objects"]["overlap_warnings"] == []
    assert placement["changed_objects"]["placement_valid"] is True
    roles = {
        item["reference"]: item["role"]
        for item in placement["changed_objects"]["moved_footprints"]
    }
    assert roles == {"R1": "resistor", "R2": "resistor"}


@pytest.mark.asyncio
async def test_functional_placement_accepts_generic_reference_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_fixture_libraries(tmp_path, monkeypatch)
    _skip_cli_validation(monkeypatch)
    server = create_server()
    tools = await server.get_tools()
    project = tools["create_kicad_project"].fn(str(tmp_path), "rule_demo", True, True, "A4")
    pcb_path = project["created_files"]["pcb"]
    added = await tools["pcb_add_footprint"].fn(
        pcb_path,
        "Resistor_SMD:R_0603_1608Metric",
        "R10",
        "10k",
        10.0,
        10.0,
        0.0,
        {"1": "SIG", "2": "GND"},
        None,
    )
    assert added["success"] is True

    placement = await tools["pcb_apply_functional_placement"].fn(
        project["project_path"],
        80.0,
        50.0,
        {"references": {"R10": {"x": 30.0, "y": 20.0, "angle": 90.0}}},
        None,
    )
    assert placement["success"] is True
    moved = placement["changed_objects"]["moved_footprints"][0]
    assert moved["reference"] == "R10"
    assert moved["role"] == "resistor"
    assert moved["position"] == {"x": 30.0, "y": 20.0, "angle": 90.0}


@pytest.mark.asyncio
async def test_pcb_complete_from_schematic_syncs_places_and_reports_pending_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_fixture_libraries(tmp_path, monkeypatch)
    _skip_cli_validation(monkeypatch)
    server = create_server()
    tools = await server.get_tools()
    project = tools["create_kicad_project"].fn(str(tmp_path), "generic_pcb", True, False, "A3")
    assert project["success"] is True

    def fake_native_netlist(_schematic_path: str) -> dict:
        return {
            "success": True,
            "components": {
                "R1": {
                    "reference": "R1",
                    "value": "10k",
                    "footprint": "Resistor_SMD:R_0603_1608Metric",
                }
            },
            "nets": {
                "NET1": {
                    "name": "NET1",
                    "nodes": [{"ref": "R1", "pin": "1", "pinfunction": "~_1", "pintype": "passive"}],
                },
                "GND": {
                    "name": "GND",
                    "nodes": [{"ref": "R1", "pin": "2", "pinfunction": "~_2", "pintype": "passive"}],
                },
            },
            "component_count": 1,
            "net_count": 2,
            "connectivity_complete": True,
            "netlist_quality": "native",
        }

    monkeypatch.setattr("kicad_mcp.tools.creation_tools.export_native_netlist", fake_native_netlist)
    completed = tools["pcb_complete_from_schematic"].fn(
        project["project_path"],
        80.0,
        50.0,
        "functional",
        True,
        True,
        {"references": {"R1": {"x": 25.0, "y": 15.0, "angle": 0.0}}},
    )

    assert completed["success"] is True
    assert completed["status"]["pcb_synced"] is True
    assert completed["status"]["pcb_placed"] is True
    assert completed["status"]["routing_complete"] is False
    assert completed["quality"]["assigned_pad_count"] == 2
    assert completed["ratsnest"]["connection_count"] == 0
    moved = completed["placement"]["changed_objects"]["moved_footprints"][0]
    assert moved["reference"] == "R1"
    assert moved["role"] == "resistor"


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


@pytest.mark.asyncio
async def test_agent_search_tools_and_compact_v2_build_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_fixture_libraries(tmp_path, monkeypatch)
    _skip_cli_validation(monkeypatch)
    monkeypatch.setattr("kicad_mcp.utils.library_resolver._common_symbol_roots", lambda: [])
    monkeypatch.setattr("kicad_mcp.utils.library_resolver._common_footprint_roots", lambda: [])
    monkeypatch.setenv("KICAD_MCP_TOOL_PROFILE", "agent")
    monkeypatch.setattr(
        "kicad_mcp.utils.schematic_builder.validate_connection_plan_membership",
        lambda path, connections: {"success": True, "checked_count": len(connections)},
    )
    monkeypatch.setattr(
        "kicad_mcp.utils.schematic_builder.export_native_netlist",
        lambda path: {
            "success": True,
            "component_count": 1,
            "net_count": 0,
            "connectivity_complete": True,
            "nets": {},
        },
    )
    server = create_server()
    tools = await server.get_tools()

    assert "find_symbols" in tools
    assert "find_footprints" in tools
    assert "list_symbol_libraries" not in tools

    symbols = tools["find_symbols"].fn("Device", 5)
    footprints = tools["find_footprints"].fn("0603", 5)
    library_symbols = tools["find_symbols"].fn("USB Micro B", 5, "MCU_Test")
    library_footprints = tools["find_footprints"].fn("LQFP", 5, "Package_QFP")

    assert symbols["success"] is True
    assert any(match["lib_id"] == "Device:R" for match in symbols["matches"])
    resistor_match = next(match for match in symbols["matches"] if match["lib_id"] == "Device:R")
    assert resistor_match["default_footprint"] == "Resistor_SMD:R_0603_1608Metric"
    assert "R_*" in resistor_match["footprint_filters"]
    assert footprints["success"] is True
    assert footprints["matches"][0]["footprint_id"] == "Resistor_SMD:R_0603_1608Metric"
    assert library_symbols["matches"][0]["lib_id"] == "MCU_Test:MCU48"
    assert library_symbols["matches"][0]["default_footprint"] == "Package_QFP:LQFP-48_7x7mm_P0.5mm"
    assert library_footprints["matches"][0]["footprint_id"] == "Package_QFP:LQFP-48_7x7mm_P0.5mm"

    project = tools["create_kicad_project"].fn(str(tmp_path), "compact_demo", True, True, "A4")
    built = tools["schematic_build_from_spec_v2"].fn(
        project["project_path"],
        {
            "parts": [{"ref": "R10", "lib_id": "Device:R", "symbol": "R_1_1", "value": "10k"}],
            "nets": {},
        },
    )

    assert built["success"] is True
    assert built["mode"] == "update"
    assert built["symbol_count"] == 1
    assert built["native_netlist"] == {
        "success": True,
        "component_count": 1,
        "net_count": 0,
        "connectivity_complete": True,
        "error": None,
    }
    assert "diff" not in built
    assert "schematic_preview" not in built
    assert "quality_report" not in built


@pytest.mark.asyncio
async def test_schematic_footprint_tools_assign_explicit_and_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_fixture_libraries(tmp_path, monkeypatch)
    _skip_cli_validation(monkeypatch)
    monkeypatch.setattr("kicad_mcp.utils.library_resolver._common_symbol_roots", lambda: [])
    monkeypatch.setattr("kicad_mcp.utils.library_resolver._common_footprint_roots", lambda: [])
    server = create_server()
    tools = await server.get_tools()
    project = tools["create_kicad_project"].fn(str(tmp_path), "footprints", True, True, "A4")
    schematic_path = project["created_files"]["schematic"]

    resistor = await tools["schematic_add_symbol"].fn(
        schematic_path,
        "Device:R",
        "R1",
        "10k",
        30.0,
        30.0,
        0.0,
        None,
        None,
        None,
    )
    capacitor = await tools["schematic_add_symbol"].fn(
        schematic_path,
        "Device:C",
        "C1",
        "100n",
        45.0,
        30.0,
        0.0,
        None,
        None,
        None,
    )
    assert resistor["success"] is True
    assert capacitor["success"] is True

    report = tools["schematic_footprint_report"].fn(project["project_path"])
    assert report["success"] is True
    assert report["missing_footprints"] == ["R1", "C1"]
    assert {"ref": "R1", "footprint": "Resistor_SMD:R_0603_1608Metric", "source": "symbol_default"} in report["suggested_assignments"]
    assert {"ref": "C1", "footprint": "Capacitor_SMD:C_0603_1608Metric", "source": "footprint_filter"} in report["suggested_assignments"]

    assigned = tools["schematic_assign_footprints"].fn(
        project["project_path"],
        [{"ref": "R1", "footprint": "Resistor_SMD:R_0603_1608Metric"}],
        True,
    )
    assert assigned["success"] is True
    assert assigned["assigned_count"] == 1
    assert assigned["footprint_report"]["missing_footprint_count"] == 1

    dry_run = tools["schematic_assign_default_footprints"].fn(
        project["project_path"],
        ["C1", "Z9"],
        "symbol_default_then_filter",
        True,
    )
    assert dry_run["success"] is True
    assert dry_run["partial_success"] is True
    assert dry_run["missing_refs"] == ["Z9"]
    assert dry_run["planned_assignments"] == [
        {"ref": "C1", "footprint": "Capacitor_SMD:C_0603_1608Metric", "source": "footprint_filter"}
    ]
    assert "Capacitor_SMD:C_0603_1608Metric" not in Path(schematic_path).read_text(encoding="utf-8")

    defaulted = tools["schematic_assign_default_footprints"].fn(
        project["project_path"],
        ["C1", "Z9"],
        "symbol_default_then_filter",
        False,
    )
    assert defaulted["success"] is True
    assert defaulted["partial_success"] is True
    assert defaulted["missing_refs"] == ["Z9"]
    assert defaulted["assigned_count"] == 1
    final_report = tools["schematic_footprint_report"].fn(project["project_path"])
    assert final_report["missing_footprint_count"] == 0
    assert final_report["invalid_footprints"] == []

    all_missing = tools["schematic_assign_default_footprints"].fn(
        project["project_path"],
        ["Z8", "Z9"],
        "symbol_default_then_filter",
        False,
    )
    assert all_missing["success"] is False
    assert all_missing["partial_success"] is False
    assert all_missing["missing_refs"] == ["Z8", "Z9"]
    assert all_missing["error"] == "all requested refs were missing"


@pytest.mark.asyncio
async def test_v2_replace_requires_explicit_destructive_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_fixture_libraries(tmp_path, monkeypatch)
    _skip_cli_validation(monkeypatch)
    monkeypatch.setattr(
        "kicad_mcp.utils.schematic_builder.validate_connection_plan_membership",
        lambda path, connections: {"success": True, "checked_count": len(connections)},
    )
    monkeypatch.setattr(
        "kicad_mcp.utils.schematic_builder.export_native_netlist",
        lambda path: {"success": True, "component_count": 1, "net_count": 0},
    )
    server = create_server()
    tools = await server.get_tools()
    project = tools["create_kicad_project"].fn(str(tmp_path), "replace_guard", True, True, "A4")

    first = tools["schematic_build_from_spec_v2"].fn(
        project["project_path"],
        {"parts": [{"ref": "R1", "symbol": "Device:R", "value": "10k"}], "nets": {}},
        "update",
    )
    assert first["success"] is True

    guarded = tools["schematic_build_from_spec_v2"].fn(
        project["project_path"],
        {"parts": [{"ref": "R2", "symbol": "Device:R", "value": "20k"}], "nets": {}},
        "replace",
    )

    assert guarded["success"] is False
    assert "allow_destructive_replace=True" in guarded["error"]
