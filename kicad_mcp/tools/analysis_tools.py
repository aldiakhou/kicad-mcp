"""
Analysis and validation tools for KiCad projects.
"""

import json
from typing import Any

from fastmcp import FastMCP

from kicad_mcp.utils.file_utils import get_project_files
from kicad_mcp.utils.path_validator import PathValidationError
from kicad_mcp.utils.transactional_edit import validate_local_path


def register_analysis_tools(mcp: FastMCP) -> None:
    """Register analysis and validation tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """

    @mcp.tool()
    def validate_project(project_path: str) -> dict[str, Any]:
        """Basic validation of a KiCad project."""
        try:
            validated_project = validate_local_path(project_path, "project", must_exist=True)
        except PathValidationError as exc:
            return {"valid": False, "error": str(exc)}

        issues = []
        files = get_project_files(validated_project)

        # Check for essential files
        if "pcb" not in files:
            issues.append("Missing PCB layout file")

        if "schematic" not in files:
            issues.append("Missing schematic file")

        # Validate project file
        try:
            with open(validated_project, encoding="utf-8") as f:
                json.load(f)
        except json.JSONDecodeError:
            issues.append("Invalid project file format (JSON parsing error)")
        except Exception as e:
            issues.append(f"Error reading project file: {str(e)}")

        return {
            "valid": len(issues) == 0,
            "path": validated_project,
            "issues": issues if issues else None,
            "files_found": list(files.keys()),
        }
