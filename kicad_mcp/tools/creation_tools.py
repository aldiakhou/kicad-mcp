"""
Project, schematic creation, library resolution, and conservative PCB authoring tools.
"""

from collections.abc import Callable
import json
from pathlib import Path
from typing import Any, cast

from fastmcp import Context, FastMCP

from kicad_mcp.utils.file_utils import get_project_files
from kicad_mcp.utils.kicad_pcb_s_expr import KiCadPcb, validate_pcb_text
from kicad_mcp.utils.kicad_s_expr import KiCadSchematic, validate_schematic_text
from kicad_mcp.utils.library_resolver import (
    KiCadLibraryError,
)
from kicad_mcp.utils.library_resolver import (
    list_footprint_libraries as resolve_footprint_libraries,
)
from kicad_mcp.utils.library_resolver import (
    list_symbol_libraries as resolve_symbol_libraries,
)
from kicad_mcp.utils.library_resolver import (
    resolve_footprint as resolve_footprint_node,
)
from kicad_mcp.utils.library_resolver import (
    resolve_symbol as resolve_symbol_node,
)
from kicad_mcp.utils.transactional_edit import (
    create_file_backup,
    get_file_diff_against_backup,
    restore_backup_manifest,
    validate_local_directory,
    validate_local_path,
    validate_schematic_with_cli_export,
)


def register_creation_tools(mcp: FastMCP) -> None:
    """Register project creation, schematic authoring, and PCB authoring tools."""

    @mcp.tool()
    def create_kicad_project(
        project_dir: str,
        project_name: str,
        create_schematic: bool = True,
        create_pcb: bool = True,
        paper: str = "A4",
    ) -> dict[str, Any]:
        """Create a new KiCad project and optional schematic/PCB files."""
        try:
            safe_name = _safe_project_name(project_name)
            base_dir = Path(validate_local_directory(project_dir, must_exist=False))
            target_dir = base_dir / safe_name
            target_dir.mkdir(parents=True, exist_ok=True)
            project_path = target_dir / f"{safe_name}.kicad_pro"
            if project_path.exists():
                return {
                    "success": False,
                    "project_path": str(project_path),
                    "error": "Project already exists",
                }
            project_path.write_text(json.dumps(_default_project_json(), indent=2), encoding="utf-8")

            created_files = {"project": str(project_path)}
            schematic_result = None
            pcb_result = None
            if create_schematic:
                schematic_result = _create_schematic_file(
                    str(project_path), overwrite=False, paper=paper
                )
                if not schematic_result["success"]:
                    return schematic_result
                created_files["schematic"] = schematic_result["schematic_path"]
            if create_pcb:
                pcb_result = _create_pcb_file(str(project_path), overwrite=False)
                if not pcb_result["success"]:
                    return pcb_result
                created_files["pcb"] = pcb_result["pcb_path"]

            return {
                "success": True,
                "project_path": str(project_path),
                "project_dir": str(target_dir),
                "created_files": created_files,
                "schematic": schematic_result,
                "pcb": pcb_result,
            }
        except Exception as exc:
            return {
                "success": False,
                "project_dir": project_dir,
                "project_name": project_name,
                "error": str(exc),
            }

    @mcp.tool()
    def create_schematic_file(
        project_path: str, overwrite: bool = False, paper: str = "A4"
    ) -> dict[str, Any]:
        """Create a schematic file for an existing KiCad project."""
        return _create_schematic_file(project_path, overwrite=overwrite, paper=paper)

    @mcp.tool()
    def create_pcb_file(
        project_path: str,
        overwrite: bool = False,
        board_width_mm: float = 100.0,
        board_height_mm: float = 80.0,
    ) -> dict[str, Any]:
        """Create a PCB file for an existing KiCad project."""
        return _create_pcb_file(
            project_path,
            overwrite=overwrite,
            board_width_mm=board_width_mm,
            board_height_mm=board_height_mm,
        )

    @mcp.tool()
    def list_symbol_libraries(query: str | None = None) -> dict[str, Any]:
        """List available KiCad symbol libraries."""
        libraries = resolve_symbol_libraries(query)
        return {"success": True, "query": query, "count": len(libraries), "libraries": libraries}

    @mcp.tool()
    def list_footprint_libraries(query: str | None = None) -> dict[str, Any]:
        """List available KiCad footprint libraries."""
        libraries = resolve_footprint_libraries(query)
        return {"success": True, "query": query, "count": len(libraries), "libraries": libraries}

    @mcp.tool()
    def resolve_symbol(lib_id: str) -> dict[str, Any]:
        """Resolve a KiCad symbol from installed libraries."""
        try:
            result = resolve_symbol_node(lib_id)
            return {key: value for key, value in result.items() if key != "node"}
        except KiCadLibraryError as exc:
            return {"success": False, "lib_id": lib_id, "error": str(exc)}

    @mcp.tool()
    def resolve_footprint(footprint_id: str) -> dict[str, Any]:
        """Resolve a KiCad footprint from installed libraries."""
        try:
            result = resolve_footprint_node(footprint_id)
            return {key: value for key, value in result.items() if key != "node"}
        except KiCadLibraryError as exc:
            return {"success": False, "footprint_id": footprint_id, "error": str(exc)}

    @mcp.tool()
    async def schematic_add_symbol(
        schematic_path: str,
        lib_id: str,
        reference: str,
        value: str,
        x: float,
        y: float,
        angle: float = 0.0,
        footprint: str | None = None,
        properties: dict[str, str] | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add a resolved KiCad library symbol to a schematic."""
        if ctx:
            await ctx.info(f"Adding schematic symbol {reference}")
        try:
            resolved = resolve_symbol_node(lib_id)
        except KiCadLibraryError as exc:
            return {
                "success": False,
                "schematic_path": schematic_path,
                "lib_id": lib_id,
                "error": str(exc),
            }
        return _apply_transactional_schematic_authoring(
            schematic_path,
            lambda schematic: {
                "symbol": schematic.add_symbol(
                    lib_id,
                    reference,
                    value,
                    x,
                    y,
                    angle,
                    footprint,
                    properties,
                    cast(Any, resolved["node"]),
                )
            },
        )

    @mcp.tool()
    async def schematic_add_wire(
        schematic_path: str,
        points: list[dict[str, float]],
        net_name: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add a schematic wire, optionally with a local net label."""
        if ctx:
            await ctx.info("Adding schematic wire")
        return _apply_transactional_schematic_authoring(
            schematic_path,
            lambda schematic: {"wire": schematic.add_wire(points, net_name)},
        )

    @mcp.tool()
    async def schematic_add_label(
        schematic_path: str,
        text: str,
        x: float,
        y: float,
        label_type: str = "local",
        angle: float = 0.0,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add a local, global, or hierarchical schematic label."""
        if ctx:
            await ctx.info(f"Adding label {text}")
        return _apply_transactional_schematic_authoring(
            schematic_path,
            lambda schematic: {"label": schematic.add_label(text, x, y, label_type, angle)},
        )

    @mcp.tool()
    async def schematic_connect_points(
        schematic_path: str,
        start: dict[str, float],
        end: dict[str, float],
        style: str = "orthogonal",
        net_name: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Connect two schematic points with a direct or orthogonal wire."""
        if ctx:
            await ctx.info("Connecting schematic points")
        return _apply_transactional_schematic_authoring(
            schematic_path,
            lambda schematic: {"connection": schematic.connect_points(start, end, style, net_name)},
        )

    @mcp.tool()
    async def schematic_delete_item(
        schematic_path: str,
        item_type: str,
        item_id: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Delete a top-level schematic symbol, wire, or label."""
        if ctx:
            await ctx.info(f"Deleting schematic {item_type} {item_id}")
        return _apply_transactional_schematic_authoring(
            schematic_path,
            lambda schematic: {"deleted": schematic.delete_item(item_type, item_id)},
        )

    @mcp.tool()
    async def pcb_add_footprint(
        pcb_path: str,
        footprint_id: str,
        reference: str,
        value: str,
        x: float,
        y: float,
        angle: float = 0.0,
        net_assignments: dict[str, str] | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add a resolved KiCad library footprint to a PCB."""
        if ctx:
            await ctx.info(f"Adding PCB footprint {reference}")
        try:
            resolved = resolve_footprint_node(footprint_id)
        except KiCadLibraryError as exc:
            return {
                "success": False,
                "pcb_path": pcb_path,
                "footprint_id": footprint_id,
                "error": str(exc),
            }
        return _apply_transactional_pcb_edit(
            pcb_path,
            lambda pcb: {
                "footprint": pcb.add_footprint(
                    footprint_id,
                    cast(Any, resolved["node"]),
                    reference,
                    value,
                    x,
                    y,
                    angle,
                    net_assignments,
                )
            },
        )

    @mcp.tool()
    async def pcb_move_footprint(
        pcb_path: str,
        reference: str,
        x: float,
        y: float,
        angle: float | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Move a PCB footprint by reference."""
        if ctx:
            await ctx.info(f"Moving PCB footprint {reference}")
        return _apply_transactional_pcb_edit(
            pcb_path, lambda pcb: {"footprint": pcb.move_footprint(reference, x, y, angle)}
        )

    @mcp.tool()
    async def pcb_create_board_outline(
        pcb_path: str,
        width_mm: float,
        height_mm: float,
        origin_x: float = 0.0,
        origin_y: float = 0.0,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create or replace a rectangular PCB board outline."""
        if ctx:
            await ctx.info("Creating PCB board outline")
        return _apply_transactional_pcb_edit(
            pcb_path,
            lambda pcb: {
                "outline": pcb.create_board_outline(width_mm, height_mm, origin_x, origin_y)
            },
        )

    @mcp.tool()
    async def pcb_add_track(
        pcb_path: str,
        net_name: str,
        points: list[dict[str, float]],
        layer: str = "F.Cu",
        width_mm: float = 0.25,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add explicit PCB track segments for a net."""
        if ctx:
            await ctx.info(f"Adding PCB track on {net_name}")
        return _apply_transactional_pcb_edit(
            pcb_path, lambda pcb: {"track": pcb.add_track(net_name, points, layer, width_mm)}
        )

    @mcp.tool()
    async def pcb_add_via(
        pcb_path: str,
        net_name: str,
        x: float,
        y: float,
        drill_mm: float = 0.3,
        diameter_mm: float = 0.6,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add a PCB via for a net."""
        if ctx:
            await ctx.info(f"Adding PCB via on {net_name}")
        return _apply_transactional_pcb_edit(
            pcb_path, lambda pcb: {"via": pcb.add_via(net_name, x, y, drill_mm, diameter_mm)}
        )

    @mcp.tool()
    async def pcb_generate_basic_layout(
        project_path: str,
        placement_style: str = "grid",
        board_width_mm: float = 100.0,
        board_height_mm: float = 80.0,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Generate a conservative board outline plus footprint placement from schematic footprint properties."""
        if placement_style != "grid":
            return {
                "success": False,
                "project_path": project_path,
                "error": "Only placement_style='grid' is supported",
            }
        if ctx:
            await ctx.info("Generating basic PCB layout")
        try:
            files = get_project_files(validate_local_path(project_path, "project", must_exist=True))
            if "pcb" not in files:
                created = _create_pcb_file(
                    project_path,
                    overwrite=False,
                    board_width_mm=board_width_mm,
                    board_height_mm=board_height_mm,
                )
                if not created["success"]:
                    return created
                files["pcb"] = created["pcb_path"]
            if "schematic" not in files:
                return {
                    "success": False,
                    "project_path": project_path,
                    "error": "No schematic file found",
                }
            schematic = KiCadSchematic.from_file(files["schematic"])
            symbols = [symbol for symbol in schematic.list_symbols() if symbol.get("footprint")]
            resolved = []
            for symbol in symbols:
                try:
                    resolved.append((symbol, resolve_footprint_node(symbol["footprint"])))
                except KiCadLibraryError as exc:
                    return {
                        "success": False,
                        "project_path": project_path,
                        "error": str(exc),
                        "symbol": symbol,
                    }

            def mutate(pcb: KiCadPcb) -> dict[str, Any]:
                outline = pcb.create_board_outline(board_width_mm, board_height_mm)
                placed = []
                columns = max(1, int(board_width_mm // 20))
                for index, (symbol, footprint) in enumerate(resolved):
                    x = 10.0 + (index % columns) * 20.0
                    y = 10.0 + (index // columns) * 20.0
                    if pcb.find_footprint(symbol["reference"]) is None:
                        placed.append(
                            pcb.add_footprint(
                                symbol["footprint"],
                                cast(Any, footprint["node"]),
                                symbol["reference"],
                                symbol["value"],
                                x,
                                y,
                            )
                        )
                return {"outline": outline, "placed_footprints": placed}

            return _apply_transactional_pcb_edit(files["pcb"], mutate)
        except Exception as exc:
            return {"success": False, "project_path": project_path, "error": str(exc)}


def _create_schematic_file(
    project_path: str, overwrite: bool = False, paper: str = "A4"
) -> dict[str, Any]:
    try:
        validated_project = validate_local_path(project_path, "project", must_exist=True)
        schematic_path = _related_path(validated_project, ".kicad_sch")
        if schematic_path.exists() and not overwrite:
            return {
                "success": False,
                "schematic_path": str(schematic_path),
                "error": "Schematic already exists",
            }
        backup = create_file_backup(str(schematic_path)) if schematic_path.exists() else None
        schematic = KiCadSchematic.empty(paper=paper)
        validation = validate_schematic_text(schematic.to_text())
        schematic_path.write_text(schematic.to_text(), encoding="utf-8")
        cli_validation = validate_schematic_with_cli_export(str(schematic_path))
        if not cli_validation["success"]:
            if backup:
                restore_backup_manifest(backup["backup_path"])
            else:
                schematic_path.unlink(missing_ok=True)
            return {
                "success": False,
                "schematic_path": str(schematic_path),
                "error": cli_validation.get("stderr") or "CLI validation failed",
            }
        return {
            "success": True,
            "project_path": validated_project,
            "schematic_path": str(schematic_path),
            "created": backup is None,
            "backup_path": backup["backup_path"] if backup else None,
            "validation": {"syntax": validation, "cli": cli_validation},
        }
    except Exception as exc:
        return {"success": False, "project_path": project_path, "error": str(exc)}


def _create_pcb_file(
    project_path: str,
    overwrite: bool = False,
    board_width_mm: float = 100.0,
    board_height_mm: float = 80.0,
) -> dict[str, Any]:
    try:
        validated_project = validate_local_path(project_path, "project", must_exist=True)
        pcb_path = _related_path(validated_project, ".kicad_pcb")
        if pcb_path.exists() and not overwrite:
            return {"success": False, "pcb_path": str(pcb_path), "error": "PCB already exists"}
        backup = create_file_backup(str(pcb_path)) if pcb_path.exists() else None
        pcb = KiCadPcb.empty(board_width_mm, board_height_mm)
        validation = validate_pcb_text(pcb.to_text())
        pcb_path.write_text(pcb.to_text(), encoding="utf-8")
        return {
            "success": True,
            "project_path": validated_project,
            "pcb_path": str(pcb_path),
            "created": backup is None,
            "backup_path": backup["backup_path"] if backup else None,
            "validation": {"syntax": validation},
        }
    except Exception as exc:
        return {"success": False, "project_path": project_path, "error": str(exc)}


def _apply_transactional_schematic_authoring(
    schematic_path: str,
    mutator: Callable[[KiCadSchematic], dict[str, Any]],
) -> dict[str, Any]:
    from kicad_mcp.utils.transactional_edit import apply_transactional_schematic_edit

    return apply_transactional_schematic_edit(schematic_path, mutator, run_cli_validation=True)


def _apply_transactional_pcb_edit(
    pcb_path: str,
    mutator: Callable[[KiCadPcb], dict[str, Any]],
) -> dict[str, Any]:
    validated_path = validate_local_path(pcb_path, "pcb", must_exist=True)
    original_text = Path(validated_path).read_text(encoding="utf-8")
    backup = create_file_backup(validated_path)
    try:
        before_validation = validate_pcb_text(original_text)
        pcb = KiCadPcb.from_text(original_text)
        change_result = mutator(pcb)
        updated_text = pcb.to_text()
        after_validation = validate_pcb_text(updated_text)
        Path(validated_path).write_text(updated_text, encoding="utf-8")
        diff_result = get_file_diff_against_backup(validated_path, backup["backup_path"])
        return {
            "success": True,
            "pcb_path": validated_path,
            "backup_path": backup["backup_path"],
            "changed_objects": change_result,
            "validation": {
                "before": before_validation,
                "after": after_validation,
                "cli": {
                    "success": True,
                    "skipped": True,
                    "reason": "PCB CLI validation is not required for primitive edits",
                },
            },
            "rolled_back": False,
            "diff": diff_result["diff"],
        }
    except Exception as exc:
        restore_result = restore_backup_manifest(backup["backup_path"])
        return {
            "success": False,
            "pcb_path": validated_path,
            "backup_path": backup["backup_path"],
            "error": str(exc),
            "rolled_back": restore_result.get("success", False),
            "restore_result": restore_result,
        }


def _related_path(project_path: str, extension: str) -> Path:
    project = Path(project_path)
    return project.with_suffix(extension)


def _safe_project_name(project_name: str) -> str:
    safe = project_name.strip().replace("/", "_").replace("\\", "_")
    if not safe or safe in {".", ".."}:
        raise ValueError("project_name must be a non-empty file name")
    if any(char in safe for char in '<>:"|?*'):
        raise ValueError("project_name contains unsupported path characters")
    return safe


def _default_project_json() -> dict[str, Any]:
    return {
        "board": {"design_settings": {"defaults": {}, "rules": {}}, "viewports": []},
        "boards": [],
        "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
        "meta": {"filename": "", "version": 1},
        "net_settings": {
            "classes": [
                {"name": "Default", "track_width": 0.25, "via_diameter": 0.6, "via_drill": 0.3}
            ]
        },
        "schematic": {"annotate_start_num": 0},
    }
