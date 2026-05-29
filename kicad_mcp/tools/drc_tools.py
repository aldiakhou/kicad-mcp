"""
Design Rule Check (DRC) tools for KiCad PCB files.
"""

import logging
import os
from typing import Any

from fastmcp import Context, FastMCP

from kicad_mcp.tools.drc_impl.cli_drc import run_drc_via_cli
from kicad_mcp.utils.drc_history import compare_with_previous, get_drc_history, save_drc_result
from kicad_mcp.utils.file_utils import get_project_files
from kicad_mcp.utils.native_netlist import run_erc_via_cli
from kicad_mcp.utils.path_validator import PathValidationError
from kicad_mcp.utils.transactional_edit import validate_local_path

logger = logging.getLogger(__name__)


def register_drc_tools(mcp: FastMCP) -> None:
    """Register DRC tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """

    @mcp.tool()
    def get_drc_history_tool(project_path: str) -> dict[str, Any]:
        """Get the DRC check history for a KiCad project.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)

        Returns:
            Dictionary with DRC history entries
        """
        logger.info(f"Getting DRC history for project: {project_path}")

        try:
            validated_project = validate_local_path(project_path, "project", must_exist=True)
        except PathValidationError as exc:
            logger.info(f"Project not found: {project_path}")
            return {"success": False, "error": str(exc)}

        # Get history entries
        history_entries = get_drc_history(validated_project)

        # Calculate trend information
        trend = None
        if len(history_entries) >= 2:
            first = history_entries[-1]  # Oldest entry
            last = history_entries[0]  # Newest entry

            first_violations = first.get("total_violations", 0)
            last_violations = last.get("total_violations", 0)

            if first_violations > last_violations:
                trend = "improving"
            elif first_violations < last_violations:
                trend = "degrading"
            else:
                trend = "stable"

        return {
            "success": True,
            "project_path": validated_project,
            "history_entries": history_entries,
            "entry_count": len(history_entries),
            "trend": trend,
        }

    @mcp.tool()
    async def run_erc_check(
        project_path: str, ctx: Context | None, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        """Run an Electrical Rule Check on a KiCad schematic or project."""
        logger.info("Running ERC check for: %s", project_path)
        try:
            if project_path.endswith(".kicad_pro"):
                validated_path = validate_local_path(project_path, "project", must_exist=True)
            else:
                validated_path = validate_local_path(project_path, "schematic", must_exist=True)
        except PathValidationError as exc:
            return {"success": False, "error": str(exc)}
        schematic_path = validated_path
        if validated_path.endswith(".kicad_pro"):
            files = get_project_files(validated_path)
            if "schematic" not in files:
                return {"success": False, "project_path": validated_path, "error": "Schematic file not found in project"}
            schematic_path = files["schematic"]
        if ctx:
            await ctx.report_progress(10, 100)
            await ctx.info(f"Starting ERC check on {os.path.basename(schematic_path)}")
        result = run_erc_via_cli(schematic_path, timeout_seconds=timeout_seconds)
        if ctx:
            await ctx.report_progress(100, 100)
        if validated_path.endswith(".kicad_pro"):
            result["project_path"] = validated_path
        return result

    @mcp.tool()
    async def run_drc_check(
        project_path: str, ctx: Context | None, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        """Run a Design Rule Check on a KiCad PCB file.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            ctx: MCP context for progress reporting

        Returns:
            Dictionary with DRC results and statistics
        """
        logger.info(f"Running DRC check for project: {project_path}")

        try:
            validated_project = validate_local_path(project_path, "project", must_exist=True)
        except PathValidationError as exc:
            logger.info(f"Project not found: {project_path}")
            return {"success": False, "error": str(exc)}

        # Get PCB file from project
        files = get_project_files(validated_project)
        if "pcb" not in files:
            logger.info("PCB file not found in project")
            return {"success": False, "error": "PCB file not found in project"}

        pcb_file = files["pcb"]
        logger.info(f"Found PCB file: {pcb_file}")

        # Report progress to user
        if ctx:
            await ctx.report_progress(10, 100)
            await ctx.info(f"Starting DRC check on {os.path.basename(pcb_file)}")

        # Run DRC using the appropriate approach
        drc_results = None

        logger.info("Using kicad-cli for DRC")
        if ctx:
            await ctx.info("Using KiCad CLI for DRC check...")
        # logging.info(f"[DRC] Calling run_drc_via_cli for {pcb_file}") # <-- Remove log
        drc_results = await run_drc_via_cli(pcb_file, ctx, timeout_seconds=timeout_seconds)
        # logging.info(f"[DRC] run_drc_via_cli finished for {pcb_file}") # <-- Remove log

        # Process and save results if successful
        if drc_results and drc_results.get("success", False):
            # logging.info(f"[DRC] DRC check successful for {pcb_file}. Saving results.") # <-- Remove log
            # Save results to history
            save_drc_result(validated_project, drc_results)

            # Add comparison with previous run
            comparison = compare_with_previous(validated_project, drc_results)
            if comparison:
                drc_results["comparison"] = comparison

                if ctx:
                    if comparison["change"] < 0:
                        await ctx.info(
                            f"Great progress! You've fixed {abs(comparison['change'])} DRC violations since the last check."
                        )
                    elif comparison["change"] > 0:
                        await ctx.info(
                            f"Found {comparison['change']} new DRC violations since the last check."
                        )
                    else:
                        await ctx.info(
                            "No change in the number of DRC violations since the last check."
                        )
        elif drc_results:
            logger.warning(
                "DRC check reported failure for %s: %s",
                pcb_file,
                drc_results.get("error") or drc_results,
            )
            drc_results.setdefault("project_path", validated_project)
            drc_results.setdefault("pcb_path", pcb_file)
        else:
            logger.error("DRC check returned no diagnostics for %s", pcb_file)

        # Complete progress
        if ctx:
            await ctx.report_progress(100, 100)

        return drc_results or {
            "success": False,
            "project_path": validated_project,
            "pcb_path": pcb_file,
            "error": "DRC check failed with no diagnostics from KiCad CLI",
        }
