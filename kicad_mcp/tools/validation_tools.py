"""
Validation tools for KiCad projects.

Provides tools for validating circuit positioning, generating reports,
and checking component boundaries in existing projects.
"""

from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP

from kicad_mcp.utils.boundary_validator import BoundaryValidator, SchematicBounds
from kicad_mcp.utils.file_utils import get_project_files
from kicad_mcp.utils.kicad_s_expr import KiCadSchematic, SExpressionError
from kicad_mcp.utils.path_validator import get_configured_validator
from kicad_mcp.utils.transactional_edit import validate_local_path


async def validate_project_boundaries(
    project_path: str, ctx: Context | None = None
) -> dict[str, Any]:
    """
    Validate component boundaries for an entire KiCad project.

    Args:
        project_path: Path to the KiCad project file (.kicad_pro)
        ctx: Context for MCP communication

    Returns:
        Dictionary with validation results and report
    """
    try:
        if ctx:
            await ctx.info("Starting boundary validation for project")
            await ctx.report_progress(10, 100)

        validated_project = validate_local_path(project_path, "project", must_exist=True)

        # Get project files
        files = get_project_files(validated_project)
        if "schematic" not in files:
            return {
                "success": False,
                "project_path": validated_project,
                "error": "No schematic file found in project",
            }

        schematic_file = files["schematic"]

        if ctx:
            await ctx.report_progress(30, 100)
            await ctx.info(f"Reading schematic file: {schematic_file}")

        # Read schematic file
        content = Path(schematic_file).read_text(encoding="utf-8").strip()

        # Parse components based on format
        components = []

        if content.startswith("(kicad_sch"):
            try:
                schematic = KiCadSchematic.from_text(content)
            except SExpressionError as exc:
                return {
                    "success": False,
                    "project_path": validated_project,
                    "schematic_path": schematic_file,
                    "error": f"Invalid KiCad schematic S-expression: {exc}",
                }
            components = _extract_components_from_sexpr(content)
            sheet_bounds = schematic.get_sheet_bounds()
            bounds = SchematicBounds(
                width=float(sheet_bounds["width"]),
                height=float(sheet_bounds["height"]),
            )
        else:
            # JSON format
            try:
                schematic_data = json.loads(content)
                components = _extract_components_from_json(schematic_data)
            except json.JSONDecodeError:
                return {
                    "success": False,
                    "project_path": validated_project,
                    "schematic_path": schematic_file,
                    "error": "Schematic file is neither valid S-expression nor JSON format",
                }
            bounds = SchematicBounds()

        if ctx:
            await ctx.report_progress(60, 100)
            await ctx.info(f"Found {len(components)} components to validate")

        # Run boundary validation
        validator = BoundaryValidator(bounds=bounds)
        validation_report = validator.validate_circuit_components(components)

        if ctx:
            await ctx.report_progress(80, 100)
            await ctx.info(
                f"Validation complete: {validation_report.out_of_bounds_count} out of bounds"
            )

        # Generate text report
        report_text = validator.generate_validation_report_text(validation_report)

        if ctx:
            await ctx.info(f"Validation Report:\n{report_text}")
            await ctx.report_progress(100, 100)

        # Create result
        result = {
            "success": validation_report.success,
            "project_path": validated_project,
            "schematic_path": schematic_file,
            "total_components": validation_report.total_components,
            "out_of_bounds_count": validation_report.out_of_bounds_count,
            "corrected_positions": validation_report.corrected_positions,
            "report_text": report_text,
            "has_errors": validation_report.has_errors(),
            "has_warnings": validation_report.has_warnings(),
            "issues": [
                {
                    "severity": issue.severity.value,
                    "component_ref": issue.component_ref,
                    "message": issue.message,
                    "position": issue.position,
                    "suggested_position": issue.suggested_position,
                }
                for issue in validation_report.issues
            ],
        }

        return result

    except Exception as e:
        error_msg = f"Error validating project boundaries: {str(e)}"
        if ctx:
            await ctx.info(error_msg)
        return {"success": False, "error": error_msg}


async def generate_validation_report(
    project_path: str, output_path: str | None = None, ctx: Context | None = None
) -> dict[str, Any]:
    """
    Generate a comprehensive validation report for a KiCad project.

    Args:
        project_path: Path to the KiCad project file (.kicad_pro)
        output_path: Optional path to save the report (defaults to project directory)
        ctx: Context for MCP communication

    Returns:
        Dictionary with report generation results
    """
    try:
        if ctx:
            await ctx.info("Generating validation report")
            await ctx.report_progress(10, 100)

        validated_project = validate_local_path(project_path, "project", must_exist=True)

        # Run validation
        validation_result = await validate_project_boundaries(validated_project, ctx)

        if not validation_result["success"]:
            return validation_result

        # Determine output path
        if output_path is None:
            project_dir = os.path.dirname(validated_project)
            project_name = Path(validated_project).stem
            output_path = os.path.join(project_dir, f"{project_name}_validation_report.json")
        else:
            output_path = os.path.realpath(os.path.expanduser(output_path))

        validator = get_configured_validator()
        output_dir = os.path.dirname(output_path) or os.getcwd()
        validator.validate_directory(output_dir, must_exist=True)
        output_path = validator.validate_path(output_path, must_exist=False)

        if ctx:
            await ctx.report_progress(80, 100)
            await ctx.info(f"Saving report to: {output_path}")

        # Save detailed report
        report_data = {
            "project_path": validated_project,
            "schematic_path": validation_result.get("schematic_path"),
            "validation_timestamp": datetime.now().isoformat(),
            "summary": {
                "total_components": validation_result["total_components"],
                "out_of_bounds_count": validation_result["out_of_bounds_count"],
                "has_errors": validation_result["has_errors"],
                "has_warnings": validation_result["has_warnings"],
            },
            "corrected_positions": validation_result["corrected_positions"],
            "issues": validation_result["issues"],
            "report_text": validation_result["report_text"],
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=2)

        if ctx:
            await ctx.report_progress(100, 100)
            await ctx.info("Validation report generated successfully")

        return {"success": True, "report_path": output_path, "summary": report_data["summary"]}

    except Exception as e:
        error_msg = f"Error generating validation report: {str(e)}"
        if ctx:
            await ctx.info(error_msg)
        return {"success": False, "error": error_msg}


def _extract_components_from_sexpr(content: str) -> list[dict[str, Any]]:
    """Extract component information from S-expression format."""
    components = []
    schematic = KiCadSchematic.from_text(content)
    for symbol in schematic.list_symbols():
        lib_id = str(symbol.get("lib_id") or "")
        position = symbol.get("position", {})
        x_pos = float(position.get("x", 0.0))
        y_pos = float(position.get("y", 0.0))
        reference = str(symbol.get("reference") or "Unknown")

        # Determine component type from lib_id
        component_type = _get_component_type_from_lib_id(lib_id)

        components.append(
            {
                "reference": reference,
                "position": (x_pos, y_pos),
                "component_type": component_type,
                "lib_id": lib_id,
            }
        )

    return components


def _extract_components_from_json(schematic_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract component information from JSON format."""
    components = []

    if "symbol" in schematic_data:
        for symbol in schematic_data["symbol"]:
            # Extract reference
            reference = "Unknown"
            if "property" in symbol:
                for prop in symbol["property"]:
                    if prop.get("name") == "Reference":
                        reference = prop.get("value", "Unknown")
                        break

            # Extract position
            position = (0, 0)
            if "at" in symbol and len(symbol["at"]) >= 2:
                # Convert from internal units to mm
                x_pos = float(symbol["at"][0]) / 10.0
                y_pos = float(symbol["at"][1]) / 10.0
                position = (x_pos, y_pos)

            # Determine component type
            lib_id = symbol.get("lib_id", "")
            component_type = _get_component_type_from_lib_id(lib_id)

            components.append(
                {
                    "reference": reference,
                    "position": position,
                    "component_type": component_type,
                    "lib_id": lib_id,
                }
            )

    return components


def _get_component_type_from_lib_id(lib_id: str) -> str:
    """Determine component type from library ID."""
    lib_id_lower = lib_id.lower()

    if "resistor" in lib_id_lower or ":r" in lib_id_lower:
        return "resistor"
    elif "capacitor" in lib_id_lower or ":c" in lib_id_lower:
        return "capacitor"
    elif "inductor" in lib_id_lower or ":l" in lib_id_lower:
        return "inductor"
    elif "led" in lib_id_lower:
        return "led"
    elif "diode" in lib_id_lower or ":d" in lib_id_lower:
        return "diode"
    elif "transistor" in lib_id_lower or "npn" in lib_id_lower or "pnp" in lib_id_lower:
        return "transistor"
    elif "power:" in lib_id_lower:
        return "power"
    elif "switch" in lib_id_lower:
        return "switch"
    elif "connector" in lib_id_lower:
        return "connector"
    elif "mcu" in lib_id_lower or "ic" in lib_id_lower or ":u" in lib_id_lower:
        return "ic"
    else:
        return "default"


def register_validation_tools(mcp: FastMCP) -> None:
    """Register validation tools with the MCP server."""

    @mcp.tool(name="validate_project_boundaries")
    async def validate_project_boundaries_tool(
        project_path: str, ctx: Context | None = None
    ) -> dict[str, Any]:
        """Validate component boundaries for an entire KiCad project."""
        return await validate_project_boundaries(project_path, ctx)

    @mcp.tool(name="generate_validation_report")
    async def generate_validation_report_tool(
        project_path: str, output_path: str | None = None, ctx: Context | None = None
    ) -> dict[str, Any]:
        """Generate a comprehensive validation report for a KiCad project."""
        return await generate_validation_report(project_path, output_path, ctx)
