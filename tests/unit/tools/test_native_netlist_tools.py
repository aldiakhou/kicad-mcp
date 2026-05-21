from pathlib import Path

import pytest

from kicad_mcp.server import create_server


@pytest.mark.asyncio
async def test_extract_netlist_prefers_native_connectivity(monkeypatch, tmp_path: Path):
    schematic = tmp_path / "demo.kicad_sch"
    schematic.write_text("(kicad_sch)", encoding="utf-8")

    monkeypatch.setattr(
        "kicad_mcp.tools.netlist_tools.export_native_netlist",
        lambda path: {
            "success": True,
            "components": {"R1": {"reference": "R1"}},
            "nets": {
                "NET1": {
                    "name": "NET1",
                    "nodes": [{"ref": "R1", "pin": "1", "pinfunction": "~_1", "pintype": "passive"}],
                }
            },
            "component_count": 1,
            "net_count": 1,
            "connectivity_complete": True,
            "netlist_quality": "native",
        },
    )
    server = create_server()
    tools = await server.get_tools()
    result = await tools["extract_schematic_netlist"].fn(str(schematic), None)
    assert result["success"] is True
    assert result["connectivity_complete"] is True
    assert result["netlist_quality"] == "native"
    assert result["nets"]["NET1"]["nodes"][0]["ref"] == "R1"


@pytest.mark.asyncio
async def test_extract_netlist_falls_back_when_native_unavailable(monkeypatch, tmp_path: Path):
    schematic = tmp_path / "demo.kicad_sch"
    schematic.write_text("(kicad_sch)", encoding="utf-8")

    monkeypatch.setattr(
        "kicad_mcp.tools.netlist_tools.export_native_netlist",
        lambda path: {"success": False, "error": "no cli"},
    )
    monkeypatch.setattr(
        "kicad_mcp.tools.netlist_tools.extract_netlist",
        lambda path: {
            "components": {},
            "nets": {},
            "component_count": 0,
            "net_count": 0,
            "netlist_quality": "partial",
            "connectivity_complete": False,
        },
    )
    server = create_server()
    tools = await server.get_tools()
    result = await tools["extract_schematic_netlist"].fn(str(schematic), None)
    assert result["success"] is True
    assert result["connectivity_complete"] is False
    assert result["native_netlist_error"] == "no cli"
