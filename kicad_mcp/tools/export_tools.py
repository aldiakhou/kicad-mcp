"""
Export tools for KiCad projects.
"""

import asyncio
import logging
import os
import subprocess

from fastmcp import Context, FastMCP

from kicad_mcp.utils.file_utils import get_project_files
from kicad_mcp.utils.kicad_cli import KiCadCLIError, get_kicad_cli_path
from kicad_mcp.utils.preview_metadata import SVG_MIME_TYPE, svg_preview_metadata

logger = logging.getLogger(__name__)


async def _generate_pcb_thumbnail_impl(project_path: str, ctx: Context | None):
    """Generate a PCB thumbnail without MCP decoration."""
    try:
        app_context = None
        if ctx:
            app_context = ctx.request_context.lifespan_context

        logger.info(f"Generating thumbnail via CLI for project: {project_path}")

        if not os.path.exists(project_path):
            logger.info(f"Project not found: {project_path}")
            if ctx:
                await ctx.info(f"Project not found: {project_path}")
            return {"success": False, "project_path": project_path, "error": "Project not found"}

        files = get_project_files(project_path)
        if "pcb" not in files:
            logger.info("PCB file not found in project")
            if ctx:
                await ctx.info("PCB file not found in project")
            return {"success": False, "project_path": project_path, "error": "PCB file not found in project"}

        pcb_file = files["pcb"]
        logger.info(f"Found PCB file: {pcb_file}")

        cache_key = f"thumbnail_cli_{pcb_file}_{os.path.getmtime(pcb_file)}"
        if app_context and hasattr(app_context, "cache") and cache_key in app_context.cache:
            logger.info(f"Using cached CLI thumbnail for {pcb_file}")
            return app_context.cache[cache_key]

        if ctx:
            await ctx.report_progress(10, 100)
            await ctx.info(
                f"Generating thumbnail for {os.path.basename(pcb_file)} using kicad-cli"
            )

        try:
            thumbnail = await generate_thumbnail_with_cli(pcb_file, ctx)
            if thumbnail and thumbnail.get("success"):
                thumbnail["project_path"] = project_path
                if app_context and hasattr(app_context, "cache"):
                    app_context.cache[cache_key] = thumbnail
                logger.info("Thumbnail generated successfully via CLI.")
                return thumbnail
            logger.warning("generate_thumbnail_with_cli returned None")
            if ctx:
                await ctx.info("Failed to generate thumbnail using kicad-cli.")
            return {
                "success": False,
                "project_path": project_path,
                "pcb_path": pcb_file,
                "error": "Failed to generate thumbnail using kicad-cli",
            }
        except Exception as e:
            logger.exception("Error calling generate_thumbnail_with_cli: %s", e)
            if ctx:
                await ctx.info(f"Error generating thumbnail with kicad-cli: {e}")
            return {"success": False, "project_path": project_path, "pcb_path": pcb_file, "error": str(e)}

    except asyncio.CancelledError:
        logger.info("Thumbnail generation cancelled")
        raise
    except Exception as e:
        logger.exception("Unexpected error in thumbnail generation: %s", e)
        if ctx:
            await ctx.info(f"Error: {e}")
        return {"success": False, "project_path": project_path, "error": str(e)}


def register_export_tools(mcp: FastMCP) -> None:
    """Register export tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """

    @mcp.tool()
    async def generate_pcb_thumbnail(project_path: str, ctx: Context | None):
        """Generate a thumbnail image of a KiCad PCB layout using kicad-cli.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            ctx: Context for MCP communication

        Returns:
            Thumbnail image of the PCB or None if generation failed
        """
        return await _generate_pcb_thumbnail_impl(project_path, ctx)

    @mcp.tool()
    async def generate_project_thumbnail(project_path: str, ctx: Context | None):
        """Generate a thumbnail of a KiCad project's PCB layout (Alias for generate_pcb_thumbnail)."""
        logger.info(
            f"generate_project_thumbnail called, redirecting to generate_pcb_thumbnail for {project_path}"
        )
        return await _generate_pcb_thumbnail_impl(project_path, ctx)


# Helper functions for thumbnail generation
async def generate_thumbnail_with_cli(pcb_file: str, ctx: Context | None):
    """Generate PCB thumbnail using command line tools.
    This is a fallback method when the kicad Python module is not available or fails.

    Args:
        pcb_file: Path to the PCB file (.kicad_pcb)
        ctx: MCP context for progress reporting

    Returns:
        JSON-safe thumbnail metadata or None if generation failed
    """
    try:
        logger.info("Attempting to generate thumbnail using KiCad CLI tools")
        if ctx:
            await ctx.report_progress(20, 100)

        # --- Determine Output Path ---
        project_dir = os.path.dirname(pcb_file)
        project_name = os.path.splitext(os.path.basename(pcb_file))[0]
        output_file = os.path.join(project_dir, f"{project_name}_thumbnail.svg")
        # ---------------------------

        try:
            kicad_cli = get_kicad_cli_path()
        except KiCadCLIError as exc:
            logger.warning("KiCad CLI unavailable for thumbnail generation: %s", exc)
            if ctx:
                await ctx.info(str(exc))
            return None

        if ctx:
            await ctx.report_progress(30, 100)
            await ctx.info("Using KiCad command line tools for thumbnail generation")
        cmd = [
            kicad_cli,
            "pcb",
            "export",
            "svg",
            "--output",
            output_file,
            "--layers",
            "F.Cu,B.Cu,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts",
            pcb_file,
        ]

        logger.info(f"Running command: {' '.join(cmd)}")
        if ctx:
            await ctx.report_progress(50, 100)

        # Run the command
        try:
            process = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
            logger.info(f"Command successful: {process.stdout}")

            if ctx:
                await ctx.report_progress(70, 100)

            # Check if the output file was created
            if not os.path.exists(output_file):
                logger.info(f"Output file not created: {output_file}")
                return None

            preview = svg_preview_metadata(output_file)
            logger.info(
                "Successfully generated thumbnail with CLI, size: %s bytes",
                preview["file_size"],
            )
            if ctx:
                await ctx.report_progress(90, 100)
                # Inform user about the saved file
                await ctx.info(f"Thumbnail saved to: {output_file}")
            return {
                "success": True,
                "pcb_path": pcb_file,
                "thumbnail_path": output_file,
                "mime_type": SVG_MIME_TYPE,
                "file_size": preview["file_size"],
                "preview": preview,
            }

        except subprocess.CalledProcessError as e:
            logger.error("Command '%s' failed with code %s", " ".join(e.cmd), e.returncode)
            logger.error("Stderr: %s", e.stderr)
            logger.error("Stdout: %s", e.stdout)
            if ctx:
                await ctx.info(f"KiCad CLI command failed: {e.stderr or e.stdout}")
            return None
        except subprocess.TimeoutExpired:
            logger.info(f"Command timed out after 30 seconds: {' '.join(cmd)}")
            if ctx:
                await ctx.info("KiCad CLI command timed out")
            return None
        except Exception as e:
            logger.exception("Error running CLI command: %s", e)
            if ctx:
                await ctx.info(f"Error running KiCad CLI: {e}")
            return None

    except asyncio.CancelledError:
        logger.info("CLI thumbnail generation cancelled")
        raise
    except Exception as e:
        logger.exception("Unexpected error in CLI thumbnail generation: %s", e)
        if ctx:
            await ctx.info(f"Unexpected error: {e}")
        return None
