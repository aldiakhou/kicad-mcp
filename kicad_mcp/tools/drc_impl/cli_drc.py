"""
Design Rule Check (DRC) implementation using KiCad command-line interface.
"""

import json
import logging
import os
import tempfile
from typing import Any

from fastmcp import Context

from kicad_mcp.config import TIMEOUT_CONSTANTS
from kicad_mcp.utils.kicad_cli import KiCadCLIError
from kicad_mcp.utils.path_validator import PathValidationError, PathValidator
from kicad_mcp.utils.secure_subprocess import SecureSubprocessError, SecureSubprocessRunner

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
        pcb_file = os.path.realpath(os.path.expanduser(pcb_file))
        results["pcb_file"] = pcb_file
        # Create a temporary directory for the output
        with tempfile.TemporaryDirectory() as temp_dir:
            # Output file for DRC report
            output_file = os.path.join(temp_dir, "drc_report.json")

            # Report progress
            if ctx:
                await ctx.report_progress(50, 100)
                await ctx.info("Running DRC using KiCad CLI...")

            # Build the DRC command
            command_args = ["pcb", "drc", "--format", "json", "--output", output_file, pcb_file]

            logger.info("Running KiCad CLI DRC for: %s", pcb_file)
            pcb_dir = os.path.dirname(os.path.realpath(os.path.expanduser(pcb_file))) or os.getcwd()
            runner = SecureSubprocessRunner(
                path_validator=PathValidator(trusted_roots={pcb_dir, temp_dir})
            )
            process = await runner.run_kicad_command_async(
                command_args,
                input_files=[pcb_file],
                output_files=[output_file],
                working_dir=pcb_dir,
                timeout=resolved_timeout,
            )

            # Check if the output file was created
            if not os.path.exists(output_file):
                if process.returncode != 0:
                    logger.error("DRC command failed with code %s", process.returncode)
                    logger.error("Error output: %s", process.stderr)
                    results["error"] = f"DRC command failed: {process.stderr}"
                    return results
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

            # Process the DRC report. KiCad reports unrouted ratsnest items
            # separately from rule violations; count them as actionable DRC
            # findings so validation does not falsely look clean.
            violations = _drc_report_violations(drc_report)
            violation_count = len(violations)
            logger.info(f"DRC completed with {violation_count} violations")
            if ctx:
                await ctx.report_progress(70, 100)
                await ctx.info(f"DRC completed with {violation_count} violations")

            # Categorize violations by type
            error_types = {}
            for violation in violations:
                error_type = (
                    violation.get("type")
                    or violation.get("description")
                    or violation.get("message")
                    or "unknown"
                )
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
                "report": drc_report,
            }

            if ctx:
                await ctx.report_progress(90, 100)
            return results

    except (KiCadCLIError, PathValidationError, SecureSubprocessError) as e:
        logger.warning("CLI DRC failed: %s", e)
        error_text = str(e)
        if "timed out" in error_text.lower():
            results["error"] = (
                f"KiCad CLI DRC timed out after {resolved_timeout:g} seconds. "
                "Increase timeout_seconds or KICAD_DRC_TIMEOUT for larger boards."
            )
        else:
            results["error"] = error_text
        return results
    except Exception as e:
        logger.exception("Error in CLI DRC: %s", e)
        results["error"] = f"Error in CLI DRC: {e}"
        return results


def _drc_report_violations(report: dict[str, Any]) -> list[dict[str, Any]]:
    violations = list(report.get("violations", []))
    for item in report.get("unconnected_items", []):
        if not isinstance(item, dict):
            continue
        normalized = dict(item)
        normalized.setdefault("type", "unconnected_item")
        normalized.setdefault("severity", "warning")
        normalized.setdefault("description", "Unconnected item")
        violations.append(normalized)
    return violations
