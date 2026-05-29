"""
Project management tools for KiCad.
"""

import logging
import os
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from kicad_mcp.utils.file_utils import get_project_files, load_project_json
from kicad_mcp.utils.kicad_utils import find_kicad_projects, open_kicad_project
from kicad_mcp.utils.path_validator import PathValidationError
from kicad_mcp.utils.transactional_edit import validate_local_path

# Get PID for logging
# _PID = os.getpid()


def register_project_tools(mcp: FastMCP) -> None:
    """Register project management tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """

    @mcp.tool()
    def list_projects() -> list[dict[str, Any]]:
        """Find and list all KiCad projects on this system."""
        logging.info("Executing list_projects tool...")
        projects = find_kicad_projects()
        logging.info(f"list_projects tool returning {len(projects)} projects.")
        return projects

    @mcp.tool()
    def discover_projects() -> dict[str, Any]:
        """Find KiCad projects and return a structured tool response."""
        logging.info("Executing discover_projects tool...")
        projects = find_kicad_projects()
        return {"success": True, "projects": projects, "count": len(projects)}

    @mcp.tool()
    def get_project_structure(project_path: str) -> dict[str, Any]:
        """Get the structure and files of a KiCad project."""
        try:
            validated_project = validate_local_path(project_path, "project", must_exist=True)
        except PathValidationError as exc:
            return {"success": False, "project_path": project_path, "error": str(exc)}

        project_dir = os.path.dirname(validated_project)
        project_name = Path(validated_project).stem

        # Get related files
        files = get_project_files(validated_project)

        # Get project metadata
        metadata = {}
        project_data = load_project_json(validated_project)
        if project_data and "metadata" in project_data:
            metadata = project_data["metadata"]

        return {
            "success": True,
            "name": project_name,
            "path": validated_project,
            "directory": project_dir,
            "files": files,
            "metadata": metadata,
        }

    @mcp.tool()
    def open_project(project_path: str) -> dict[str, Any]:
        """Open a KiCad project in KiCad."""
        try:
            validated_project = validate_local_path(project_path, "project", must_exist=True)
        except PathValidationError as exc:
            return {"success": False, "project_path": project_path, "error": str(exc)}
        return open_kicad_project(validated_project)
