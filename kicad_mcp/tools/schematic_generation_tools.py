"""Schematic generation MCP tool registration."""

from fastmcp import FastMCP

from kicad_mcp.tools.design_intent_tools import register_design_intent_tools


def register_schematic_generation_tools(mcp: FastMCP) -> None:
    """Register the single public schematic-generation workflow."""
    register_design_intent_tools(mcp)
