"""
Design Rule Check (DRC) implementation using KiCad command-line interface.
"""

import json
import logging
import os
import subprocess
import tempfile
from typing import Any

from fastmcp import Context

from kicad_mcp.config import TIMEOUT_CONSTANTS
from kicad_mcp.utils.kicad_cli import KiCadCLIError, get_kicad_cli_path

logger = logging.getLogger(__name__)


def _resolve_drc_timeout(timeout_seconds: float | None) -> float:
    """Resolve DRC timeout from explicit argument, environment, or config default."""
    if timeout_seconds is not None and timeout_seconds > 0:
        return float(timeout_seconds)
    env_value = os.getenv("KICAD_DRC_TIMEOUT")
    if env_value:
        try:
            parsed = float(env_value)
            if parsed > 0:
                return parsed
        except ValueError:
            pass
    return float(TIMEOUT_CONSTANTS["kicad_cli_drc"])


async def run_drc_via_cli(
    pcb_file: str, ctx: Context | None, timeout_seconds: float | None = None
) -> dict[str, Any]:
    """Run DRC using KiCad command line tools.

    Args:
        pcb_file: Path to the PCB file (.kicad_pcb)
        ctx: MCP context for progress reporting

    Returns:
        Dictionary with DRC results
    """
    resolved_timeout = _resolve_drc_timeout(timeout_seconds)
    results = {
        "success": False,
        "method": "cli",
        "pcb_file": pcb_file,
        "timeout_seconds": resolved_timeout,
    }

    try:
        # Create a temporary directory for the output
        with tempfile.TemporaryDirectory() as temp_dir:
            # Output file for DRC report
            output_file = os.path.join(temp_dir, "drc_report.json")

            try:
                kicad_cli = get_kicad_cli_path()
            except KiCadCLIError as exc:
                logger.warning("KiCad CLI unavailable for DRC: %s", exc)
                results["error"] = str(exc)
                return results

            # Report progress
            if ctx:
                await ctx.report_progress(50, 100)
                await ctx.info("Running DRC using KiCad CLI...")

            # Build the DRC command
            cmd = [kicad_cli, "pcb", "drc", "--format", "json", "--output", output_file, pcb_file]

            logger.info("Running command: %s", " ".join(cmd))
            process = subprocess.run(
                cmd, capture_output=True, text=True, timeout=resolved_timeout
            )

            # Check if the command was successful
            if process.returncode != 0:
                logger.error("DRC command failed with code %s", process.returncode)
                logger.error("Error output: %s", process.stderr)
                results["error"] = f"DRC command failed: {process.stderr}"
                return results

            # Check if the output file was created
            if not os.path.exists(output_file):
                logger.info("DRC report file not created")
                results["error"] = "DRC report file not created"
                return results

            # Read the DRC report
            with open(output_file) as f:
                try:
                    drc_report = json.load(f)
                except json.JSONDecodeError:
                    logger.error("Failed to parse DRC report JSON")
                    results["error"] = "Failed to parse DRC report JSON"
                    return results

            # Process the DRC report
            violations = drc_report.get("violations", [])
            violation_count = len(violations)
            logger.info(f"DRC completed with {violation_count} violations")
            if ctx:
                await ctx.report_progress(70, 100)
                await ctx.info(f"DRC completed with {violation_count} violations")

            # Categorize violations by type
            error_types = {}
            for violation in violations:
                error_type = violation.get("message", "Unknown")
                if error_type not in error_types:
                    error_types[error_type] = 0
                error_types[error_type] += 1

            # Create success response
            results = {
                "success": True,
                "method": "cli",
                "pcb_file": pcb_file,
                "timeout_seconds": resolved_timeout,
                "total_violations": violation_count,
                "violation_categories": error_types,
                "violations": violations,
            }

            if ctx:
                await ctx.report_progress(90, 100)
            return results

    except subprocess.TimeoutExpired as e:
        logger.warning("CLI DRC timed out after %s seconds: %s", resolved_timeout, e)
        results["error"] = (
            f"KiCad CLI DRC timed out after {resolved_timeout:g} seconds. "
            "Increase timeout_seconds or KICAD_DRC_TIMEOUT for larger boards."
        )
        return results
    except Exception as e:
        logger.exception("Error in CLI DRC: %s", e)
        results["error"] = f"Error in CLI DRC: {e}"
        return results
