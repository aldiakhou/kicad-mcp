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
from kicad_mcp.utils.native_netlist import export_native_netlist
from kicad_mcp.utils.schematic_builder import (
    add_no_connect_marker,
    apply_connection_plan,
    build_schematic_from_spec,
    card_reader_v1_spec,
    preview_build_from_spec,
)
from kicad_mcp.utils.schematic_builder import (
    schematic_quality_report as build_quality_report,
)
from kicad_mcp.utils.schematic_pins import (
    attach_net_to_pin,
    get_symbol_pin_map,
    verify_native_net_membership,
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
        return _create_kicad_project(project_dir, project_name, create_schematic, create_pcb, paper)

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
    def create_card_reader_reference_project(
        project_dir: str, project_name: str = "Card_Reader_clean"
    ) -> dict[str, Any]:
        """Create a clean card-reader reference project using the built-in v1 spec."""
        project = _create_kicad_project(project_dir, project_name, True, False, "A3")
        if not project.get("success"):
            return project
        build = build_schematic_from_spec(
            project["project_path"],
            card_reader_v1_spec(),
            mode="replace",
            run_erc=True,
        )
        return {
            "success": bool(build.get("success")),
            "project_path": project["project_path"],
            "project_dir": project["project_dir"],
            "created_files": project["created_files"],
            "build": build,
        }

    @mcp.tool()
    def schematic_preview_build_from_spec(project_path: str, spec: dict[str, Any]) -> dict[str, Any]:
        """Preview a spec-driven schematic build without writing files."""
        return preview_build_from_spec(project_path, spec)

    @mcp.tool()
    def schematic_build_from_spec(
        project_path: str,
        spec: dict[str, Any],
        mode: str = "replace",
        backup: bool = True,
        run_erc: bool = True,
    ) -> dict[str, Any]:
        """Build a schematic from a structured specification."""
        if not backup:
            return {
                "success": False,
                "project_path": project_path,
                "error": "backup=False is not supported; schematic builds are always backed up",
            }
        return build_schematic_from_spec(project_path, spec, mode=mode, run_erc=run_erc)

    @mcp.tool()
    def schematic_quality_report(project_path: str, run_erc: bool = True) -> dict[str, Any]:
        """Summarize schematic ERC, netlist, footprint, page-bound, and grid quality."""
        try:
            return build_quality_report(project_path, run_erc=run_erc)
        except Exception as exc:
            return {"success": False, "project_path": project_path, "error": str(exc)}

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
    def schematic_get_pin_map(schematic_path: str, reference: str) -> dict[str, Any]:
        """Return transformed pin positions for a placed schematic symbol."""
        return get_symbol_pin_map(schematic_path, reference)

    @mcp.tool()
    async def schematic_attach_net_to_pin(
        schematic_path: str,
        reference: str,
        pin: str,
        net_name: str,
        label_type: str = "global",
        stub_length_mm: float = 5.08,
        allow_hidden_power: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Attach a net label to an actual symbol pin coordinate and verify it natively."""
        if ctx:
            await ctx.info(f"Attaching {net_name} to {reference}.{pin}")
        return _apply_transactional_schematic_authoring(
            schematic_path,
            lambda schematic: {
                "attachment": attach_net_to_pin(
                    schematic,
                    schematic_path,
                    reference,
                    pin,
                    net_name,
                    label_type,
                    stub_length_mm,
                    allow_hidden_power,
                )
            },
            post_write_validator=lambda path: verify_native_net_membership(
                path, reference, pin, net_name
            ),
        )

    @mcp.tool()
    async def schematic_apply_connection_plan(
        schematic_path: str,
        connections: list[dict[str, Any]],
        run_native_netlist: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Apply multiple pin-net attachments and verify them as one transaction."""
        if ctx:
            await ctx.info(f"Applying {len(connections)} schematic connections")
        return apply_connection_plan(schematic_path, connections, run_native_netlist)

    @mcp.tool()
    async def schematic_add_no_connect(
        schematic_path: str,
        reference: str,
        pin: str,
        allow_hidden_power: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add a no-connect marker at an actual symbol pin coordinate."""
        if ctx:
            await ctx.info(f"Adding no-connect marker to {reference}.{pin}")
        return add_no_connect_marker(schematic_path, reference, pin, allow_hidden_power)

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

    @mcp.tool()
    async def pcb_sync_from_schematic(
        project_path: str,
        board_width_mm: float = 100.0,
        board_height_mm: float = 80.0,
        placement_style: str = "functional",
        preserve_existing_placement: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Synchronize PCB footprints and pad nets from KiCad's native schematic netlist."""
        if ctx:
            await ctx.info("Synchronizing PCB from schematic netlist")
        return _pcb_sync_from_schematic(
            project_path,
            board_width_mm,
            board_height_mm,
            placement_style,
            preserve_existing_placement,
        )

    @mcp.tool()
    async def pcb_apply_functional_placement(
        project_path: str,
        board_width_mm: float,
        board_height_mm: float,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Apply a functional, overlap-aware initial placement to existing PCB footprints."""
        if ctx:
            await ctx.info("Applying functional PCB placement")
        files = get_project_files(validate_local_path(project_path, "project", must_exist=True))
        if "pcb" not in files:
            return {"success": False, "project_path": project_path, "error": "PCB file not found"}
        return _apply_transactional_pcb_edit(
            files["pcb"],
            lambda pcb: _apply_functional_placement(pcb, board_width_mm, board_height_mm),
        )

    @mcp.tool()
    def pcb_get_ratsnest(project_path: str) -> dict[str, Any]:
        """Expose unrouted pad-to-pad endpoints from current PCB pad net assignments."""
        try:
            files = get_project_files(validate_local_path(project_path, "project", must_exist=True))
            if "pcb" not in files:
                return {"success": False, "project_path": project_path, "error": "PCB file not found"}
            pcb = KiCadPcb.from_file(files["pcb"])
            return _build_ratsnest(project_path, files["pcb"], pcb)
        except Exception as exc:
            return {"success": False, "project_path": project_path, "error": str(exc)}

    @mcp.tool()
    async def pcb_route_net_manhattan(
        pcb_path: str,
        net_name: str,
        waypoints: list[dict[str, float]],
        layer: str = "F.Cu",
        width_mm: float = 0.25,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Route a net with explicit Manhattan segments through the provided waypoints."""
        if ctx:
            await ctx.info(f"Routing {net_name} with Manhattan segments")
        return _apply_transactional_pcb_edit(
            pcb_path,
            lambda pcb: {
                "route": pcb.add_track(
                    net_name,
                    _manhattan_points(waypoints),
                    layer,
                    width_mm,
                )
            },
        )


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


def _create_kicad_project(
    project_dir: str,
    project_name: str,
    create_schematic: bool = True,
    create_pcb: bool = True,
    paper: str = "A4",
) -> dict[str, Any]:
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
            schematic_result = _create_schematic_file(str(project_path), overwrite=False, paper=paper)
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


def _pcb_sync_from_schematic(
    project_path: str,
    board_width_mm: float,
    board_height_mm: float,
    placement_style: str,
    preserve_existing_placement: bool,
) -> dict[str, Any]:
    if placement_style not in {"functional", "grid"}:
        return {
            "success": False,
            "project_path": project_path,
            "error": "placement_style must be one of: functional, grid",
        }
    try:
        validated_project = validate_local_path(project_path, "project", must_exist=True)
        files = get_project_files(validated_project)
        if "schematic" not in files:
            return {"success": False, "project_path": validated_project, "error": "No schematic file found"}
        if "pcb" not in files:
            created = _create_pcb_file(
                validated_project,
                overwrite=False,
                board_width_mm=board_width_mm,
                board_height_mm=board_height_mm,
            )
            if not created["success"]:
                return created
            files["pcb"] = created["pcb_path"]
        native = export_native_netlist(files["schematic"])
        if not native.get("success"):
            return {
                "success": False,
                "project_path": validated_project,
                "schematic_path": files["schematic"],
                "error": native.get("error", "Native netlist export failed"),
                "native_netlist": native,
            }
        components = native.get("components", {})
        footprint_refs = {
            ref: component
            for ref, component in components.items()
            if component.get("footprint")
        }
        resolved_footprints: dict[str, dict[str, Any]] = {}
        missing_footprints = []
        for ref, component in footprint_refs.items():
            try:
                resolved_footprints[ref] = resolve_footprint_node(component["footprint"])
            except KiCadLibraryError as exc:
                missing_footprints.append(
                    {"reference": ref, "footprint": component.get("footprint"), "error": str(exc)}
                )
        assignments = _net_assignments_by_ref(native)

        def mutate(pcb: KiCadPcb) -> dict[str, Any]:
            outline = pcb.create_board_outline(board_width_mm, board_height_mm)
            existing_refs = {
                item["reference"] for item in pcb.list_footprints() if item.get("reference")
            }
            placed = []
            updated = []
            missing_pads = []
            for net_name in native.get("nets", {}):
                pcb.ensure_net(net_name)
            for index, (ref, component) in enumerate(footprint_refs.items()):
                if ref not in resolved_footprints:
                    continue
                if ref not in existing_refs:
                    x, y, angle = _initial_component_position(
                        ref, component, index, board_width_mm, board_height_mm, placement_style
                    )
                    placed.append(
                        pcb.add_footprint(
                            component["footprint"],
                            cast(Any, resolved_footprints[ref]["node"]),
                            ref,
                            component.get("value", ""),
                            x,
                            y,
                            angle,
                        )
                    )
                elif not preserve_existing_placement:
                    x, y, angle = _initial_component_position(
                        ref, component, index, board_width_mm, board_height_mm, placement_style
                    )
                    updated.append(pcb.move_footprint(ref, x, y, angle))
                pad_result = pcb.assign_footprint_pad_nets(ref, assignments.get(ref, {}))
                missing_pads.extend(
                    {"reference": ref, "pad": pad, "net": assignments.get(ref, {}).get(pad)}
                    for pad in pad_result["missing_pads"]
                )
            stale = sorted(existing_refs - set(footprint_refs))
            return {
                "outline": outline,
                "placed_footprints": placed,
                "moved_footprints": updated,
                "synced_footprints": sorted(set(footprint_refs) - {item["reference"] for item in missing_footprints}),
                "synced_net_count": len(native.get("nets", {})),
                "synced_pad_count": sum(len(item) for item in assignments.values()),
                "missing_footprints": missing_footprints,
                "missing_pads": missing_pads,
                "stale_footprints": stale,
                "unconnected_pins": [],
            }

        result = _apply_transactional_pcb_edit(files["pcb"], mutate)
        if result.get("success"):
            result["project_path"] = validated_project
            result["schematic_path"] = files["schematic"]
            result["native_netlist"] = {
                "component_count": native.get("component_count", 0),
                "net_count": native.get("net_count", 0),
                "connectivity_complete": native.get("connectivity_complete", False),
            }
        return result
    except Exception as exc:
        return {"success": False, "project_path": project_path, "error": str(exc)}


def _net_assignments_by_ref(native_netlist: dict[str, Any]) -> dict[str, dict[str, str]]:
    assignments: dict[str, dict[str, str]] = {}
    for net_name, net in native_netlist.get("nets", {}).items():
        for node in net.get("nodes", []):
            ref = node.get("ref")
            pin = node.get("pin")
            if ref and pin:
                assignments.setdefault(ref, {})[pin] = net_name
    return assignments


def _initial_component_position(
    reference: str,
    component: dict[str, Any],
    index: int,
    board_width_mm: float,
    board_height_mm: float,
    placement_style: str,
) -> tuple[float, float, float]:
    if placement_style == "grid":
        columns = max(1, int(board_width_mm // 20))
        return 10.0 + (index % columns) * 20.0, 10.0 + (index // columns) * 20.0, 0.0
    text = f"{reference} {component.get('value', '')} {component.get('footprint', '')}".lower()
    if "usb" in text:
        return 8.0, max(12.0, board_height_mm * 0.25), 90.0
    if reference.startswith("U") and ("esp" in text or "mcu" in text):
        return board_width_mm * 0.45, 18.0, 0.0
    if "lcd" in text or "display" in text or "nhd" in text:
        return board_width_mm * 0.58, board_height_mm * 0.62, 0.0
    if reference.startswith("J"):
        return board_width_mm * 0.15, board_height_mm * 0.55 + index * 4.0, 0.0
    if reference.startswith(("SW", "S")):
        return board_width_mm * 0.25 + index * 8.0, board_height_mm - 12.0, 0.0
    if reference.startswith(("R", "C", "D")):
        return board_width_mm * 0.30 + (index % 8) * 10.0, board_height_mm * 0.25 + (index // 8) * 8.0, 0.0
    return board_width_mm * 0.5 + (index % 5) * 12.0, board_height_mm * 0.45 + (index // 5) * 10.0, 0.0


def _apply_functional_placement(
    pcb: KiCadPcb, board_width_mm: float, board_height_mm: float
) -> dict[str, Any]:
    outline = pcb.create_board_outline(board_width_mm, board_height_mm)
    moved = []
    occupied: list[dict[str, float]] = []
    overlap_warnings = []
    for index, footprint in enumerate(pcb.list_footprints()):
        ref = footprint.get("reference") or f"FP{index}"
        x, y, angle = _initial_component_position(
            ref,
            {
                "value": footprint.get("value", ""),
                "footprint": footprint.get("footprint_name", ""),
            },
            index,
            board_width_mm,
            board_height_mm,
            "functional",
        )
        for _attempt in range(25):
            pcb.move_footprint(ref, x, y, angle)
            node = pcb.find_footprint(ref)
            bounds = pcb.footprint_bounds(cast(Any, node)) if node is not None else {}
            if not any(_bounds_intersect(bounds, other, padding=1.0) for other in occupied):
                occupied.append(bounds)
                break
            x += 8.0
            if x > board_width_mm - 8.0:
                x = 10.0
                y += 8.0
        else:
            overlap_warnings.append({"reference": ref, "warning": "Could not find non-overlapping placement"})
        moved.append({"reference": ref, "position": {"x": x, "y": y, "angle": angle}})
    keepout_warnings = _esp_antenna_keepout_warnings(pcb)
    return {
        "outline": outline,
        "moved_footprints": moved,
        "overlap_warnings": overlap_warnings,
        "keepout_warnings": keepout_warnings,
    }


def _bounds_intersect(a: dict[str, float], b: dict[str, float], padding: float = 0.0) -> bool:
    if not a or not b:
        return False
    return not (
        a["right"] + padding < b["left"]
        or a["left"] - padding > b["right"]
        or a["bottom"] + padding < b["top"]
        or a["top"] - padding > b["bottom"]
    )


def _esp_antenna_keepout_warnings(pcb: KiCadPcb) -> list[dict[str, str]]:
    warnings = []
    footprints = pcb.list_footprints()
    for footprint in footprints:
        name = f"{footprint.get('reference', '')} {footprint.get('footprint_name', '')}".lower()
        if "esp" not in name:
            continue
        bounds = footprint.get("bounds", {})
        antenna_keepout = {
            "left": bounds.get("left", 0.0),
            "right": bounds.get("right", 0.0),
            "top": bounds.get("top", 0.0),
            "bottom": bounds.get("top", 0.0) + 8.0,
        }
        for other in footprints:
            if other.get("reference") == footprint.get("reference"):
                continue
            if _bounds_intersect(antenna_keepout, other.get("bounds", {}), padding=1.0):
                warnings.append(
                    {
                        "reference": footprint.get("reference", ""),
                        "warning": f"Antenna keepout may overlap {other.get('reference', '')}",
                    }
                )
    return warnings


def _build_ratsnest(project_path: str, pcb_path: str, pcb: KiCadPcb) -> dict[str, Any]:
    pads_by_net: dict[str, list[dict[str, Any]]] = {}
    for pad in pcb.footprint_pad_positions():
        if pad.get("net_name"):
            pads_by_net.setdefault(pad["net_name"], []).append(pad)
    connections = []
    for net_name, pads in sorted(pads_by_net.items()):
        if len(pads) < 2:
            continue
        anchor = pads[0]
        for pad in pads[1:]:
            connections.append(
                {
                    "net_name": net_name,
                    "from": {
                        "reference": anchor["reference"],
                        "pad": anchor["pad"],
                        "position": anchor["position"],
                    },
                    "to": {
                        "reference": pad["reference"],
                        "pad": pad["pad"],
                        "position": pad["position"],
                    },
                }
            )
    return {
        "success": True,
        "project_path": project_path,
        "pcb_path": pcb_path,
        "net_count": len(pads_by_net),
        "connection_count": len(connections),
        "connections": connections,
    }


def _manhattan_points(waypoints: list[dict[str, float]]) -> list[dict[str, float]]:
    if len(waypoints) < 2:
        raise ValueError("At least two waypoints are required")
    points = [{"x": float(waypoints[0]["x"]), "y": float(waypoints[0]["y"])}]
    for raw in waypoints[1:]:
        end = {"x": float(raw["x"]), "y": float(raw["y"])}
        start = points[-1]
        if start["x"] != end["x"] and start["y"] != end["y"]:
            points.append({"x": end["x"], "y": start["y"]})
        points.append(end)
    return points


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
    post_write_validator: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from kicad_mcp.utils.transactional_edit import apply_transactional_schematic_edit

    return apply_transactional_schematic_edit(
        schematic_path,
        mutator,
        run_cli_validation=True,
        post_write_validator=post_write_validator,
    )


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
