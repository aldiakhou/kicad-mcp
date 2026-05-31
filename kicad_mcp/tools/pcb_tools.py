"""PCB authoring MCP tools."""

from typing import Any, cast

from fastmcp import Context, FastMCP

import kicad_mcp.tools.creation_tools as ct
from kicad_mcp.utils.kicad_pcb_s_expr import KiCadPcb
from kicad_mcp.utils.kicad_s_expr import KiCadSchematic
from kicad_mcp.utils.library_resolver import KiCadLibraryError


def register_pcb_tools(mcp: FastMCP) -> None:
    """Register PCB creation, sync, placement, and routing tools."""

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
            resolved = ct.resolve_footprint_node(footprint_id)
        except KiCadLibraryError as exc:
            return {
                "success": False,
                "pcb_path": pcb_path,
                "footprint_id": footprint_id,
                "error": str(exc),
            }
        return ct._apply_transactional_pcb_edit(
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
        return ct._apply_transactional_pcb_edit(
            pcb_path,
            lambda pcb: {"footprint": pcb.move_footprint(reference, x, y, angle)},
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
        return ct._apply_transactional_pcb_edit(
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
        return ct._apply_transactional_pcb_edit(
            pcb_path,
            lambda pcb: {"track": pcb.add_track(net_name, points, layer, width_mm)},
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
        return ct._apply_transactional_pcb_edit(
            pcb_path,
            lambda pcb: {"via": pcb.add_via(net_name, x, y, drill_mm, diameter_mm)},
        )

    @mcp.tool()
    async def pcb_generate_basic_layout(
        project_path: str,
        placement_style: str = "grid",
        board_width_mm: float = 100.0,
        board_height_mm: float = 80.0,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Generate a conservative board outline plus footprint placement."""
        if placement_style != "grid":
            return {
                "success": False,
                "project_path": project_path,
                "error": "Only placement_style='grid' is supported",
            }
        if ctx:
            await ctx.info("Generating basic PCB layout")
        try:
            files = ct.get_project_files(
                ct.validate_local_path(project_path, "project", must_exist=True)
            )
            if "pcb" not in files:
                created = ct._create_pcb_file(
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
                    resolved.append((symbol, ct.resolve_footprint_node(symbol["footprint"])))
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

            return ct._apply_transactional_pcb_edit(files["pcb"], mutate)
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
        return ct._pcb_sync_from_schematic(
            project_path,
            board_width_mm,
            board_height_mm,
            placement_style,
            preserve_existing_placement,
        )

    @mcp.tool()
    def pcb_complete_from_schematic(
        project_path: str,
        board_width_mm: float = 100.0,
        board_height_mm: float = 80.0,
        placement_style: str = "functional",
        preserve_existing_placement: bool = True,
        place_pcb: bool = True,
        placement_rules: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Sync PCB from schematic, optionally place footprints, and report routing status."""
        return ct._complete_pcb_from_schematic(
            project_path,
            board_width_mm,
            board_height_mm,
            placement_style,
            preserve_existing_placement,
            place_pcb,
            placement_rules,
        )

    @mcp.tool()
    async def pcb_sync_place_and_report(
        project_path: str,
        board_width_mm: float = 100.0,
        board_height_mm: float = 80.0,
        placement_style: str = "functional",
        placement_rules: dict[str, Any] | None = None,
        run_drc: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Sync PCB from schematic, apply initial placement, and report quality."""
        if ctx:
            await ctx.info("Synchronizing PCB from schematic and building placement report")
        return await ct._pcb_sync_place_and_report(
            project_path,
            board_width_mm,
            board_height_mm,
            placement_style,
            placement_rules,
            run_drc,
        )

    @mcp.tool()
    async def pcb_apply_functional_placement(
        project_path: str,
        board_width_mm: float,
        board_height_mm: float,
        placement_rules: dict[str, Any] | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Apply a functional, overlap-aware initial placement to existing PCB footprints."""
        if ctx:
            await ctx.info("Applying functional PCB placement")
        files = ct.get_project_files(
            ct.validate_local_path(project_path, "project", must_exist=True)
        )
        if "pcb" not in files:
            return {"success": False, "project_path": project_path, "error": "PCB file not found"}
        return ct._apply_transactional_pcb_edit(
            files["pcb"],
            lambda pcb: ct._apply_functional_placement(
                pcb,
                board_width_mm,
                board_height_mm,
                placement_rules,
            ),
        )

    @mcp.tool()
    def pcb_get_ratsnest(project_path: str) -> dict[str, Any]:
        """Expose unrouted pad-to-pad endpoints from current PCB pad net assignments."""
        try:
            files = ct.get_project_files(
                ct.validate_local_path(project_path, "project", must_exist=True)
            )
            if "pcb" not in files:
                return {
                    "success": False,
                    "project_path": project_path,
                    "error": "PCB file not found",
                }
            pcb = KiCadPcb.from_file(files["pcb"])
            return ct._build_ratsnest(project_path, files["pcb"], pcb)
        except Exception as exc:
            return {"success": False, "project_path": project_path, "error": str(exc)}

    @mcp.tool()
    def pcb_quality_report(project_path: str) -> dict[str, Any]:
        """Summarize PCB sync, placement, routing, and ratsnest status."""
        try:
            files = ct.get_project_files(
                ct.validate_local_path(project_path, "project", must_exist=True)
            )
            if "pcb" not in files:
                return {
                    "success": False,
                    "project_path": project_path,
                    "error": "PCB file not found",
                }
            pcb = KiCadPcb.from_file(files["pcb"])
            return ct._pcb_quality_report(project_path, files["pcb"], pcb)
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
        """Advanced coordinate routing tool. Prefer pcb_route_between_pads for normal routing."""
        if ctx:
            await ctx.info(f"Routing {net_name} with Manhattan segments")
        return ct._apply_transactional_pcb_edit(
            pcb_path,
            lambda pcb: {
                "route": pcb.add_track(
                    net_name,
                    ct._manhattan_points(waypoints),
                    layer,
                    width_mm,
                )
            },
        )

    @mcp.tool()
    async def pcb_route_between_pads(
        pcb_path: str,
        from_ref: str,
        from_pad: str,
        to_ref: str,
        to_pad: str,
        net_name: str | None = None,
        layer: str = "F.Cu",
        width_mm: float = 0.25,
        strategy: str = "manhattan",
        clearance_mm: float = 0.25,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Route a PCB connection by footprint reference and pad number."""
        if ctx:
            await ctx.info(f"Routing {from_ref}.{from_pad} to {to_ref}.{to_pad}")
        return ct._apply_transactional_pcb_edit(
            pcb_path,
            lambda pcb: {
                "route": ct._route_between_pads(
                    pcb,
                    from_ref,
                    from_pad,
                    to_ref,
                    to_pad,
                    net_name,
                    layer,
                    width_mm,
                    strategy,
                    clearance_mm,
                )
            },
            run_cli_validation=True,
        )

    @mcp.tool()
    async def pcb_route_ratsnest_connection(
        project_path: str,
        connection_index: int,
        layer: str = "F.Cu",
        width_mm: float = 0.25,
        strategy: str = "manhattan",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Route one geometric pad-ratsnest connection by index."""
        if ctx:
            await ctx.info(f"Routing ratsnest connection {connection_index}")
        files = ct.get_project_files(
            ct.validate_local_path(project_path, "project", must_exist=True)
        )
        if "pcb" not in files:
            return {"success": False, "project_path": project_path, "error": "PCB file not found"}
        pcb = KiCadPcb.from_file(files["pcb"])
        ratsnest = ct._build_ratsnest(project_path, files["pcb"], pcb)
        connections = ratsnest.get("connections", [])
        if connection_index < 0 or connection_index >= len(connections):
            return {
                "success": False,
                "project_path": project_path,
                "error": "connection_index is outside the ratsnest connection list",
                "connection_count": len(connections),
            }
        connection = connections[connection_index]
        return ct._apply_transactional_pcb_edit(
            files["pcb"],
            lambda model: {
                "route": ct._route_between_pads(
                    model,
                    connection["from"]["reference"],
                    connection["from"]["pad"],
                    connection["to"]["reference"],
                    connection["to"]["pad"],
                    connection.get("net_name"),
                    layer,
                    width_mm,
                    strategy,
                    0.25,
                )
            },
            run_cli_validation=True,
        )
