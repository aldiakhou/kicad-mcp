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
from kicad_mcp.tools.schematic_edit_tools import register_schematic_edit_tools
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
    register_schematic_edit_tools,
    register_creation_tools,
)

AGENT_PROFILE_TOOLS = {
    "project_design_state",
    "create_kicad_project",
    "discover_projects",
    "get_project_structure",
    "schematic_apply_design_intent",
    "schematic_apply_design_intent_safe",
    "schematic_preview_design_intent",
    "schematic_apply_expanded_spec",
    "schematic_start_design_intent_job",
    "schematic_get_job_status",
    "schematic_get_job_result",
    "schematic_cancel_job",
    "schematic_design_intent_schema",
    "schematic_add_support_circuits",
    "schematic_add_decoupling_capacitor",
    "schematic_add_pullup_resistor",
    "schematic_add_passive",
    "schematic_apply_no_connect_rules",
    "schematic_build_from_spec_v2",
    "export_schematic_preview",
    "export_schematic_svg",
    "schematic_apply_functional_layout",
    "schematic_apply_connection_plan",
    "schematic_connect_pin_to_net",
    "schematic_connect_pins",
    "schematic_connect_pin_to_ground",
    "schematic_connect_pin_to_power",
    "schematic_snap_to_grid",
    "schematic_delete_item",
    "schematic_quality_report",
    "validate_project_boundaries",
    "generate_validation_report",
    "schematic_footprint_report",
    "schematic_assign_footprints",
    "schematic_assign_default_footprints",
    "schematic_explain_erc",
    "schematic_plan_erc_fixes",
    "schematic_apply_safe_erc_fixes",
    "run_erc_check",
    "find_symbols",
    "find_footprints",
    "resolve_symbol",
    "resolve_symbols",
    "resolve_footprint",
    "resolve_footprints",
}

ADVANCED_PROFILE_TOOLS = {
    "create_schematic_file",
    "schematic_add_symbol",
    "schematic_snap_to_grid",
    "schematic_delete_item",
    "schematic_apply_functional_layout",
    "list_symbol_libraries",
    "list_footprint_libraries",
}

DEBUG_PROFILE_TOOLS = {
    "schematic_add_wire",
    "schematic_add_label",
    "schematic_connect_points",
    "schematic_get_pin_map",
    "schematic_attach_net_to_pin",
    "schematic_preview_build_from_spec",
    "schematic_build_from_spec",
}


def register_all_tools(mcp: FastMCP) -> None:
    """Register all KiCad MCP tools in the canonical order."""
    for registrar in TOOL_REGISTRARS:
        registrar(mcp)
