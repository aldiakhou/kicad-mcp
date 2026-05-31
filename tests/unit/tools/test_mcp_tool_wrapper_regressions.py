from pathlib import Path

from fastmcp import FastMCP
import pytest

from kicad_mcp.server import create_server
from kicad_mcp.tools.export_tools import register_export_tools
from kicad_mcp.tools.netlist_tools import register_netlist_tools
from kicad_mcp.tools.pattern_tools import register_pattern_tools


async def _tools_from(registrar):
    mcp = FastMCP("tool-wrapper-test")
    registrar(mcp)
    return {tool.name: tool for tool in await mcp.list_tools()}


@pytest.mark.asyncio
async def test_extract_project_netlist_uses_undecorated_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    project_path = tmp_path / "demo.kicad_pro"
    schematic_path = tmp_path / "demo.kicad_sch"
    project_path.write_text("{}", encoding="utf-8")
    schematic_path.write_text('(kicad_sch (version 20231120) (generator "pytest"))', encoding="utf-8")

    monkeypatch.setattr(
        "kicad_mcp.tools.netlist_tools.get_project_files",
        lambda path: {"schematic": str(schematic_path)},
    )
    monkeypatch.setattr(
        "kicad_mcp.tools.netlist_tools.extract_netlist",
        lambda path: {
            "component_count": 1,
            "net_count": 1,
            "components": {"R1": {"reference": "R1"}},
            "nets": {"NET1": []},
            "limitations": ["partial"],
            "netlist_quality": "partial",
        },
    )

    tools = await _tools_from(register_netlist_tools)

    result = await tools["extract_project_netlist"].fn(str(project_path), None)

    assert result["success"] is True
    assert result["project_path"] == str(project_path)
    assert result["component_count"] == 1


@pytest.mark.asyncio
async def test_analyze_project_circuit_patterns_uses_undecorated_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    project_path = tmp_path / "demo.kicad_pro"
    schematic_path = tmp_path / "demo.kicad_sch"
    project_path.write_text("{}", encoding="utf-8")
    schematic_path.write_text('(kicad_sch (version 20231120) (generator "pytest"))', encoding="utf-8")

    monkeypatch.setattr(
        "kicad_mcp.tools.pattern_tools.get_project_files",
        lambda path: {"schematic": str(schematic_path)},
    )
    monkeypatch.setattr(
        "kicad_mcp.tools.pattern_tools.extract_netlist",
        lambda path: {
            "component_count": 1,
            "net_count": 1,
            "components": {"U1": {"reference": "U1"}},
            "nets": {"NET1": []},
        },
    )
    for name in (
        "identify_power_supplies",
        "identify_amplifiers",
        "identify_filters",
        "identify_oscillators",
        "identify_digital_interfaces",
        "identify_microcontrollers",
        "identify_sensor_interfaces",
    ):
        monkeypatch.setattr(f"kicad_mcp.tools.pattern_tools.{name}", lambda *args: [])

    tools = await _tools_from(register_pattern_tools)

    result = await tools["analyze_project_circuit_patterns"].fn(str(project_path), None)

    assert result["success"] is True
    assert result["project_path"] == str(project_path)
    assert result["total_patterns_found"] == 0


@pytest.mark.asyncio
async def test_generate_project_thumbnail_uses_undecorated_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    project_path = tmp_path / "demo.kicad_pro"
    pcb_path = tmp_path / "demo.kicad_pcb"
    project_path.write_text("{}", encoding="utf-8")
    pcb_path.write_text("(kicad_pcb)", encoding="utf-8")

    async def fake_thumbnail(pcb_file: str, ctx):
        assert pcb_file == str(pcb_path)
        return {
            "success": True,
            "pcb_path": pcb_file,
            "thumbnail_path": str(tmp_path / "thumbnail.svg"),
            "mime_type": "image/svg+xml",
            "file_size": 6,
        }

    monkeypatch.setattr(
        "kicad_mcp.tools.export_tools.get_project_files",
        lambda path: {"pcb": str(pcb_path)},
    )
    monkeypatch.setattr(
        "kicad_mcp.tools.export_tools.generate_thumbnail_with_cli",
        fake_thumbnail,
    )

    tools = await _tools_from(register_export_tools)

    result = await tools["generate_project_thumbnail"].fn(str(project_path), None)

    assert result["success"] is True
    assert result["project_path"] == str(project_path)
    assert result["pcb_path"] == str(pcb_path)


@pytest.mark.asyncio
async def test_validation_tools_are_registered_and_parse_schematic_symbols(tmp_path: Path):
    project_path = tmp_path / "demo.kicad_pro"
    schematic_path = tmp_path / "demo.kicad_sch"
    project_path.write_text("{}", encoding="utf-8")
    schematic_path.write_text(
        """
(kicad_sch
  (version 20231120)
  (generator "pytest")
  (paper "A4")
  (symbol
    (uuid 11111111-1111-1111-1111-111111111111)
    (property "Value" "10k" (at 500 24 0))
    (at 500 20 0)
    (lib_id "Device:R")
    (property "Reference" "R1" (at 500 16 0))
  )
)
""",
        encoding="utf-8",
    )

    server = create_server()
    tools = await server.get_tools()

    result = await tools["validate_project_boundaries"].fn(str(project_path), None)

    assert result["success"] is False
    assert result["project_path"] == str(project_path.resolve())
    assert result["schematic_path"] == str(schematic_path)
    assert result["total_components"] == 1
    assert result["out_of_bounds_count"] == 1
    assert result["issues"][0]["component_ref"] == "R1"
