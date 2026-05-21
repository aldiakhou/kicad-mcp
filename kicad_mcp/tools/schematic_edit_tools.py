"""
Safe schematic inspection, editing, validation, and preview tools.
"""

import logging
import os
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.utilities.types import Image

from kicad_mcp.utils.file_utils import get_project_files
from kicad_mcp.utils.kicad_s_expr import KiCadSchematic, compare_connectivity_snapshots
from kicad_mcp.utils.path_validator import PathValidator
from kicad_mcp.utils.secure_subprocess import SecureSubprocessRunner
from kicad_mcp.utils.transactional_edit import (
    apply_transactional_schematic_edit,
    backup_project_files,
    get_file_diff_against_backup,
    restore_backup_manifest,
    validate_local_path,
    validate_schematic_file_safely,
    validate_schematic_with_cli_export,
)

logger = logging.getLogger(__name__)


def register_schematic_edit_tools(mcp: FastMCP) -> None:
    """Register safe schematic inspection and editing tools."""

    @mcp.tool()
    def validate_schematic_syntax(schematic_path: str) -> dict[str, Any]:
        """Validate schematic S-expression syntax safely."""
        return validate_schematic_file_safely(schematic_path)

    @mcp.tool()
    def backup_project(project_path: str) -> dict[str, Any]:
        """Create a timestamped backup of a KiCad project and related files."""
        return backup_project_files(project_path)

    @mcp.tool()
    def restore_backup(backup_path: str) -> dict[str, Any]:
        """Restore files from a previously created backup."""
        return restore_backup_manifest(backup_path)

    @mcp.tool()
    def get_file_diff(file_path: str, backup_path: str) -> dict[str, Any]:
        """Return a unified diff between a file and a backup."""
        try:
            return get_file_diff_against_backup(file_path, backup_path)
        except Exception as exc:
            return {"success": False, "file_path": file_path, "backup_path": backup_path, "error": str(exc)}

    @mcp.tool()
    def schematic_list_symbols(schematic_path: str) -> dict[str, Any]:
        """List schematic symbols with positions, UUIDs, and properties."""
        try:
            schematic = _load_schematic(schematic_path)
            return {
                "success": True,
                "schematic_path": schematic_path,
                "symbols": schematic.list_symbols(),
            }
        except Exception as exc:
            return {"success": False, "schematic_path": schematic_path, "error": str(exc)}

    @mcp.tool()
    def schematic_list_labels(schematic_path: str) -> dict[str, Any]:
        """List schematic labels with positions and UUIDs."""
        try:
            schematic = _load_schematic(schematic_path)
            return {
                "success": True,
                "schematic_path": schematic_path,
                "labels": schematic.list_labels(),
            }
        except Exception as exc:
            return {"success": False, "schematic_path": schematic_path, "error": str(exc)}

    @mcp.tool()
    def schematic_list_wires(schematic_path: str) -> dict[str, Any]:
        """List schematic wires and points."""
        try:
            schematic = _load_schematic(schematic_path)
            return {
                "success": True,
                "schematic_path": schematic_path,
                "wires": schematic.list_wires(),
            }
        except Exception as exc:
            return {"success": False, "schematic_path": schematic_path, "error": str(exc)}

    @mcp.tool()
    def schematic_connectivity_snapshot(
        schematic_path: str,
        target_type: str | None = None,
        target_id: str | None = None,
    ) -> dict[str, Any]:
        """Return a local geometric connectivity snapshot."""
        try:
            schematic = _load_schematic(schematic_path)
            snapshot = (
                schematic.connectivity_snapshot()
                if target_type is None or target_id is None
                else schematic.target_connectivity_snapshot(target_type, target_id)
            )
            return {
                "success": True,
                "schematic_path": schematic_path,
                "target_type": target_type,
                "target_id": target_id,
                "snapshot": snapshot,
            }
        except Exception as exc:
            return {"success": False, "schematic_path": schematic_path, "error": str(exc)}

    @mcp.tool()
    def schematic_get_symbol(schematic_path: str, reference: str) -> dict[str, Any]:
        """Get a single schematic symbol by reference."""
        try:
            schematic = _load_schematic(schematic_path)
            symbol = schematic.get_symbol(reference)
            if symbol is None:
                return {
                    "success": False,
                    "schematic_path": schematic_path,
                    "error": f"Symbol not found: {reference}",
                }
            return {"success": True, "schematic_path": schematic_path, "symbol": symbol}
        except Exception as exc:
            return {"success": False, "schematic_path": schematic_path, "error": str(exc)}

    @mcp.tool()
    def schematic_find_overlaps(schematic_path: str) -> dict[str, Any]:
        """Detect obvious label and property overlaps."""
        try:
            schematic = _load_schematic(schematic_path)
            overlaps = schematic.find_overlaps()
            return {
                "success": True,
                "schematic_path": schematic_path,
                "overlaps": overlaps,
            }
        except Exception as exc:
            return {"success": False, "schematic_path": schematic_path, "error": str(exc)}

    @mcp.tool()
    def schematic_get_sheet_bounds(schematic_path: str) -> dict[str, Any]:
        """Return sheet bounds for a schematic."""
        try:
            schematic = _load_schematic(schematic_path)
            return {
                "success": True,
                "schematic_path": schematic_path,
                "bounds": schematic.get_sheet_bounds(),
            }
        except Exception as exc:
            return {"success": False, "schematic_path": schematic_path, "error": str(exc)}

    @mcp.tool()
    async def schematic_move_symbol(
        schematic_path: str,
        reference: str,
        x: float,
        y: float,
        angle: float | None = None,
        allow_connectivity_change: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Move a symbol, optionally allowing connectivity-affecting changes."""
        if ctx:
            await ctx.info(f"Moving symbol {reference}")
        connectivity_risk = _get_symbol_connectivity_risk(schematic_path, reference)
        if not allow_connectivity_change and connectivity_risk["attached"]:
            return _connectivity_refusal(
                schematic_path,
                f"Refused: moving {reference} alone may disconnect wires or pins. "
                "Use allow_connectivity_change=True only if you intentionally accept that connectivity may change. "
                "Consider moving connected wires together or keeping this edit to symbol properties for now.",
                connectivity_risk,
            )
        return _transactional_edit(
            schematic_path,
            lambda schematic: {"symbol": schematic.move_symbol(reference, x, y, angle)},
            ctx=ctx,
        )

    @mcp.tool()
    async def schematic_move_label(
        schematic_path: str,
        label_uuid: str,
        x: float,
        y: float,
        angle: float | None = None,
        allow_connectivity_change: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Move an electrical label, optionally allowing connectivity-affecting changes."""
        if ctx:
            await ctx.info(f"Moving label {label_uuid}")
        connectivity_risk = _get_label_connectivity_risk(schematic_path, label_uuid)
        if not allow_connectivity_change and connectivity_risk["attached"]:
            return _connectivity_refusal(
                schematic_path,
                f"Refused: moving label {label_uuid} may disconnect a wire or pin. "
                "Use allow_connectivity_change=True only if you intentionally accept that connectivity may change. "
                "Consider moving connected wires together or keeping this edit to non-connectivity-affecting properties.",
                connectivity_risk,
            )
        return _transactional_edit(
            schematic_path,
            lambda schematic: {"label": schematic.move_label(label_uuid, x, y, angle)},
            ctx=ctx,
        )

    @mcp.tool()
    async def schematic_move_symbol_with_connections(
        schematic_path: str,
        reference: str,
        x: float,
        y: float,
        angle: float | None = None,
        preserve_connectivity: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Move a symbol while preserving clearly attached local connectivity."""
        if not preserve_connectivity:
            return {
                "success": False,
                "schematic_path": schematic_path,
                "error": "This tool always preserves connectivity; use schematic_move_symbol for non-preserving moves.",
            }
        if ctx:
            await ctx.info(f"Moving symbol {reference} with connected wires")
        before_snapshot = _load_schematic(schematic_path).target_connectivity_snapshot("symbol", reference)
        return _transactional_edit(
            schematic_path,
            lambda schematic: schematic.move_symbol_with_connections(reference, x, y, angle),
            ctx=ctx,
            post_write_validator=_build_connectivity_validator("symbol", reference, before_snapshot),
        )

    @mcp.tool()
    async def schematic_move_label_with_wire(
        schematic_path: str,
        label_uuid: str,
        x: float,
        y: float,
        angle: float | None = None,
        preserve_connectivity: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Move a label with any clearly attached wire endpoints."""
        if not preserve_connectivity:
            return {
                "success": False,
                "schematic_path": schematic_path,
                "error": "This tool always preserves connectivity; use schematic_move_label for non-preserving moves.",
            }
        if ctx:
            await ctx.info(f"Moving label {label_uuid} with connected wire")
        before_snapshot = _load_schematic(schematic_path).target_connectivity_snapshot("label", label_uuid)
        return _transactional_edit(
            schematic_path,
            lambda schematic: schematic.move_label_with_wire(label_uuid, x, y, angle),
            ctx=ctx,
            post_write_validator=_build_connectivity_validator("label", label_uuid, before_snapshot),
        )

    @mcp.tool()
    def schematic_preview_connectivity_move(
        schematic_path: str,
        target_type: str,
        target_id: str,
        x: float,
        y: float,
        angle: float | None = None,
    ) -> dict[str, Any]:
        """Preview a connectivity-preserving move without writing the schematic."""
        try:
            schematic = _load_schematic(schematic_path)
            return {
                "success": True,
                "schematic_path": schematic_path,
                "target_type": target_type,
                "target_id": target_id,
                "preview": schematic.preview_connectivity_move(target_type, target_id, x, y, angle),
            }
        except Exception as exc:
            return {"success": False, "schematic_path": schematic_path, "error": str(exc)}

    @mcp.tool()
    async def schematic_move_symbol_property(
        schematic_path: str,
        reference: str,
        property_name: str,
        x: float,
        y: float,
        angle: float | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Move a symbol property safely using transactional editing."""
        if ctx:
            await ctx.info(f"Moving property {property_name} on {reference}")
        return _transactional_edit(
            schematic_path,
            lambda schematic: {
                "property": schematic.move_symbol_property(reference, property_name, x, y, angle)
            },
            ctx=ctx,
        )

    @mcp.tool()
    async def schematic_set_property(
        schematic_path: str,
        reference: str,
        property_name: str,
        value: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Set or create a symbol property safely."""
        if ctx:
            await ctx.info(f"Setting property {property_name} on {reference}")
        return _transactional_edit(
            schematic_path,
            lambda schematic: {"property": schematic.set_property(reference, property_name, value)},
            ctx=ctx,
        )

    @mcp.tool()
    async def schematic_auto_arrange_symbol_properties(
        schematic_path: str,
        reference: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Arrange symbol properties around a symbol safely."""
        if ctx:
            await ctx.info(f"Auto-arranging properties for {reference}")
        return _transactional_edit(
            schematic_path,
            lambda schematic: {
                "symbol": schematic.auto_arrange_symbol_properties(reference)
            },
            ctx=ctx,
        )

    @mcp.tool()
    async def schematic_auto_arrange_labels(
        schematic_path: str,
        allow_connectivity_change: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Reposition overlapping labels, optionally allowing connectivity-affecting changes."""
        if ctx:
            await ctx.info("Auto-arranging overlapping labels")
        schematic = _load_schematic(schematic_path)
        connectivity_risks = schematic.auto_arrange_label_risks()
        if not allow_connectivity_change and connectivity_risks:
            return _connectivity_refusal(
                schematic_path,
                "Refused: auto-arranging electrical labels may change connectivity. "
                "Use allow_connectivity_change=True or wait for a connectivity-preserving arrange tool.",
                {"labels": connectivity_risks},
            )
        return _transactional_edit(
            schematic_path,
            lambda schematic: {"labels": schematic.auto_arrange_labels()},
            ctx=ctx,
        )

    @mcp.tool()
    async def export_schematic_svg(
        schematic_path: str,
        output_path: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Export a schematic SVG preview using KiCad CLI."""
        try:
            validated_path = validate_local_path(schematic_path, "schematic", must_exist=True)
            if ctx:
                await ctx.report_progress(10, 100)
                await ctx.info(f"Exporting schematic SVG for {os.path.basename(validated_path)}")
            export_result = _export_schematic_svg(validated_path, output_path)
            if ctx:
                await ctx.report_progress(100, 100)
            return export_result
        except Exception as exc:
            logger.exception("Failed to export schematic SVG")
            return {"success": False, "schematic_path": schematic_path, "error": str(exc)}

    @mcp.tool()
    async def export_schematic_preview(project_path: str, ctx: Context | None = None) -> dict[str, Any]:
        """Export and return a schematic preview for a KiCad project."""
        files = get_project_files(project_path)
        if "schematic" not in files:
            return {"success": False, "project_path": project_path, "error": "No schematic file found in project"}

        if ctx:
            await ctx.report_progress(10, 100)
            await ctx.info(f"Exporting schematic preview for {os.path.basename(files['schematic'])}")
        export_result = _export_schematic_svg(files["schematic"], None)
        if not export_result.get("success"):
            export_result["project_path"] = project_path
            return export_result
        if ctx:
            await ctx.report_progress(100, 100)

        return {
            "success": True,
            "project_path": project_path,
            "schematic_path": files["schematic"],
            "svg_path": export_result["svg_path"],
            "preview": export_result["preview"],
        }


def _load_schematic(schematic_path: str) -> KiCadSchematic:
    validated_path = validate_local_path(schematic_path, "schematic", must_exist=True)
    return KiCadSchematic.from_file(validated_path)


def _get_symbol_connectivity_risk(schematic_path: str, reference: str) -> dict[str, Any]:
    schematic = _load_schematic(schematic_path)
    return schematic.symbol_connectivity_risk(reference)


def _get_label_connectivity_risk(schematic_path: str, label_uuid: str) -> dict[str, Any]:
    schematic = _load_schematic(schematic_path)
    return schematic.label_connectivity_risk(label_uuid)


def _connectivity_refusal(schematic_path: str, message: str, risk: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": False,
        "schematic_path": schematic_path,
        "error": message,
        "connectivity_risk": risk,
    }


def _transactional_edit(
    schematic_path: str,
    mutator: Any,
    *,
    ctx: Context | None = None,
    post_write_validator: Any | None = None,
) -> dict[str, Any]:
    if ctx:
        logger.info("Running transactional schematic edit for %s", schematic_path)
    result = apply_transactional_schematic_edit(
        schematic_path,
        mutator,
        run_cli_validation=True,
        post_write_validator=post_write_validator,
    )
    return result


def _build_connectivity_validator(
    target_type: str,
    target_id: str,
    before_snapshot: dict[str, Any],
) -> Any:
    def validator(schematic_path: str) -> dict[str, Any]:
        after_snapshot = _load_schematic(schematic_path).target_connectivity_snapshot(target_type, target_id)
        comparison = compare_connectivity_snapshots(target_type, before_snapshot, after_snapshot)
        return {
            "success": comparison["preserved"],
            "reason": comparison["reason"],
            "connectivity_snapshot": "preserved" if comparison["preserved"] else "changed",
            "before": before_snapshot,
            "after": after_snapshot,
        }

    return validator


def _export_schematic_svg(schematic_path: str, output_path: str | None) -> dict[str, Any]:
    validation = validate_schematic_file_safely(schematic_path)
    if not validation["success"]:
        return validation

    cli_validation = validate_schematic_with_cli_export(schematic_path)
    if not cli_validation["success"]:
        return {
            "success": False,
            "schematic_path": schematic_path,
            "error": cli_validation.get("stderr") or "KiCad CLI export validation failed",
        }

    schematic_dir = os.path.dirname(schematic_path) or schematic_path
    if output_path is None:
        output_path = os.path.join(schematic_dir, f"{Path(schematic_path).stem}_schematic.svg")
    output_path = os.path.realpath(os.path.expanduser(output_path))

    output_dir_name = os.path.dirname(output_path)
    output_dir = output_dir_name if output_dir_name else schematic_dir
    validator = PathValidator(trusted_roots={schematic_dir, output_dir})
    runner = SecureSubprocessRunner(path_validator=validator)
    result = runner.run_kicad_command(
        ["sch", "export", "svg", schematic_path, "-o", output_path],
        input_files=[schematic_path],
        output_files=[output_path],
        working_dir=schematic_dir,
    )
    if result.returncode != 0:
        return {
            "success": False,
            "schematic_path": schematic_path,
            "svg_path": output_path,
            "error": result.stderr or result.stdout or "KiCad CLI export failed",
        }

    preview_bytes = Path(output_path).read_bytes()
    return {
        "success": True,
        "schematic_path": schematic_path,
        "svg_path": output_path,
        "preview": Image(data=preview_bytes, format="svg"),
    }
