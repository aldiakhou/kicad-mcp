"""Project creation and project-state MCP tools."""

from __future__ import annotations

from typing import Any

from fastmcp import Context, FastMCP

import kicad_mcp.tools.creation_tools as ct


def register_project_creation_tools(mcp: FastMCP) -> None:
    """Register project creation and project-state tools."""

    @mcp.tool()
    def create_kicad_project(
        project_dir: str | None = None,
        project_name: str = "",
        create_schematic: bool = True,
        create_pcb: bool = True,
        paper: str = "A4",
        directory: str | None = None,
        path: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Create a new KiCad project and optional schematic/PCB files."""
        resolved_dir = project_dir or directory or path
        resolved_name = project_name or name or ""
        if not resolved_dir:
            return {
                "success": False,
                "project_name": resolved_name,
                "error": "project_dir is required",
            }
        return ct._create_kicad_project(
            resolved_dir,
            resolved_name,
            create_schematic,
            create_pcb,
            paper,
        )

    @mcp.tool()
    def create_schematic_file(
        project_path: str | None = None,
        overwrite: bool = False,
        paper: str = "A4",
        path: str | None = None,
    ) -> dict[str, Any]:
        """Create a schematic file for an existing KiCad project."""
        try:
            return ct._create_schematic_file(
                ct._resolve_project_alias(project_path, path=path),
                overwrite=overwrite,
                paper=paper,
            )
        except Exception as exc:
            return {"success": False, "project_path": project_path or path, "error": str(exc)}

    @mcp.tool()
    def create_pcb_file(
        project_path: str | None = None,
        overwrite: bool = False,
        board_width_mm: float = 100.0,
        board_height_mm: float = 80.0,
        path: str | None = None,
    ) -> dict[str, Any]:
        """Create a PCB file for an existing KiCad project."""
        try:
            return ct._create_pcb_file(
                ct._resolve_project_alias(project_path, path=path),
                overwrite=overwrite,
                board_width_mm=board_width_mm,
                board_height_mm=board_height_mm,
            )
        except Exception as exc:
            return {"success": False, "project_path": project_path or path, "error": str(exc)}

    @mcp.tool()
    async def project_completion_report(
        project_path: str | None = None,
        run_erc: bool = True,
        run_drc: bool = False,
        timeout_seconds: float | None = None,
        path: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Summarize schematic, netlist, PCB sync, routing, and optional DRC status."""
        resolved_project = ct._resolve_project_alias(project_path, path=path)
        if ctx:
            await ctx.info("Building project completion report")
        return await ct._project_completion_report(
            resolved_project,
            run_erc,
            run_drc,
            timeout_seconds,
        )

    @mcp.tool()
    async def project_next_actions(
        project_path: str | None = None,
        run_erc: bool = True,
        run_drc: bool = False,
        timeout_seconds: float | None = None,
        path: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Return ordered generic next actions for bringing a project to completion."""
        resolved_project = ct._resolve_project_alias(project_path, path=path)
        if ctx:
            await ctx.info("Planning project next actions")
        return await ct._project_next_actions(
            resolved_project,
            run_erc,
            run_drc,
            timeout_seconds,
        )

    @mcp.tool()
    async def project_design_state(
        project_path: str | None = None,
        run_erc: bool = True,
        run_drc: bool = False,
        schematic_path: str | None = None,
        path: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Return one compact state object with the safest next KiCad MCP action."""
        resolved_project = ct._resolve_project_alias(project_path, schematic_path, path)
        if ctx:
            await ctx.info("Building project design state")
        return await ct._project_design_state(resolved_project, run_erc, run_drc)
