"""Central tool registration manifest for KiCad MCP."""

from __future__ import annotations

from collections.abc import Callable

from fastmcp import FastMCP

from kicad_mcp.tools.analysis_tools import register_analysis_tools
from kicad_mcp.tools.bom_tools import register_bom_tools
from kicad_mcp.tools.creation_tools import register_creation_tools
from kicad_mcp.tools.drc_tools import register_drc_tools
from kicad_mcp.tools.export_tools import register_export_tools
from kicad_mcp.tools.netlist_tools import register_netlist_tools
from kicad_mcp.tools.pattern_tools import register_pattern_tools
from kicad_mcp.tools.project_tools import register_project_tools
from kicad_mcp.tools.validation_tools import register_validation_tools

ToolRegistrar = Callable[[FastMCP], None]

TOOL_REGISTRARS: tuple[ToolRegistrar, ...] = (
    register_project_tools,
    register_analysis_tools,
    register_export_tools,
    register_drc_tools,
    register_bom_tools,
    register_netlist_tools,
    register_pattern_tools,
    register_validation_tools,
    register_creation_tools,
)

AGENT_PROFILE_TOOLS = {
    "project_design_state",
    "create_kicad_project",
    "discover_projects",
    "get_project_structure",
    "schematic_preview_design_intent",
    "schematic_start_design_intent_job",
    "schematic_get_job_status",
    "schematic_get_job_result",
    "schematic_cancel_job",
    "schematic_design_intent_schema",
    "export_schematic_preview",
    "export_schematic_svg",
    "schematic_validate_generated_schematic",
    "schematic_engine_status",
    "validate_project_boundaries",
    "generate_validation_report",
    "find_symbols",
    "find_footprints",
    "resolve_symbol",
    "resolve_symbols",
    "resolve_footprint",
    "resolve_footprints",
}

ADVANCED_PROFILE_TOOLS = {
    "create_schematic_file",
    "create_pcb_file",
    "project_completion_report",
    "project_next_actions",
    "list_symbol_libraries",
    "list_footprint_libraries",
    "pcb_sync_from_schematic",
    "pcb_complete_from_schematic",
    "pcb_sync_place_and_report",
    "pcb_apply_functional_placement",
    "pcb_get_ratsnest",
    "pcb_quality_report",
}

DEBUG_PROFILE_TOOLS = {
    "extract_schematic_netlist",
    "extract_project_netlist",
    "analyze_schematic_connections",
    "find_component_connections",
    "pcb_add_footprint",
    "pcb_move_footprint",
    "pcb_create_board_outline",
    "pcb_add_track",
    "pcb_add_via",
    "pcb_generate_basic_layout",
    "pcb_route_net_manhattan",
    "pcb_route_between_pads",
    "pcb_route_ratsnest_connection",
}


def register_all_tools(mcp: FastMCP) -> None:
    """Register all KiCad MCP tools in the canonical order."""
    for registrar in TOOL_REGISTRARS:
        registrar(mcp)
