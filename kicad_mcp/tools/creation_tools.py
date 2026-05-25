"""
Project, schematic creation, library resolution, and conservative PCB authoring tools.
"""

from collections.abc import Callable
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, cast

from fastmcp import Context, FastMCP

from kicad_mcp.tools.drc_impl.cli_drc import run_drc_via_cli
from kicad_mcp.tools.export_tools import _generate_pcb_thumbnail_impl
from kicad_mcp.utils.design_intent_compiler import compile_design_intent, design_intent_schema
from kicad_mcp.utils.file_utils import get_project_files
from kicad_mcp.utils.kicad_cli import get_kicad_cli_path
from kicad_mcp.utils.kicad_cli_batch import validate_schematic_batch
from kicad_mcp.utils.kicad_pcb_s_expr import KiCadPcb, validate_pcb_text
from kicad_mcp.utils.kicad_s_expr import (
    KiCadSchematic,
    SExprAtom,
    SExprList,
    validate_schematic_text,
)
from kicad_mcp.utils.library_resolver import (
    KiCadLibraryError,
)
from kicad_mcp.utils.library_resolver import (
    find_footprints as search_footprints,
)
from kicad_mcp.utils.library_resolver import (
    find_symbols as search_symbols,
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
from kicad_mcp.utils.library_resolver import (
    symbol_footprint_suggestions as resolve_symbol_footprint_suggestions,
)
from kicad_mcp.utils.native_netlist import export_native_netlist, run_erc_via_cli
from kicad_mcp.utils.preview_metadata import svg_preview_metadata
from kicad_mcp.utils.schematic_builder import (
    add_no_connect_marker,
    apply_connection_plan,
    build_schematic_from_spec,
    build_schematic_from_spec_v2,
    preview_build_from_spec,
    preview_build_from_spec_v2,
)
from kicad_mcp.utils.schematic_builder import (
    schematic_quality_report as build_quality_report,
)
from kicad_mcp.utils.schematic_intent import (
    apply_connection_plan_v2,
    connect_pin_to_net,
    snap_schematic_to_grid_model,
)
from kicad_mcp.utils.schematic_pins import (
    SCHEMATIC_GRID_MM,
    get_symbol_pin_map,
    get_symbol_pin_map_from_schematic,
    verify_native_net_membership,
)
from kicad_mcp.utils.schematic_visual_layout import apply_visual_layout_to_v2_spec
from kicad_mcp.utils.transactional_edit import (
    create_file_backup,
    export_schematic_svg_file,
    get_file_diff_against_backup,
    restore_backup_manifest,
    validate_local_directory,
    validate_local_path,
    validate_schematic_with_cli_export,
)


def _resolve_project_alias(
    project_path: str | None,
    schematic_path: str | None = None,
) -> str:
    candidate = project_path or schematic_path
    if not candidate:
        raise ValueError("project_path is required")
    path = Path(candidate)
    if project_path is None and schematic_path and path.suffix == ".kicad_sch":
        project_candidate = path.with_suffix(".kicad_pro")
        if project_candidate.exists():
            return str(project_candidate)
    return str(candidate)


def _suggested_library_queries(query: str, kind: str) -> list[str]:
    normalized = query.strip()
    if not normalized:
        return ["resistor", "capacitor", "connector"] if kind == "symbol" else ["0603", "SOT-23", "PinHeader"]
    compact = re.sub(r"[^A-Za-z0-9]+", " ", normalized).strip()
    suggestions = [compact] if compact and compact != normalized else []
    tokens = compact.split()
    suggestions.extend(tokens[:2])
    if kind == "symbol":
        suggestions.extend(["Device", "Connector", "MCU"])
    else:
        suggestions.extend(["Resistor_SMD", "Capacitor_SMD", "Package"])
    deduped = []
    for item in suggestions:
        if item and item not in deduped:
            deduped.append(item)
    return deduped[:5]


def _native_netlist_for_tool(schematic_path: str) -> dict[str, Any]:
    if getattr(export_native_netlist, "__module__", "") == "kicad_mcp.utils.native_netlist":
        return validate_schematic_batch(
            schematic_path,
            need_netlist=True,
            need_erc=False,
            timeout_seconds=60.0,
        ).native_netlist or {"success": False, "error": "Native netlist export did not run"}
    return export_native_netlist(schematic_path)


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
    def schematic_preview_build_from_spec(project_path: str, spec: dict[str, Any]) -> dict[str, Any]:
        """Preview a spec-driven schematic build without writing files."""
        return preview_build_from_spec(project_path, spec)

    @mcp.tool()
    def schematic_preview_build_from_spec_v2(
        project_path: str | None = None,
        spec: dict[str, Any] | None = None,
        schematic_path: str | None = None,
        intent: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Preview an agent-friendly parts/nets schematic build without writing files.

        Part symbols must be full KiCad library IDs such as "Device:R"; use find_symbols
        first when unsure. Nets may use ["U1", "1"] or {"ref": "U1", "pin": "1"} endpoints.
        """
        resolved_project = _resolve_project_alias(project_path, schematic_path)
        return preview_build_from_spec_v2(resolved_project, spec or intent or {})

    @mcp.tool()
    def schematic_preview_design_intent(
        project_path: str | None = None,
        intent: dict[str, Any] | None = None,
        schematic_path: str | None = None,
        spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compile generic bulk design intent into a v2 schematic spec without writing."""
        return _schematic_design_intent_response(
            _resolve_project_alias(project_path, schematic_path),
            intent or spec or {},
            mode="update",
            dry_run=True,
            strict=False,
            detail="compact",
            include_expanded_spec=False,
            tool_name="schematic_preview_design_intent",
            visual_layout=True,
            visual_style="readable",
        )

    @mcp.tool()
    def schematic_apply_design_intent(
        project_path: str | None = None,
        intent: dict[str, Any] | None = None,
        mode: str = "update",
        dry_run: bool = False,
        strict: bool = False,
        detail: str = "compact",
        include_expanded_spec: bool = False,
        visual_layout: bool = True,
        visual_style: str = "readable",
        dry_run_validation: str = "none",
        schematic_path: str | None = None,
        spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compile and apply generic bulk schematic design intent.

        Prefer this high-level tool for agent schematic generation. Intent may describe
        parts, rails, pin_rules, interfaces, support_circuits, bulk_connections, and
        no_connect_rules; the compiler expands those into the v2 build spec.
        """
        return _schematic_design_intent_response(
            _resolve_project_alias(project_path, schematic_path),
            intent or spec or {},
            mode=mode,
            dry_run=dry_run,
            strict=strict,
            detail=detail,
            include_expanded_spec=include_expanded_spec,
            tool_name="schematic_apply_design_intent",
            visual_layout=visual_layout,
            visual_style=visual_style,
            dry_run_validation=dry_run_validation,
        )

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
    def schematic_build_from_spec_v2(
        project_path: str | None = None,
        spec: dict[str, Any] | None = None,
        mode: str = "update",
        backup: bool = True,
        run_erc: bool = True,
        allow_destructive_replace: bool = False,
        detail: str = "compact",
        include_diff: bool = False,
        include_preview: bool = False,
        include_full_native_netlist: bool = False,
        run_quality_report: bool = False,
        schematic_path: str | None = None,
        intent: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build a schematic from an agent-friendly parts/nets/no_connects specification.

        Part symbol/lib_id values must be full KiCad library IDs such as "Device:R", not
        unit names such as "R_1_1". Prefer mode="update"; mode="replace" requires
        allow_destructive_replace=True when the schematic is non-empty.
        """
        if not backup:
            return {
                "success": False,
                "project_path": project_path or schematic_path,
                "error": "backup=False is not supported; schematic builds are always backed up",
            }
        resolved_project = _resolve_project_alias(project_path, schematic_path)
        result = build_schematic_from_spec_v2(
            resolved_project,
            spec or intent or {},
            mode=mode,
            run_erc=run_erc,
            allow_destructive_replace=allow_destructive_replace,
            detail=detail,
            include_diff=include_diff,
            include_preview=include_preview,
            include_full_native_netlist=include_full_native_netlist,
            run_quality_report=run_quality_report,
        )
        if result.get("success"):
            result.setdefault("tool", "schematic_build_from_spec_v2")
            result.setdefault("stage", "schematic_built")
            result.setdefault("changed", True)
            result.setdefault("warnings", [])
            result.setdefault("recommended_next_tool", "schematic_quality_report")
            result.setdefault("recommended_next_arguments", {"project_path": resolved_project})
        return result

    @mcp.tool()
    def schematic_quality_report(
        project_path: str | None = None,
        run_erc: bool = True,
        schematic_path: str | None = None,
        detail: str = "compact",
    ) -> dict[str, Any]:
        """Summarize schematic ERC, netlist, footprint, page-bound, and grid quality."""
        resolved_project = _resolve_project_alias(project_path, schematic_path)
        try:
            return _format_quality_report(build_quality_report(resolved_project, run_erc=run_erc), detail)
        except Exception as exc:
            return {"success": False, "project_path": resolved_project, "error": str(exc)}

    @mcp.tool()
    def schematic_assign_footprints(
        project_path: str,
        assignments: list[dict[str, str]],
        verify: bool = True,
    ) -> dict[str, Any]:
        """Bulk-assign schematic Footprint properties by reference."""
        return _schematic_assign_footprints(project_path, assignments, verify=verify)

    @mcp.tool()
    def schematic_assign_default_footprints(
        project_path: str,
        refs: list[str] | None = None,
        strategy: str = "symbol_default_then_filter",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Assign missing footprints from symbol defaults, then footprint filters."""
        return _schematic_assign_default_footprints(project_path, refs, strategy, dry_run)

    @mcp.tool()
    def schematic_footprint_report(project_path: str) -> dict[str, Any]:
        """Report missing and invalid schematic footprints with default suggestions."""
        return _schematic_footprint_report(project_path)

    @mcp.tool()
    def schematic_design_intent_schema(section: str = "all") -> dict[str, Any]:
        """Return compact schema examples for schematic_apply_design_intent."""
        return design_intent_schema(section)

    @mcp.tool()
    def schematic_explain_erc(
        project_path: str,
        include_suggestions: bool = True,
        timeout_seconds: float | None = None,
        detail: str = "compact",
    ) -> dict[str, Any]:
        """Explain KiCad ERC violations as generic blocking, accepted-warning, or manual-fix findings."""
        return _schematic_explain_erc(project_path, include_suggestions, timeout_seconds, detail)

    @mcp.tool()
    def schematic_plan_erc_fixes(
        project_path: str,
        timeout_seconds: float | None = None,
        detail: str = "compact",
    ) -> dict[str, Any]:
        """Produce a non-destructive generic ERC repair plan."""
        return _schematic_plan_erc_fixes(project_path, timeout_seconds, detail)

    @mcp.tool()
    async def schematic_apply_functional_layout(
        project_path: str,
        preserve_connectivity: bool = True,
        arrange_properties: bool = True,
        run_quality_report: bool = True,
        placement_rules: dict[str, Any] | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Place schematic symbols into generic functional lanes and preserve pin-attached labels."""
        if ctx:
            await ctx.info("Applying generic schematic functional layout")
        try:
            schematic_path = _schematic_file_path(project_path)
        except Exception as exc:
            return {"success": False, "project_path": project_path, "error": str(exc)}

        result = _apply_transactional_schematic_authoring(
            schematic_path,
            lambda schematic: _apply_schematic_functional_layout(
                schematic,
                schematic_path,
                preserve_connectivity,
                arrange_properties,
                placement_rules,
            ),
        )
        if result.get("success") and run_quality_report:
            result["quality_report"] = build_quality_report(schematic_path, run_erc=True)
        return result

    @mcp.tool()
    async def project_completion_report(
        project_path: str,
        run_erc: bool = True,
        run_drc: bool = False,
        timeout_seconds: float | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Summarize schematic, netlist, PCB sync, ratsnest/routing, and optional DRC completion status."""
        if ctx:
            await ctx.info("Building project completion report")
        return await _project_completion_report(project_path, run_erc, run_drc, timeout_seconds)

    @mcp.tool()
    async def project_next_actions(
        project_path: str,
        run_erc: bool = True,
        run_drc: bool = False,
        timeout_seconds: float | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Return ordered generic next actions for bringing a KiCad project to completion."""
        if ctx:
            await ctx.info("Planning project next actions")
        return await _project_next_actions(project_path, run_erc, run_drc, timeout_seconds)

    @mcp.tool()
    async def project_design_state(
        project_path: str | None = None,
        run_erc: bool = True,
        run_drc: bool = False,
        schematic_path: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Return one compact state object with the safest next KiCad MCP action."""
        resolved_project = _resolve_project_alias(project_path, schematic_path)
        if ctx:
            await ctx.info("Building project design state from cached schematic validation")
        return await _project_design_state(resolved_project, run_erc, run_drc)

    @mcp.tool()
    async def schematic_apply_safe_erc_fixes(
        project_path: str,
        fixes: list[dict[str, Any]] | None = None,
        dry_run: bool = True,
        timeout_seconds: float | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Apply only explicitly safe ERC fixes; ambiguous ERC findings remain manual."""
        if ctx:
            await ctx.info("Applying safe ERC fixes" if not dry_run else "Previewing safe ERC fixes")
        return _schematic_apply_safe_erc_fixes(project_path, fixes, dry_run, timeout_seconds)

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
    def find_symbols(
        query: str,
        max_results: int = 10,
        library: str | None = None,
        limit: int | None = None,
        filter: str | None = None,
    ) -> dict[str, Any]:
        """Fuzzy-search KiCad symbols before resolving an exact lib_id."""
        try:
            resolved_library = library or filter
            resolved_limit = limit if limit is not None else max_results
            matches = search_symbols(query, max_results=resolved_limit, library=resolved_library)
            return {
                "success": True,
                "query": query,
                "library": resolved_library,
                "count": len(matches),
                "matches": matches,
                "suggested_queries": _suggested_library_queries(query, "symbol") if not matches else [],
                "recommended_next_tool": "resolve_symbol",
            }
        except Exception as exc:
            return {"success": False, "query": query, "error": str(exc)}

    @mcp.tool()
    def find_footprints(
        query: str,
        max_results: int = 10,
        library: str | None = None,
        limit: int | None = None,
        filter: str | None = None,
    ) -> dict[str, Any]:
        """Fuzzy-search KiCad footprints before resolving an exact footprint_id."""
        try:
            resolved_library = library or filter
            resolved_limit = limit if limit is not None else max_results
            matches = search_footprints(query, max_results=resolved_limit, library=resolved_library)
            return {
                "success": True,
                "query": query,
                "library": resolved_library,
                "count": len(matches),
                "matches": matches,
                "suggested_queries": _suggested_library_queries(query, "footprint") if not matches else [],
                "recommended_next_tool": "resolve_footprint",
            }
        except Exception as exc:
            return {"success": False, "query": query, "error": str(exc)}

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
        """Advanced low-level geometry tool. Prefer intent-based schematic connection tools."""
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
        """Advanced low-level label tool. Prefer schematic_connect_pin_to_net for normal wiring."""
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
        """Advanced low-level geometry tool. Prefer schematic_connect_pins for normal wiring."""
        if ctx:
            await ctx.info("Connecting schematic points")
        return _apply_transactional_schematic_authoring(
            schematic_path,
            lambda schematic: {"connection": schematic.connect_points(start, end, style, net_name)},
        )

    @mcp.tool()
    def schematic_get_pin_map(schematic_path: str, reference: str) -> dict[str, Any]:
        """Advanced diagnostics tool for inspecting transformed placed-symbol pin positions."""
        return get_symbol_pin_map(schematic_path, reference)

    @mcp.tool()
    def schematic_snap_to_grid(
        schematic_path: str,
        grid_mm: float = 1.27,
        include_symbols: bool = True,
        include_labels: bool = True,
        include_wires: bool = True,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Snap schematic symbols, labels, wires, and no-connects to a KiCad-safe grid."""
        try:
            validated_path = validate_local_path(schematic_path, "schematic", must_exist=True)
            schematic = KiCadSchematic.from_file(validated_path)
            summary = snap_schematic_to_grid_model(
                schematic,
                grid_mm,
                include_symbols=include_symbols,
                include_labels=include_labels,
                include_wires=include_wires,
            )
            if dry_run:
                return {
                    "success": True,
                    "tool": "schematic_snap_to_grid",
                    "stage": "schematic_cleanup",
                    "schematic_path": validated_path,
                    "dry_run": True,
                    "changed": summary["changed_count"] > 0,
                    "snap": summary,
                    "warnings": [],
                    "recommended_next_tool": "schematic_snap_to_grid",
                    "recommended_next_arguments": {"schematic_path": validated_path, "dry_run": False},
                }
            return _apply_transactional_schematic_authoring(
                validated_path,
                lambda model: {
                    "snap": snap_schematic_to_grid_model(
                        model,
                        grid_mm,
                        include_symbols=include_symbols,
                        include_labels=include_labels,
                        include_wires=include_wires,
                    )
                },
            )
        except Exception as exc:
            return {
                "success": False,
                "tool": "schematic_snap_to_grid",
                "stage": "schematic_cleanup",
                "schematic_path": schematic_path,
                "error": str(exc),
                "rolled_back": False,
                "recoverable": True,
                "recommended_next_tool": "schematic_quality_report",
                "debug": {},
            }

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
        """Advanced compatibility alias. Prefer schematic_connect_pin_to_net for normal wiring."""
        if ctx:
            await ctx.info(f"Attaching {net_name} to {reference}.{pin}")
        return _apply_transactional_schematic_authoring(
            schematic_path,
            lambda schematic: {
                "attachment": connect_pin_to_net(
                    schematic,
                    schematic_path,
                    reference,
                    pin,
                    net_name,
                    label_type=label_type,
                    stub_length_mm=stub_length_mm,
                    allow_hidden_power=allow_hidden_power,
                )
            },
            post_write_validator=lambda path: verify_native_net_membership(
                path, reference, pin, net_name
            ),
        )

    @mcp.tool()
    async def schematic_connect_pin_to_net(
        schematic_path: str,
        reference: str,
        pin: str,
        net_name: str,
        label_type: str = "global",
        stub_length_mm: float = 5.08,
        auto_snap: bool = True,
        verify: bool = True,
        fail_on_erc_violations: bool = False,
        replace_existing: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Connect one schematic symbol pin to a named net by electrical intent."""
        if ctx:
            await ctx.info(f"Connecting {reference}.{pin} to {net_name}")
        result = apply_connection_plan_v2(
            schematic_path,
            [
                {
                    "type": "pin_to_net",
                    "ref": reference,
                    "pin": pin,
                    "net": net_name,
                    "label_type": label_type,
                    "stub_length_mm": stub_length_mm,
                }
            ],
            verify_native_netlist=verify,
            run_erc=verify,
            auto_snap=auto_snap,
            fail_on_erc_violations=fail_on_erc_violations,
            replace_existing=replace_existing,
        )
        result["tool"] = "schematic_connect_pin_to_net"
        return result

    @mcp.tool()
    async def schematic_connect_pins(
        schematic_path: str,
        ref_a: str,
        pin_a: str,
        ref_b: str,
        pin_b: str,
        net_name: str | None = None,
        style: str = "auto",
        auto_snap: bool = True,
        verify: bool = True,
        fail_on_erc_violations: bool = False,
        replace_existing: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Connect two pins by assigning both to the same named net."""
        if ctx:
            await ctx.info(f"Connecting {ref_a}.{pin_a} to {ref_b}.{pin_b}")
        result = apply_connection_plan_v2(
            schematic_path,
            [
                {
                    "type": "pin_to_pin",
                    "from": {"ref": ref_a, "pin": pin_a},
                    "to": {"ref": ref_b, "pin": pin_b},
                    "net": net_name,
                    "style": style,
                }
            ],
            verify_native_netlist=verify,
            run_erc=verify,
            auto_snap=auto_snap,
            fail_on_erc_violations=fail_on_erc_violations,
            replace_existing=replace_existing,
        )
        result["tool"] = "schematic_connect_pins"
        return result

    @mcp.tool()
    async def schematic_connect_pin_to_ground(
        schematic_path: str,
        reference: str,
        pin: str,
        ground_net: str = "GND",
        verify: bool = True,
        fail_on_erc_violations: bool = False,
        replace_existing: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Connect one schematic symbol pin to a ground net by electrical intent."""
        if ctx:
            await ctx.info(f"Connecting {reference}.{pin} to {ground_net}")
        result = apply_connection_plan_v2(
            schematic_path,
            [{"type": "pin_to_ground", "ref": reference, "pin": pin, "net": ground_net}],
            verify_native_netlist=verify,
            run_erc=verify,
            auto_snap=True,
            fail_on_erc_violations=fail_on_erc_violations,
            replace_existing=replace_existing,
        )
        result["tool"] = "schematic_connect_pin_to_ground"
        return result

    @mcp.tool()
    async def schematic_connect_pin_to_power(
        schematic_path: str,
        reference: str,
        pin: str,
        power_net: str,
        verify: bool = True,
        fail_on_erc_violations: bool = False,
        replace_existing: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Connect one schematic symbol pin to a power net by electrical intent."""
        if ctx:
            await ctx.info(f"Connecting {reference}.{pin} to {power_net}")
        result = apply_connection_plan_v2(
            schematic_path,
            [{"type": "pin_to_power", "ref": reference, "pin": pin, "net": power_net}],
            verify_native_netlist=verify,
            run_erc=verify,
            auto_snap=True,
            fail_on_erc_violations=fail_on_erc_violations,
            replace_existing=replace_existing,
        )
        result["tool"] = "schematic_connect_pin_to_power"
        return result

    @mcp.tool()
    async def schematic_apply_connection_plan(
        schematic_path: str,
        connections: list[dict[str, Any]],
        no_connects: list[dict[str, Any]] | None = None,
        run_native_netlist: bool = True,
        rollback_on_failed_membership: bool = True,
        fail_on_erc_violations: bool = False,
        replace_existing: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Primary agent tool for schematic wiring. Prefer this over raw wire/point tools."""
        if ctx:
            await ctx.info(f"Applying {len(connections)} schematic connections")
        result = apply_connection_plan(
            schematic_path,
            connections,
            no_connects,
            run_native_netlist,
            rollback_on_failed_membership,
            fail_on_erc_violations,
            replace_existing=replace_existing,
        )
        if ctx and run_native_netlist:
            await ctx.info("Applied schematic edits; checked native netlist membership")
        return result

    @mcp.tool()
    async def schematic_add_no_connect(
        schematic_path: str,
        reference: str,
        pin: str,
        allow_hidden_power: bool = False,
        allow_hidden_no_connect: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Add a no-connect marker at an actual symbol pin coordinate.

        For hidden no-connect markers, use allow_hidden_no_connect; allow_hidden_power
        is retained for compatibility and is intended for net attachment tools.
        """
        if ctx:
            await ctx.info(f"Adding no-connect marker to {reference}.{pin}")
        return add_no_connect_marker(
            schematic_path,
            reference,
            pin,
            allow_hidden_power,
            allow_hidden_no_connect=allow_hidden_no_connect,
        )

    @mcp.tool()
    async def schematic_delete_item(
        schematic_path: str,
        item_type: str,
        item_id: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Delete a top-level schematic symbol, wire, label, or no-connect marker."""
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
    def pcb_complete_from_schematic(
        project_path: str,
        board_width_mm: float = 100.0,
        board_height_mm: float = 80.0,
        placement_style: str = "functional",
        preserve_existing_placement: bool = True,
        place_pcb: bool = True,
        placement_rules: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Sync PCB from schematic, optionally apply generic functional placement, and report routing status."""
        return _complete_pcb_from_schematic(
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
        """Sync PCB from schematic, apply initial placement, and return placement/ratsnest/quality reports."""
        if ctx:
            await ctx.info("Synchronizing PCB from schematic and building placement report")
        return await _pcb_sync_place_and_report(
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
        files = get_project_files(validate_local_path(project_path, "project", must_exist=True))
        if "pcb" not in files:
            return {"success": False, "project_path": project_path, "error": "PCB file not found"}
        return _apply_transactional_pcb_edit(
            files["pcb"],
            lambda pcb: _apply_functional_placement(
                pcb, board_width_mm, board_height_mm, placement_rules
            ),
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
    def pcb_quality_report(project_path: str) -> dict[str, Any]:
        """Summarize PCB sync, placement, routing, and ratsnest status."""
        try:
            files = get_project_files(validate_local_path(project_path, "project", must_exist=True))
            if "pcb" not in files:
                return {"success": False, "project_path": project_path, "error": "PCB file not found"}
            pcb = KiCadPcb.from_file(files["pcb"])
            return _pcb_quality_report(project_path, files["pcb"], pcb)
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
        return _apply_transactional_pcb_edit(
            pcb_path,
            lambda pcb: {
                "route": _route_between_pads(
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
        files = get_project_files(validate_local_path(project_path, "project", must_exist=True))
        if "pcb" not in files:
            return {"success": False, "project_path": project_path, "error": "PCB file not found"}
        pcb = KiCadPcb.from_file(files["pcb"])
        ratsnest = _build_ratsnest(project_path, files["pcb"], pcb)
        connections = ratsnest.get("connections", [])
        if connection_index < 0 or connection_index >= len(connections):
            return {
                "success": False,
                "project_path": project_path,
                "error": "connection_index is outside the ratsnest connection list",
                "connection_count": len(connections),
            }
        connection = connections[connection_index]
        return _apply_transactional_pcb_edit(
            files["pcb"],
            lambda model: {
                "route": _route_between_pads(
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


def _schematic_file_path(project_or_schematic_path: str) -> str:
    if project_or_schematic_path.endswith(".kicad_sch"):
        return str(validate_local_path(project_or_schematic_path, "schematic", must_exist=True))
    files = get_project_files(validate_local_path(project_or_schematic_path, "project", must_exist=True))
    if "schematic" not in files:
        raise FileNotFoundError("Schematic file not found")
    return files["schematic"]


def _schematic_footprint_report(project_or_schematic_path: str) -> dict[str, Any]:
    try:
        schematic_path = _schematic_file_path(project_or_schematic_path)
        schematic = KiCadSchematic.from_file(schematic_path)
        symbols = [symbol for symbol in schematic.list_symbols() if _is_assignable_symbol(symbol)]
        missing_footprints = [
            symbol["reference"] for symbol in symbols if not str(symbol.get("footprint") or "").strip()
        ]
        invalid_footprints = []
        for symbol in symbols:
            footprint = str(symbol.get("footprint") or "").strip()
            if not footprint:
                continue
            try:
                resolve_footprint_node(footprint)
            except Exception as exc:
                invalid_footprints.append(
                    {
                        "ref": symbol["reference"],
                        "footprint": footprint,
                        "error": str(exc),
                    }
                )
        suggested_assignments = []
        for symbol in symbols:
            if symbol["reference"] not in missing_footprints:
                continue
            suggestion = _symbol_default_footprint(symbol)
            if suggestion is not None:
                suggested_assignments.append(
                    {
                        "ref": symbol["reference"],
                        "footprint": suggestion["footprint"],
                        "source": suggestion["source"],
                    }
                )
        return {
            "success": True,
            "project_path": project_or_schematic_path,
            "schematic_path": schematic_path,
            "symbol_count": len(symbols),
            "missing_footprint_count": len(missing_footprints),
            "missing_footprints": missing_footprints,
            "invalid_footprints": invalid_footprints,
            "invalid_footprint_count": len(invalid_footprints),
            "suggested_assignments": suggested_assignments,
        }
    except Exception as exc:
        return {"success": False, "project_path": project_or_schematic_path, "error": str(exc)}


def _schematic_assign_footprints(
    project_or_schematic_path: str,
    assignments: list[dict[str, str]],
    *,
    verify: bool,
) -> dict[str, Any]:
    try:
        schematic_path = _schematic_file_path(project_or_schematic_path)
        schematic = KiCadSchematic.from_file(schematic_path)
        refs = {symbol["reference"] for symbol in schematic.list_symbols()}
        normalized_assignments = []
        malformed_assignments = []
        missing_refs = []
        invalid_footprints = []
        for index, assignment in enumerate(assignments or []):
            if not isinstance(assignment, dict):
                malformed_assignments.append({"index": index, "error": "assignment must be an object"})
                continue
            ref = str(assignment.get("ref") or assignment.get("reference") or "").strip()
            footprint = str(assignment.get("footprint") or "").strip()
            if not ref or not footprint:
                malformed_assignments.append(
                    {"index": index, "ref": ref, "footprint": footprint, "error": "assignment requires ref and footprint"}
                )
                continue
            if ref not in refs:
                missing_refs.append(ref)
                continue
            if verify:
                try:
                    resolve_footprint_node(footprint)
                except Exception as exc:
                    invalid_footprints.append({"ref": ref, "footprint": footprint, "error": str(exc)})
                    continue
            normalized_assignments.append({"ref": ref, "footprint": footprint})
        if malformed_assignments or missing_refs or invalid_footprints:
            return {
                "success": False,
                "project_path": project_or_schematic_path,
                "schematic_path": schematic_path,
                "assigned_count": 0,
                "missing_refs": sorted(set(missing_refs)),
                "invalid_footprints": invalid_footprints,
                "malformed_assignments": malformed_assignments,
                "footprint_report": _compact_footprint_report(_schematic_footprint_report(schematic_path)),
            }

        def mutate(target: KiCadSchematic) -> dict[str, Any]:
            assigned = []
            for assignment in normalized_assignments:
                property_data = target.set_property(
                    assignment["ref"], "Footprint", assignment["footprint"]
                )
                assigned.append({**assignment, "property": property_data})
            return {"assigned": assigned, "assigned_count": len(assigned)}

        result = _apply_transactional_schematic_authoring(schematic_path, mutate)
        report = _schematic_footprint_report(schematic_path)
        result.update(
            {
                "tool": "schematic_assign_footprints",
                "project_path": project_or_schematic_path,
                "schematic_path": schematic_path,
                "assigned_count": result.get("changed_objects", {}).get("assigned_count", 0),
                "missing_refs": [],
                "invalid_footprints": [],
                "footprint_report": _compact_footprint_report(report),
            }
        )
        return result
    except Exception as exc:
        return {"success": False, "project_path": project_or_schematic_path, "error": str(exc)}


def _schematic_assign_default_footprints(
    project_or_schematic_path: str,
    refs: list[str] | None,
    strategy: str,
    dry_run: bool,
) -> dict[str, Any]:
    if strategy != "symbol_default_then_filter":
        return {
            "success": False,
            "project_path": project_or_schematic_path,
            "error": "strategy must be symbol_default_then_filter",
        }
    try:
        schematic_path = _schematic_file_path(project_or_schematic_path)
        schematic = KiCadSchematic.from_file(schematic_path)
        wanted_refs = {str(ref) for ref in refs} if refs else None
        assignments = []
        skipped = []
        missing_refs = []
        symbols_by_ref = {
            symbol["reference"]: symbol
            for symbol in schematic.list_symbols()
            if _is_assignable_symbol(symbol)
        }
        if wanted_refs is not None:
            missing_refs = sorted(ref for ref in wanted_refs if ref not in symbols_by_ref)
        request_status = _default_footprint_request_status(wanted_refs, missing_refs)
        for symbol in symbols_by_ref.values():
            ref = symbol["reference"]
            if wanted_refs is not None and ref not in wanted_refs:
                continue
            if str(symbol.get("footprint") or "").strip():
                skipped.append({"ref": ref, "reason": "footprint already assigned"})
                continue
            suggestion = _symbol_default_footprint(symbol)
            if suggestion is None:
                skipped.append({"ref": ref, "reason": "no symbol default or matching footprint filter"})
                continue
            assignments.append(
                {
                    "ref": ref,
                    "footprint": suggestion["footprint"],
                    "source": suggestion["source"],
                }
            )
        if request_status["all_requested_refs_missing"]:
            return {
                "success": False,
                "project_path": project_or_schematic_path,
                "schematic_path": schematic_path,
                "dry_run": dry_run,
                "assigned_count": 0,
                "planned_assignments": assignments,
                "planned_assignment_count": len(assignments),
                "missing_refs": missing_refs,
                "partial_success": False,
                "skipped": skipped,
                "footprint_report": _compact_footprint_report(_schematic_footprint_report(schematic_path)),
                "error": "all requested refs were missing",
            }
        if dry_run:
            return {
                "success": True,
                "project_path": project_or_schematic_path,
                "schematic_path": schematic_path,
                "dry_run": True,
                "assigned_count": 0,
                "planned_assignments": assignments,
                "planned_assignment_count": len(assignments),
                "missing_refs": missing_refs,
                "partial_success": request_status["partial_success"],
                "skipped": skipped,
                "footprint_report": _compact_footprint_report(_schematic_footprint_report(schematic_path)),
            }
        assign_result = _schematic_assign_footprints(
            schematic_path,
            [{"ref": item["ref"], "footprint": item["footprint"]} for item in assignments],
            verify=True,
        )
        assign_result["tool"] = "schematic_assign_default_footprints"
        assign_result["dry_run"] = False
        assign_result["planned_assignments"] = assignments
        assign_result["planned_assignment_count"] = len(assignments)
        assign_result["missing_refs"] = missing_refs or assign_result.get("missing_refs", [])
        assign_result["partial_success"] = request_status["partial_success"]
        assign_result["skipped"] = skipped
        return assign_result
    except Exception as exc:
        return {"success": False, "project_path": project_or_schematic_path, "error": str(exc)}


def _compact_footprint_report(report: dict[str, Any]) -> dict[str, Any]:
    if not report.get("success"):
        return report
    return {
        "symbol_count": report.get("symbol_count", 0),
        "missing_footprint_count": report.get("missing_footprint_count", 0),
        "missing_footprints": report.get("missing_footprints", []),
        "invalid_footprints": report.get("invalid_footprints", []),
        "invalid_footprint_count": report.get("invalid_footprint_count", 0),
    }


def _default_footprint_request_status(
    wanted_refs: set[str] | None, missing_refs: list[str]
) -> dict[str, bool]:
    if wanted_refs is None:
        return {"all_requested_refs_missing": False, "partial_success": False}
    missing = set(missing_refs)
    return {
        "all_requested_refs_missing": bool(wanted_refs) and missing == wanted_refs,
        "partial_success": bool(missing) and missing != wanted_refs,
    }


def _symbol_default_footprint(symbol: dict[str, Any]) -> dict[str, str] | None:
    lib_id = str(symbol.get("lib_id") or "")
    if not lib_id:
        return None
    try:
        for suggestion in resolve_symbol_footprint_suggestions(lib_id, max_results=5):
            footprint = str(suggestion.get("footprint") or "").strip()
            if not footprint:
                continue
            try:
                resolve_footprint_node(footprint)
            except Exception:
                continue
            return {
                "footprint": footprint,
                "source": str(suggestion.get("source") or "symbol_default"),
            }
    except Exception:
        return None
    return None


def _is_assignable_symbol(symbol: dict[str, Any]) -> bool:
    ref = str(symbol.get("reference") or "")
    lib_id = str(symbol.get("lib_id") or "")
    return bool(ref) and not ref.startswith("#") and not lib_id.startswith("power:")


def _design_intent_artifact_dir(project_path: str) -> Path:
    path = Path(project_path)
    if path.suffix in {".kicad_pro", ".kicad_sch"}:
        return path.parent / ".kicad_mcp"
    return path / ".kicad_mcp"


def _save_visual_expanded_spec(project_path: str, spec: dict[str, Any]) -> str:
    artifact_dir = _design_intent_artifact_dir(project_path)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    output_path = artifact_dir / "design_intent.visual_expanded_spec.json"
    output_path.write_text(json.dumps(spec, indent=2, sort_keys=True), encoding="utf-8")
    return str(output_path)


def _native_dry_run_design_intent(
    project_path: str,
    expanded_spec: dict[str, Any],
    mode: str,
    strict: bool,
) -> dict[str, Any]:
    try:
        with tempfile.TemporaryDirectory(prefix="kicad_mcp_dry_run_") as temp_dir:
            temp_project = _copy_project_for_dry_run(project_path, temp_dir)
            built = build_schematic_from_spec_v2(
                temp_project,
                expanded_spec,
                mode=mode,
                run_erc=strict,
                allow_destructive_replace=False,
                detail="compact",
                include_diff=False,
                include_preview=False,
                include_full_native_netlist=False,
                run_quality_report=False,
            )
            validation = built.get("validation", {}) if isinstance(built.get("validation"), dict) else {}
            post_write = validation.get("post_write", {}) if isinstance(validation.get("post_write"), dict) else {}
            native = post_write.get("native_verification", post_write)
            return {
                "mode": "native",
                "success": bool(built.get("success")),
                "changed_original": False,
                "temp_project_path": temp_project,
                "native_verification": native,
                "missing_connection_count": len(native.get("missing", []))
                if isinstance(native, dict)
                else 0,
                "error": built.get("error"),
            }
    except Exception as exc:
        return {
            "mode": "native",
            "success": False,
            "changed_original": False,
            "error": str(exc),
        }


def _copy_project_for_dry_run(project_path: str, temp_dir: str) -> str:
    source = Path(project_path)
    if source.suffix == ".kicad_sch":
        temp_schematic = Path(temp_dir) / source.name
        shutil.copy2(source, temp_schematic)
        temp_project = temp_schematic.with_suffix(".kicad_pro")
        if source.with_suffix(".kicad_pro").exists():
            shutil.copy2(source.with_suffix(".kicad_pro"), temp_project)
        else:
            temp_project.write_text("{}", encoding="utf-8")
        return str(temp_project)

    files = get_project_files(str(source))
    temp_project = Path(temp_dir) / source.name
    if source.exists():
        shutil.copy2(source, temp_project)
    else:
        temp_project.write_text("{}", encoding="utf-8")
    if "schematic" in files and Path(files["schematic"]).exists():
        shutil.copy2(files["schematic"], Path(temp_dir) / Path(files["schematic"]).name)
    return str(temp_project)


def _schematic_design_intent_response(
    project_path: str,
    intent: dict[str, Any],
    *,
    mode: str,
    dry_run: bool,
    strict: bool,
    detail: str,
    include_expanded_spec: bool,
    tool_name: str,
    visual_layout: bool = True,
    visual_style: str = "readable",
    dry_run_validation: str = "none",
) -> dict[str, Any]:
    compiled = compile_design_intent(project_path, intent, strict=strict)
    expanded_spec = compiled.get("expanded_spec")
    visual_summary = {"enabled": False}
    visual_expanded_spec_path: str | None = None
    if compiled.get("success") and isinstance(expanded_spec, dict) and visual_layout:
        expanded_spec = apply_visual_layout_to_v2_spec(
            expanded_spec,
            page=str(expanded_spec.get("paper") or "A3"),
            style=visual_style,
        )
        visual_summary = expanded_spec.get("layout_hints", {}).get(
            "visual_layout",
            {"enabled": True, "style": visual_style},
        )
        try:
            visual_expanded_spec_path = _save_visual_expanded_spec(project_path, expanded_spec)
        except Exception as exc:
            compiled.setdefault("warnings", []).append(
                {
                    "path": ".kicad_mcp/design_intent.visual_expanded_spec.json",
                    "warning": f"failed to save visual expanded spec: {exc}",
                }
            )
    base: dict[str, Any] = {
        "success": compiled.get("success", False),
        "tool": tool_name,
        "stage": "compiled" if dry_run else "compile_failed",
        "mode": mode,
        "dry_run": dry_run,
        "changed": False,
        "summary": compiled.get("summary", {}),
        "generated_refs": compiled.get("generated_refs", {}),
        "warnings": compiled.get("warnings", []),
        "errors": compiled.get("errors", []),
        "expanded_spec_path": compiled.get("expanded_spec_path"),
        "visual_expanded_spec_path": visual_expanded_spec_path,
        "normalized_intent_path": compiled.get("normalized_intent_path"),
        "report_path": compiled.get("report_path"),
        "visual_layout": visual_summary,
        "recommended_next_tool": (
            "schematic_apply_design_intent" if dry_run else "schematic_quality_report"
        ),
    }
    if include_expanded_spec:
        base["expanded_spec"] = expanded_spec
    if not compiled.get("success"):
        base["recoverable"] = compiled.get("recoverable", True)
        return base
    if dry_run:
        base["stage"] = "preview"
        if dry_run_validation not in {"none", "syntactic", "native"}:
            base["success"] = False
            base["errors"].append(
                {
                    "path": "dry_run_validation",
                    "error": 'dry_run_validation must be one of: "none", "syntactic", "native"',
                }
            )
            return base
        if dry_run_validation == "syntactic":
            base["dry_run_validation"] = {
                "mode": "syntactic",
                "success": True,
                "planned_connection_count": sum(
                    len(endpoints) for endpoints in (expanded_spec or {}).get("nets", {}).values()
                )
                if isinstance(expanded_spec, dict)
                else 0,
            }
        elif dry_run_validation == "native":
            base["dry_run_validation"] = _native_dry_run_design_intent(
                project_path,
                expanded_spec if isinstance(expanded_spec, dict) else {},
                mode,
                strict,
            )
            base["success"] = bool(base["dry_run_validation"].get("success"))
        return base

    built = build_schematic_from_spec_v2(
        project_path,
        expanded_spec,
        mode=mode,
        run_erc=strict,
        allow_destructive_replace=False,
        detail=detail,
        include_diff=False,
        include_preview=True,
        include_full_native_netlist=False,
        run_quality_report=False,
    )
    base["stage"] = "schematic_built" if built.get("success") else "build_failed"
    base["success"] = bool(built.get("success"))
    base["changed"] = bool(built.get("success"))
    if not built.get("success"):
        base["error"] = built.get("error", "schematic build failed")
        if detail == "full":
            base["build_result"] = built
        return base

    try:
        quality = build_quality_report(project_path, run_erc=strict)
    except Exception as exc:
        quality = {"success": False, "error": str(exc)}
    validation = built.get("validation", {}) if isinstance(built.get("validation"), dict) else {}
    post_write = (
        validation.get("post_write", {})
        if isinstance(validation.get("post_write"), dict)
        else {}
    )
    native = quality.get("native_netlist", {}) if isinstance(quality.get("native_netlist"), dict) else {}
    erc = quality.get("erc", {}) if isinstance(quality.get("erc"), dict) else {}
    gate = quality.get("quality_gate", {}) if isinstance(quality.get("quality_gate"), dict) else {}
    base["verification"] = {
        "native_netlist_success": native.get("success"),
        "missing_connection_count": len(post_write.get("missing", [])),
        "erc_total_violations": erc.get("total_violations", 0),
        "quality_gate_passed": gate.get("passed"),
    }
    if strict and (
        base["verification"]["native_netlist_success"] is not True
        or base["verification"]["missing_connection_count"] > 0
        or base["verification"]["quality_gate_passed"] is not True
        or int(base["verification"]["erc_total_violations"] or 0) > 0
    ):
        base["success"] = False
        base["stage"] = "verification_failed"
        base["recoverable"] = True
        base["errors"].append(
            {
                "path": "verification",
                "error": "strict mode verification failed",
                "verification": base["verification"],
            }
        )
    if detail == "full":
        base["build_result"] = built
        base["quality_report"] = quality
    if built.get("schematic_preview"):
        base["schematic_preview"] = built["schematic_preview"]
    elif built.get("schematic_preview_error"):
        base["schematic_preview_error"] = built["schematic_preview_error"]
    else:
        try:
            schematic_path = get_project_files(project_path).get("schematic")
            if schematic_path:
                svg_result = export_schematic_svg_file(schematic_path, None)
                if svg_result.get("success"):
                    base["schematic_preview"] = svg_preview_metadata(svg_result["svg_path"])
                else:
                    base["schematic_preview_error"] = svg_result.get("error")
        except Exception as exc:
            base["schematic_preview_error"] = str(exc)
    return base


def _format_quality_report(report: dict[str, Any], detail: str = "compact") -> dict[str, Any]:
    normalized = str(detail or "compact").lower()
    if normalized == "full":
        return report
    if normalized not in {"summary", "compact"}:
        return {
            "success": False,
            "error": 'detail must be one of: "summary", "compact", "full"',
            "detail": detail,
        }

    visual = report.get("visual_quality", {}) if isinstance(report.get("visual_quality"), dict) else {}
    erc = report.get("erc", {}) if isinstance(report.get("erc"), dict) else {}
    native = report.get("native_netlist", {}) if isinstance(report.get("native_netlist"), dict) else {}
    compact = {
        "success": report.get("success"),
        "schematic_path": report.get("schematic_path"),
        "detail": normalized,
        "page": report.get("page"),
        "symbol_count": report.get("symbol_count", 0),
        "wire_count": report.get("wire_count", 0),
        "label_count": report.get("label_count", 0),
        "no_connect_count": report.get("no_connect_count", 0),
        "missing_footprint_count": report.get("missing_footprint_count", 0),
        "outside_page_count": report.get("outside_page_count", 0),
        "off_grid_count": report.get("off_grid_count", 0),
        "dangling_label_count": report.get("dangling_label_count", 0),
        "isolated_label_count": report.get("isolated_label_count", 0),
        "power_ground_mismatch_count": report.get("power_ground_mismatch_count", 0),
        "quality_gate": report.get("quality_gate", {}),
        "erc": {
            "success": erc.get("success"),
            "total_violations": erc.get("total_violations"),
            "unacceptable_categories": erc.get("unacceptable_categories", {}),
            "error": erc.get("error"),
        },
        "native_netlist": {
            "success": native.get("success"),
            "component_count": native.get("component_count"),
            "net_count": native.get("net_count"),
            "non_empty_nets": native.get("non_empty_nets"),
            "error": native.get("error"),
        },
        "visual_quality": {
            "readability_score": visual.get("readability_score"),
            "blocking_count": visual.get("blocking_count"),
            "warning_count": visual.get("warning_count"),
            "symbol_overlap_count": visual.get("symbol_overlap_count"),
            "label_inside_symbol_count": visual.get("label_inside_symbol_count"),
            "floating_wire_count": visual.get("floating_wire_count"),
        },
        "recommended_next_tool": "schematic_quality_report"
        if report.get("quality_gate", {}).get("passed")
        else "schematic_plan_erc_fixes",
    }
    if normalized == "compact":
        compact.update(
            {
                "missing_footprints": report.get("missing_footprints", []),
                "invalid_footprints": report.get("invalid_footprints", []),
                "symbols_outside_page": report.get("symbols_outside_page", []),
                "dangling_labels": report.get("dangling_labels", []),
                "isolated_labels": report.get("isolated_labels", []),
                "power_ground_mismatches": report.get("power_ground_mismatches", []),
            }
        )
    return compact


def _schematic_explain_erc(
    project_or_schematic_path: str,
    include_suggestions: bool = True,
    timeout_seconds: float | None = None,
    detail: str = "compact",
) -> dict[str, Any]:
    try:
        schematic_path = _schematic_file_path(project_or_schematic_path)
        erc = run_erc_via_cli(schematic_path, timeout_seconds=timeout_seconds)
        if not erc.get("success"):
            return {
                "success": False,
                "project_path": project_or_schematic_path,
                "schematic_path": schematic_path,
                "error": erc.get("error", "ERC failed"),
                "erc": erc,
            }
        findings = [
            _explain_erc_violation(violation, include_suggestions)
            for violation in erc.get("violations", [])
        ]
        groups: dict[str, dict[str, Any]] = {}
        for finding in findings:
            group = groups.setdefault(
                finding["type"],
                {
                    "type": finding["type"],
                    "count": 0,
                    "classification_counts": {},
                    "severity_counts": {},
                },
            )
            group["count"] += 1
            classification = finding["classification"]
            severity = finding["severity"]
            group["classification_counts"][classification] = (
                group["classification_counts"].get(classification, 0) + 1
            )
            group["severity_counts"][severity] = group["severity_counts"].get(severity, 0) + 1
        classification_counts: dict[str, int] = {}
        for finding in findings:
            classification_counts[finding["classification"]] = (
                classification_counts.get(finding["classification"], 0) + 1
            )
        result = {
            "success": True,
            "project_path": project_or_schematic_path,
            "schematic_path": schematic_path,
            "total_violations": len(findings),
            "blocking_count": classification_counts.get("blocking", 0),
            "manual_count": classification_counts.get("manual_decision", 0),
            "accepted_warning_count": classification_counts.get("accepted_warning", 0),
            "classification_counts": classification_counts,
            "groups": sorted(groups.values(), key=lambda item: item["type"]),
            "findings": findings,
            "erc": {
                "success": erc.get("success"),
                "total_violations": erc.get("total_violations"),
                "violation_categories": erc.get("violation_categories"),
                "severity_counts": erc.get("severity_counts"),
            },
        }
        return _format_erc_explanation(result, detail)
    except Exception as exc:
        return {"success": False, "project_path": project_or_schematic_path, "error": str(exc)}


def _schematic_plan_erc_fixes(
    project_or_schematic_path: str,
    timeout_seconds: float | None = None,
    detail: str = "compact",
) -> dict[str, Any]:
    explanation = _schematic_explain_erc(
        project_or_schematic_path, include_suggestions=True, timeout_seconds=timeout_seconds, detail="full"
    )
    if not explanation.get("success"):
        return explanation
    dangling_label_fixes = _unique_dangling_label_fixes(explanation["schematic_path"])
    safe_auto_fixes = []
    manual_decisions = []
    accepted_warnings = []
    blocked_reasons = []
    for finding in explanation["findings"]:
        action = finding.get("suggested_action", {})
        if finding.get("type") == "label_dangling":
            labels = finding.get("affected_labels", [])
            if len(labels) == 1 and labels[0] in dangling_label_fixes:
                matched = dangling_label_fixes[labels[0]]
                action = {
                    "kind": "delete_dangling_label",
                    "auto_safe": True,
                    "details": "Delete exactly matched dangling label that is not attached to a pin or wire.",
                    "label_uuid": matched["label_uuid"],
                }
                finding["suggested_action"] = action
        classification = finding["classification"]
        if classification == "accepted_warning":
            accepted_warnings.append(
                {
                    "type": finding["type"],
                    "refs": finding["affected_refs"],
                    "reason": finding["explanation"],
                    "suggested_action": action,
                }
            )
        elif action.get("auto_safe"):
            safe_auto_fixes.append(
                {
                    "type": finding["type"],
                    "refs": finding["affected_refs"],
                    "labels": finding["affected_labels"],
                    "label_uuid": action.get("label_uuid"),
                    "action": action,
                }
            )
        else:
            manual_decisions.append(
                {
                    "type": finding["type"],
                    "severity": finding["severity"],
                    "refs": finding["affected_refs"],
                    "labels": finding["affected_labels"],
                    "reason": finding["explanation"],
                    "suggested_action": action,
                }
            )
            blocked_reasons.append(
                f"{finding['type']}: {finding['explanation']}"
            )
    result = {
        "success": True,
        "project_path": explanation["project_path"],
        "schematic_path": explanation["schematic_path"],
        "erc_total_violations": explanation["total_violations"],
        "safe_auto_fixes": safe_auto_fixes,
        "safe_auto_fix_count": len(safe_auto_fixes),
        "manual_decisions": manual_decisions,
        "manual_decision_count": len(manual_decisions),
        "accepted_warnings": accepted_warnings,
        "accepted_warning_count": len(accepted_warnings),
        "blocked_reasons": blocked_reasons,
        "blocked": bool(manual_decisions or safe_auto_fixes),
        "explanation": explanation,
    }
    return _format_erc_plan(result, detail)


def _format_erc_explanation(report: dict[str, Any], detail: str = "compact") -> dict[str, Any]:
    normalized = str(detail or "compact").lower()
    if normalized == "full":
        return report
    if normalized not in {"summary", "compact"}:
        return {
            "success": False,
            "error": 'detail must be one of: "summary", "compact", "full"',
            "detail": detail,
        }
    result = {
        "success": report.get("success"),
        "project_path": report.get("project_path"),
        "schematic_path": report.get("schematic_path"),
        "detail": normalized,
        "total_violations": report.get("total_violations", 0),
        "blocking_count": report.get("blocking_count", 0),
        "manual_count": report.get("manual_count", 0),
        "accepted_warning_count": report.get("accepted_warning_count", 0),
        "classification_counts": report.get("classification_counts", {}),
        "erc": report.get("erc", {}),
        "recommended_next_tool": "schematic_plan_erc_fixes"
        if report.get("blocking_count", 0) or report.get("manual_count", 0)
        else "schematic_quality_report",
    }
    if normalized == "compact":
        result["groups"] = report.get("groups", [])
        result["findings"] = report.get("findings", [])
    return result


def _format_erc_plan(plan: dict[str, Any], detail: str = "compact") -> dict[str, Any]:
    normalized = str(detail or "compact").lower()
    if normalized == "full":
        return plan
    if normalized not in {"summary", "compact"}:
        return {
            "success": False,
            "error": 'detail must be one of: "summary", "compact", "full"',
            "detail": detail,
        }
    result = {
        "success": plan.get("success"),
        "project_path": plan.get("project_path"),
        "schematic_path": plan.get("schematic_path"),
        "detail": normalized,
        "erc_total_violations": plan.get("erc_total_violations", 0),
        "safe_auto_fix_count": plan.get("safe_auto_fix_count", 0),
        "manual_decision_count": plan.get("manual_decision_count", 0),
        "accepted_warning_count": plan.get("accepted_warning_count", 0),
        "blocked": plan.get("blocked", False),
        "recommended_next_tool": "schematic_apply_safe_erc_fixes"
        if plan.get("safe_auto_fix_count", 0)
        else "schematic_explain_erc",
    }
    if normalized == "compact":
        result.update(
            {
                "safe_auto_fixes": plan.get("safe_auto_fixes", []),
                "manual_decisions": plan.get("manual_decisions", []),
                "accepted_warnings": plan.get("accepted_warnings", []),
                "blocked_reasons": plan.get("blocked_reasons", []),
            }
        )
    return result


def _unique_dangling_label_fixes(schematic_path: str) -> dict[str, dict[str, Any]]:
    try:
        quality = build_quality_report(schematic_path, run_erc=False)
    except Exception:
        return {}
    by_text: dict[str, list[dict[str, Any]]] = {}
    for label in quality.get("dangling_labels", []):
        text = str(label.get("text") or "")
        label_uuid = label.get("uuid")
        if text and label_uuid:
            by_text.setdefault(text, []).append(label)
    return {
        text: {"label_uuid": labels[0]["uuid"], "label": labels[0]}
        for text, labels in by_text.items()
        if len(labels) == 1
    }


def _explain_erc_violation(
    violation: dict[str, Any], include_suggestions: bool
) -> dict[str, Any]:
    violation_type = violation.get("type", "unknown")
    severity = violation.get("severity", "unknown")
    description = violation.get("description") or violation.get("message") or ""
    affected_refs, affected_pins, affected_labels = _erc_affected_objects(violation)
    classification = _erc_classification(violation_type, severity)
    explanation, action = _erc_explanation_and_action(
        violation_type,
        severity,
        description,
        affected_refs,
        affected_pins,
        affected_labels,
    )
    finding = {
        "type": violation_type,
        "severity": severity,
        "classification": classification,
        "description": description,
        "affected_refs": affected_refs,
        "affected_pins": affected_pins,
        "affected_labels": affected_labels,
        "explanation": explanation,
        "items": violation.get("items", []),
    }
    if include_suggestions:
        finding["suggested_action"] = action
    return finding


def _erc_classification(violation_type: str, severity: str) -> str:
    if violation_type in {"lib_symbol_mismatch"} and severity == "warning":
        return "accepted_warning"
    if violation_type in {
        "label_dangling",
        "isolated_pin_label",
        "endpoint_off_grid",
        "pin_not_connected",
        "ground_pin_not_ground",
        "power_pin_not_driven",
        "pin_to_pin",
    }:
        return "blocking"
    if severity in {"error", "fatal"}:
        return "blocking"
    return "manual_decision"


def _erc_explanation_and_action(
    violation_type: str,
    severity: str,
    description: str,
    affected_refs: list[str],
    affected_pins: list[dict[str, str]],
    affected_labels: list[str],
) -> tuple[str, dict[str, Any]]:
    if violation_type == "lib_symbol_mismatch":
        return (
            "The schematic embeds a symbol copy that differs from the installed library. "
            "This is usually a warning, not a connectivity failure.",
            {
                "kind": "update_or_accept_library_symbol",
                "auto_safe": False,
                "details": "Update the embedded symbol from library if you want the warning gone, or accept it when the local copy is intentional.",
            },
        )
    if violation_type == "label_dangling":
        return (
            "A label is not attached to a pin or wire endpoint, so KiCad ignores it electrically.",
            {
                "kind": "reattach_label_to_pin_or_wire",
                "auto_safe": False,
                "labels": affected_labels,
                "details": "Move the label to the exact pin coordinate or a wire endpoint using pin-aware attachment tools.",
            },
        )
    if violation_type == "pin_not_connected":
        return (
            "A symbol pin is electrically unconnected and has no no-connect marker.",
            {
                "kind": "connect_pin_or_add_no_connect",
                "auto_safe": False,
                "refs": affected_refs,
                "pins": affected_pins,
                "details": "Attach the pin to the intended net, or add a no-connect marker only if the design intentionally leaves it unused.",
            },
        )
    if violation_type == "endpoint_off_grid":
        return (
            "A wire, label, or endpoint is off KiCad's schematic grid, which can break visual or electrical attachment.",
            {
                "kind": "snap_endpoint_to_grid",
                "auto_safe": False,
                "details": "Snap the affected endpoint/label/wire to the schematic grid, then rerun ERC.",
            },
        )
    if violation_type in {"power_pin_not_driven", "pin_to_pin"}:
        return (
            "A power or driving-pin rule requires design intent before changing connectivity.",
            {
                "kind": "inspect_power_or_driver_intent",
                "auto_safe": False,
                "details": "Add a valid driver/PWR_FLAG only when the net is intentionally powered by that source.",
            },
        )
    if "power" in violation_type or "ground" in violation_type:
        return (
            "ERC indicates a possible power/ground net mismatch.",
            {
                "kind": "fix_power_ground_assignment",
                "auto_safe": False,
                "details": "Use the pin map and native netlist to verify the pin is on the correct power or ground net.",
            },
        )
    return (
        f"KiCad reported {severity} ERC violation '{violation_type}'.",
        {
            "kind": "manual_inspection",
            "auto_safe": False,
            "details": description or "Inspect the referenced schematic item and decide the intended fix.",
        },
    )


def _erc_affected_objects(
    violation: dict[str, Any]
) -> tuple[list[str], list[dict[str, str]], list[str]]:
    refs: set[str] = set()
    labels: set[str] = set()
    pins: list[dict[str, str]] = []
    for item in violation.get("items", []):
        text = item.get("description", "")
        ref, pin = _parse_erc_symbol_pin(text)
        if ref:
            refs.add(ref)
        if ref and pin:
            pins.append({"ref": ref, "pin": pin})
        label = _parse_erc_label(text)
        if label:
            labels.add(label)
    return sorted(refs), pins, sorted(labels)


def _parse_erc_symbol_pin(text: str) -> tuple[str | None, str | None]:
    pin_match = re.search(r"Symbol\s+([#A-Za-z0-9_]+)\s+Pin\s+([^\s\[]+)", text)
    if pin_match:
        return pin_match.group(1), pin_match.group(2)
    symbol_match = re.search(r"(?:Symbol|Symbole)\s+([#A-Za-z0-9_]+)", text)
    if symbol_match:
        return symbol_match.group(1), None
    return None, None


def _parse_erc_label(text: str) -> str | None:
    label_match = re.search(r"Label(?:\s+\w+)?\s+'([^']+)'", text)
    return label_match.group(1) if label_match else None


def _apply_schematic_functional_layout(
    schematic: KiCadSchematic,
    schematic_path: str,
    preserve_connectivity: bool,
    arrange_properties: bool,
    placement_rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bounds = schematic.get_sheet_bounds()
    width = float(bounds["width"])
    height = float(bounds["height"])
    symbols = sorted(
        schematic.list_symbols(),
        key=lambda symbol: _schematic_layout_priority(symbol),
    )
    occupied: list[dict[str, float]] = []
    moved_symbols = []
    label_moves = []
    no_connect_moves = []
    refusals = []

    for index, symbol in enumerate(symbols):
        reference = symbol["reference"]
        role = _infer_component_role(reference, symbol)
        target = _schematic_symbol_position(
            symbol,
            role,
            index,
            width,
            height,
            placement_rules,
        )
        target = _avoid_schematic_overlap(target, symbol.get("bounds", {}), occupied, width, height)
        current = symbol["position"]
        dx = target["x"] - current["x"]
        dy = target["y"] - current["y"]
        if abs(dx) < 1e-6 and abs(dy) < 1e-6 and target["angle"] == current.get("angle", 0.0):
            occupied.append(_translated_bounds(symbol.get("bounds", {}), dx, dy))
            continue

        pin_points = _symbol_pin_points(schematic, schematic_path, reference)
        labels_at_pins = _labels_at_points(schematic, pin_points)
        no_connects_at_pins = _no_connects_at_points(schematic, pin_points)
        try:
            if preserve_connectivity:
                move_result = schematic.move_symbol_with_connections(
                    reference, target["x"], target["y"], target["angle"]
                )
            else:
                move_result = {
                    "symbol": schematic.move_symbol(reference, target["x"], target["y"], target["angle"]),
                    "moved_wire_endpoints": [],
                    "moved_labels": [],
                }
            moved_label_ids = {
                item.get("uuid") for item in move_result.get("moved_labels", []) if item.get("uuid")
            }
            for label in labels_at_pins:
                if label.get("uuid") in moved_label_ids:
                    continue
                label_moves.append(
                    schematic.move_label(
                        label["uuid"],
                        _snap_schematic(label["position"]["x"] + dx),
                        _snap_schematic(label["position"]["y"] + dy),
                        label["position"].get("angle", 0.0),
                    )
                )
            no_connect_moves.extend(
                _move_no_connects_at_points(schematic, no_connects_at_pins, dx, dy)
            )
            moved_symbols.append(
                {
                    "reference": reference,
                    "role": role,
                    "from": current,
                    "to": target,
                    "preserved_connectivity": preserve_connectivity,
                    "moved_label_count": len(labels_at_pins),
                    "moved_no_connect_count": len(no_connects_at_pins),
                }
            )
            moved_bounds = schematic.get_symbol(reference)
            if moved_bounds is not None:
                occupied.append(moved_bounds.get("bounds", {}))
        except Exception as exc:
            refusals.append({"reference": reference, "role": role, "error": str(exc)})
            occupied.append(symbol.get("bounds", {}))

    property_moves = (
        schematic.auto_arrange_symbol_properties_all()
        if arrange_properties
        else {"moves": [], "move_count": 0}
    )
    if refusals:
        raise ValueError(f"Functional layout refused {len(refusals)} symbols: {refusals}")
    return {
        "sheet": bounds,
        "moved_symbols": moved_symbols,
        "moved_symbol_count": len(moved_symbols),
        "moved_labels": label_moves,
        "moved_label_count": len(label_moves),
        "moved_no_connects": no_connect_moves,
        "moved_no_connect_count": len(no_connect_moves),
        "property_arrangement": property_moves,
        "placement_style": "generic_functional_lanes",
    }


def _schematic_layout_priority(symbol: dict[str, Any]) -> tuple[int, str]:
    role = _infer_component_role(symbol.get("reference", ""), symbol)
    priorities = {
        "usb_connector": 0,
        "protection": 1,
        "regulator": 2,
        "primary_controller": 3,
        "display": 4,
        "connector": 5,
        "button": 6,
        "ic": 7,
        "capacitor": 8,
        "resistor": 9,
        "power_symbol": 10,
        "other": 11,
    }
    return (priorities.get(role, 11), symbol.get("reference", ""))


def _schematic_symbol_position(
    symbol: dict[str, Any],
    role: str,
    index: int,
    width: float,
    height: float,
    placement_rules: dict[str, Any] | None,
) -> dict[str, float]:
    reference = symbol["reference"]
    current_angle = symbol["position"].get("angle", 0.0)
    rule = _placement_rule_position(reference, role, placement_rules, width, height)
    if rule is not None:
        return {
            "x": _snap_schematic(rule[0]),
            "y": _snap_schematic(rule[1]),
            "angle": rule[2] if _placement_rule_has_angle(reference, role, placement_rules) else current_angle,
        }
    x, y, angle = _schematic_role_lane_position(role, index, width, height)
    return {"x": _snap_schematic(x), "y": _snap_schematic(y), "angle": current_angle if angle == 0.0 else angle}


def _placement_rule_has_angle(
    reference: str, role: str, placement_rules: dict[str, Any] | None
) -> bool:
    if not placement_rules:
        return False
    rule = None
    references = placement_rules.get("references", {})
    roles = placement_rules.get("roles", {})
    if isinstance(references, dict):
        rule = references.get(reference) or references.get(reference.upper())
    if rule is None and reference in placement_rules:
        rule = placement_rules.get(reference)
    if rule is None and isinstance(roles, dict):
        rule = roles.get(role)
    return isinstance(rule, dict) and "angle" in rule


def _schematic_role_lane_position(
    role: str, index: int, width: float, height: float
) -> tuple[float, float, float]:
    offset = index % 8
    row = index // 8
    if role == "usb_connector":
        return width * 0.12, height * 0.24 + offset * 12.0, 0.0
    if role == "protection":
        return width * 0.22, height * 0.20 + offset * 10.0, 0.0
    if role == "regulator":
        return width * 0.30, height * 0.22 + offset * 10.0, 0.0
    if role == "primary_controller":
        return width * 0.48, height * 0.38 + offset * 6.0, 0.0
    if role == "display":
        return width * 0.76, height * 0.34 + offset * 8.0, 0.0
    if role == "connector":
        return width * (0.34 + (offset % 4) * 0.16), height * 0.76 + row * 10.0, 0.0
    if role == "button":
        return width * (0.34 + offset * 0.08), height * 0.64 + row * 10.0, 0.0
    if role == "capacitor":
        return width * 0.34 + offset * 12.0, height * 0.34 + row * 9.0, 0.0
    if role == "resistor":
        return width * 0.34 + offset * 12.0, height * 0.50 + row * 9.0, 0.0
    if role == "power_symbol":
        return width * 0.18 + offset * 10.0, height * 0.12 + row * 8.0, 0.0
    if role == "ic":
        return width * 0.50 + (offset % 3) * 18.0, height * 0.50 + row * 12.0, 0.0
    return width * 0.52 + (offset % 4) * 14.0, height * 0.58 + row * 10.0, 0.0


def _avoid_schematic_overlap(
    target: dict[str, float],
    original_bounds: dict[str, float],
    occupied: list[dict[str, float]],
    width: float,
    height: float,
) -> dict[str, float]:
    if not original_bounds:
        return target
    current = dict(target)
    for _attempt in range(40):
        dx = current["x"] - (original_bounds["left"] + original_bounds["right"]) / 2.0
        dy = current["y"] - (original_bounds["top"] + original_bounds["bottom"]) / 2.0
        candidate_bounds = _translated_bounds(original_bounds, dx, dy)
        if not any(_bounds_intersect(candidate_bounds, other, padding=2.0) for other in occupied):
            return current
        current["y"] = _snap_schematic(current["y"] + 12.7)
        if current["y"] > height - 15.0:
            current["y"] = 15.24
            current["x"] = _snap_schematic(current["x"] + 17.78)
        if current["x"] > width - 15.0:
            current["x"] = 15.24
    return current


def _translated_bounds(bounds: dict[str, float], dx: float, dy: float) -> dict[str, float]:
    if not bounds:
        return {}
    return {
        "left": bounds["left"] + dx,
        "right": bounds["right"] + dx,
        "top": bounds["top"] + dy,
        "bottom": bounds["bottom"] + dy,
    }


def _symbol_pin_points(
    schematic: KiCadSchematic, schematic_path: str, reference: str
) -> set[tuple[float, float]]:
    pin_map = get_symbol_pin_map_from_schematic(schematic, schematic_path, reference)
    if not pin_map.get("success"):
        return set()
    return {
        (pin["connection_point"]["x"], pin["connection_point"]["y"])
        for pin in pin_map.get("pins", [])
    }


def _labels_at_points(
    schematic: KiCadSchematic, points: set[tuple[float, float]]
) -> list[dict[str, Any]]:
    labels = []
    for label in schematic.list_labels():
        position = label["position"]
        if (position["x"], position["y"]) in points and label.get("uuid"):
            labels.append(label)
    return labels


def _no_connects_at_points(
    schematic: KiCadSchematic, points: set[tuple[float, float]]
) -> list[dict[str, Any]]:
    markers = []
    for marker in schematic.list_no_connects():
        position = marker["position"]
        if (position["x"], position["y"]) in points:
            markers.append(marker)
    return markers


def _move_no_connects_at_points(
    schematic: KiCadSchematic, markers: list[dict[str, Any]], dx: float, dy: float
) -> list[dict[str, Any]]:
    moved = []
    for marker in markers:
        old = marker["position"]
        for node in schematic._top_level("no_connect"):
            position = schematic._parse_at(node)
            if position["x"] == old["x"] and position["y"] == old["y"]:
                new_x = _snap_schematic(old["x"] + dx)
                new_y = _snap_schematic(old["y"] + dy)
                _set_no_connect_at(node, new_x, new_y)
                moved.append(
                    {
                        "uuid": marker.get("uuid"),
                        "from": old,
                        "to": {"x": new_x, "y": new_y},
                    }
                )
                break
    return moved


def _set_no_connect_at(node: SExprList, x: float, y: float) -> None:
    replacement = SExprList(
        [
            SExprAtom("at"),
            SExprAtom(_format_schematic_number(x)),
            SExprAtom(_format_schematic_number(y)),
        ]
    )
    at_expr = node.first_child("at")
    if at_expr is None:
        node.items.append(replacement)
        return
    for index, item in enumerate(node.items):
        if item is at_expr:
            node.items[index] = replacement
            return


def _format_schematic_number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _snap_schematic(value: float, grid: float = SCHEMATIC_GRID_MM) -> float:
    return round(round(value / grid) * grid, 6)


async def _project_completion_report(
    project_path: str,
    run_erc: bool,
    run_drc: bool,
    timeout_seconds: float | None,
) -> dict[str, Any]:
    try:
        validated_project = validate_local_path(project_path, "project", must_exist=True)
        files = get_project_files(validated_project)
        schematic_report = (
            build_quality_report(validated_project, run_erc=run_erc)
            if "schematic" in files
            else {"success": False, "error": "Schematic file not found"}
        )
        native = (
            _native_netlist_for_tool(files["schematic"])
            if "schematic" in files
            else {"success": False, "error": "Schematic file not found"}
        )
        erc_plan = (
            _schematic_plan_erc_fixes(files["schematic"], timeout_seconds)
            if run_erc and "schematic" in files
            else {
                "success": True,
                "skipped": True,
                "safe_auto_fix_count": 0,
                "manual_decision_count": 0,
                "accepted_warning_count": 0,
                "blocked": False,
            }
        )
        pcb_quality = None
        ratsnest = None
        drc = {"success": True, "skipped": True, "reason": "run_drc=False"}
        if "pcb" in files:
            pcb = KiCadPcb.from_file(files["pcb"])
            pcb_quality = _pcb_quality_report(validated_project, files["pcb"], pcb)
            ratsnest = _build_ratsnest(validated_project, files["pcb"], pcb)
            if run_drc:
                drc = await run_drc_via_cli(files["pcb"], None, timeout_seconds=timeout_seconds)
        else:
            pcb_quality = {"success": False, "error": "PCB file not found"}
            ratsnest = {"success": False, "error": "PCB file not found"}

        quality_gate = schematic_report.get("quality_gate", {})
        erc = schematic_report.get("erc", {})
        unacceptable_erc = erc.get("unacceptable_categories", {})
        erc_blocked = bool(
            erc_plan.get("safe_auto_fix_count", 0) or erc_plan.get("manual_decision_count", 0)
        )
        schematic_complete = bool(
            schematic_report.get("success")
            and quality_gate.get("passed")
            and native.get("success")
            and native.get("connectivity_complete", False)
            and not unacceptable_erc
            and not erc_blocked
        )
        pcb_synced = bool(
            pcb_quality
            and pcb_quality.get("success")
            and pcb_quality.get("net_count", 0) > 0
            and pcb_quality.get("assigned_pad_count", 0) > 0
        )
        routing_complete = bool(pcb_quality and pcb_quality.get("routing_complete", False))
        drc_clean = bool(
            drc.get("skipped")
            or (drc.get("success") and drc.get("total_violations", 0) == 0)
        )
        return {
            "success": True,
            "project_path": validated_project,
            "files": files,
            "status": {
                "schematic_complete": schematic_complete,
                "pcb_synced": pcb_synced,
                "placement_valid": bool(pcb_quality and pcb_quality.get("placement_valid", False)),
                "routing_complete": routing_complete,
                "drc_clean_or_skipped": drc_clean,
                "ready_for_pcb_sync": schematic_complete,
                "ready_for_routing": schematic_complete and pcb_synced,
                "ready_for_release": schematic_complete and pcb_synced and routing_complete and drc_clean,
            },
            "schematic": schematic_report,
            "native_netlist": {
                "success": native.get("success"),
                "component_count": native.get("component_count"),
                "net_count": native.get("net_count"),
                "connectivity_complete": native.get("connectivity_complete"),
                "error": native.get("error"),
            },
            "erc_plan": {
                "success": erc_plan.get("success"),
                "safe_auto_fix_count": erc_plan.get("safe_auto_fix_count"),
                "manual_decision_count": erc_plan.get("manual_decision_count"),
                "accepted_warning_count": erc_plan.get("accepted_warning_count"),
                "blocked": erc_plan.get("blocked"),
                "accepted_warnings": erc_plan.get("accepted_warnings", []),
                "blocked_reasons": erc_plan.get("blocked_reasons", []),
            },
            "pcb": pcb_quality,
            "ratsnest": {
                "success": ratsnest.get("success"),
                "net_count": ratsnest.get("net_count"),
                "connection_count": ratsnest.get("connection_count"),
                "error": ratsnest.get("error"),
            },
            "drc": {
                "success": drc.get("success"),
                "skipped": drc.get("skipped", False),
                "total_violations": drc.get("total_violations"),
                "violation_categories": drc.get("violation_categories"),
                "error": drc.get("error"),
            },
        }
    except Exception as exc:
        return {"success": False, "project_path": project_path, "error": str(exc)}


async def _project_next_actions(
    project_path: str,
    run_erc: bool,
    run_drc: bool,
    timeout_seconds: float | None,
) -> dict[str, Any]:
    report = await _project_completion_report(project_path, run_erc, run_drc, timeout_seconds)
    if not report.get("success"):
        return report
    actions = _next_actions_from_completion_report(report)
    return {
        "success": True,
        "project_path": report["project_path"],
        "actions": actions,
        "action_count": len(actions),
        "top_action": actions[0] if actions else None,
        "status": report["status"],
        "completion_report": report,
    }


async def _project_design_state(
    project_path: str,
    run_erc: bool,
    run_drc: bool,
) -> dict[str, Any]:
    report = await _project_completion_report(project_path, run_erc, run_drc, None)
    if not report.get("success"):
        return {
            "success": False,
            "stage": "unknown",
            "project_path": project_path,
            "blocking_issues": [report.get("error", "Project state could not be read")],
            "recommended_next_tool": "create_kicad_project",
            "recommended_arguments": {},
            "tools_to_avoid_now": ["schematic_add_wire", "schematic_connect_points"],
            "safe_to_continue": False,
            "debug": report,
        }
    status = report.get("status", {})
    pcb = report.get("pcb", {}) or {}
    drc = report.get("drc", {}) or {}
    blocking: list[str] = []
    if not status.get("schematic_complete"):
        stage = "schematic_invalid"
        next_tool = "schematic_apply_connection_plan"
        next_args = {"schematic_path": report.get("files", {}).get("schematic", project_path)}
        quality = report.get("schematic", {}).get("quality_gate", {})
        blocking.extend(
            f"{key}: {value}"
            for key, value in quality.get("blocking_counts", {}).items()
            if value
        )
    elif not status.get("pcb_synced"):
        stage = "schematic_valid"
        next_tool = "pcb_sync_place_and_report"
        next_args = {"project_path": report["project_path"]}
    elif not status.get("placement_valid"):
        stage = "pcb_synced"
        next_tool = "pcb_apply_functional_placement"
        next_args = {"project_path": report["project_path"]}
    elif pcb.get("routing_status") in {"unrouted", "partially_routed", "unknown_needs_drc"}:
        stage = "routing_needed"
        next_tool = "pcb_get_ratsnest"
        next_args = {"project_path": report["project_path"]}
    elif run_drc and drc.get("success") and drc.get("total_violations", 0) == 0:
        stage = "ready"
        next_tool = "project_design_state"
        next_args = {"project_path": report["project_path"], "run_drc": True}
    else:
        stage = "drc_needed"
        next_tool = "run_drc_check"
        next_args = {"project_path": report["project_path"]}
    return {
        "success": True,
        "project_path": report["project_path"],
        "stage": stage,
        "blocking_issues": blocking,
        "recommended_next_tool": next_tool,
        "recommended_arguments": next_args,
        "tools_to_avoid_now": ["schematic_add_wire", "schematic_connect_points"],
        "safe_to_continue": not blocking,
        "status": status,
        "summary": {
            "schematic_complete": status.get("schematic_complete"),
            "pcb_synced": status.get("pcb_synced"),
            "placement_valid": status.get("placement_valid"),
            "routing_status": pcb.get("routing_status"),
        },
    }


def _next_actions_from_completion_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    actions = []
    erc_plan = report.get("erc_plan", {})
    schematic = report.get("schematic", {})
    quality_gate = schematic.get("quality_gate", {})
    blocking_counts = quality_gate.get("blocking_counts", {})
    pcb = report.get("pcb", {}) or {}
    ratsnest = report.get("ratsnest", {}) or {}
    drc = report.get("drc", {}) or {}
    status = report.get("status", {})

    if erc_plan.get("safe_auto_fix_count", 0) > 0:
        actions.append(
            _next_action(
                "fix_safe_erc_items",
                "Fix deterministic ERC issues",
                "schematic_apply_safe_erc_fixes",
                "high",
                "The ERC planner found fixes marked safe to apply.",
                {"dry_run_first": True},
            )
        )
    if erc_plan.get("manual_decision_count", 0) > 0:
        actions.append(
            _next_action(
                "resolve_manual_erc_decisions",
                "Resolve ERC items needing design intent",
                "schematic_plan_erc_fixes",
                "high",
                "Some ERC findings cannot be fixed safely without choosing the intended circuit behavior.",
                {"blocked_reasons": erc_plan.get("blocked_reasons", [])},
            )
        )
    if not quality_gate.get("passed", False):
        actions.append(
            _next_action(
                "fix_schematic_quality_gate",
                "Fix schematic quality blockers",
                "schematic_quality_report",
                "high",
                "The schematic quality gate has blocking findings.",
                {"blocking_counts": blocking_counts},
            )
        )
    if not report.get("native_netlist", {}).get("connectivity_complete", False):
        actions.append(
            _next_action(
                "complete_native_netlist",
                "Complete schematic connectivity",
                "schematic_apply_connection_plan",
                "high",
                "Native KiCad netlist extraction is incomplete.",
                {},
            )
        )
    if status.get("schematic_complete") and not status.get("pcb_synced"):
        actions.append(
            _next_action(
                "sync_pcb_from_schematic",
                "Sync PCB from schematic",
                "pcb_complete_from_schematic",
                "high",
                "Schematic is complete, but PCB footprints/pad nets are not synced.",
                {},
            )
        )
    if status.get("pcb_synced") and not status.get("placement_valid"):
        actions.append(
            _next_action(
                "apply_pcb_functional_placement",
                "Apply functional PCB placement",
                "pcb_apply_functional_placement",
                "medium",
                "PCB exists but placement has overlap or keepout warnings.",
                {
                    "overlap_warning_count": pcb.get("overlap_warning_count"),
                    "keepout_warning_count": pcb.get("keepout_warning_count"),
                },
            )
        )
    if status.get("ready_for_routing") and not status.get("routing_complete"):
        actions.append(
            _next_action(
                "route_unrouted_nets",
                "Route remaining ratsnest connections",
                "pcb_get_ratsnest",
                "high",
                "PCB is synced and placed, but copper routing is not complete.",
                {"ratsnest_connection_count": ratsnest.get("connection_count")},
            )
        )
    if status.get("routing_complete") and drc.get("skipped", False):
        actions.append(
            _next_action(
                "run_drc",
                "Run PCB DRC",
                "run_drc_check",
                "medium",
                "Routing appears complete and DRC has not been run in this report.",
                {},
            )
        )
    if drc.get("success") and drc.get("total_violations", 0):
        actions.append(
            _next_action(
                "fix_drc_violations",
                "Fix PCB DRC violations",
                "run_drc_check",
                "high",
                "KiCad DRC reports board-level violations.",
                {"violation_categories": drc.get("violation_categories")},
            )
        )
    if erc_plan.get("accepted_warning_count", 0) > 0:
        actions.append(
            _next_action(
                "optional_review_accepted_erc_warnings",
                "Review accepted ERC warnings",
                "schematic_explain_erc",
                "low",
                "Only non-blocking accepted ERC warnings remain.",
                {"accepted_warnings": erc_plan.get("accepted_warnings", [])},
            )
        )
    if not actions and status.get("ready_for_release"):
        actions.append(
            _next_action(
                "ready_for_release",
                "Project is ready for release checks",
                "project_completion_report",
                "low",
                "Schematic, PCB sync, routing, and DRC status are complete.",
                {},
            )
        )
    return actions


def _next_action(
    action_id: str,
    title: str,
    tool: str,
    priority: str,
    reason: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": action_id,
        "title": title,
        "tool": tool,
        "priority": priority,
        "reason": reason,
        "details": details,
    }


def _schematic_apply_safe_erc_fixes(
    project_or_schematic_path: str,
    fixes: list[dict[str, Any]] | None,
    dry_run: bool,
    timeout_seconds: float | None,
) -> dict[str, Any]:
    plan = _schematic_plan_erc_fixes(project_or_schematic_path, timeout_seconds, detail="full")
    if not plan.get("success"):
        return plan
    requested_fixes = fixes or plan.get("safe_auto_fixes", [])
    supported, unsupported = _partition_supported_safe_fixes(requested_fixes)
    if dry_run:
        return {
            "success": True,
            "project_path": project_or_schematic_path,
            "schematic_path": plan.get("schematic_path"),
            "dry_run": True,
            "planned_fixes": supported,
            "planned_fix_count": len(supported),
            "unsupported_or_manual": unsupported + plan.get("manual_decisions", []),
            "accepted_warnings": plan.get("accepted_warnings", []),
            "message": "Dry run only; pass dry_run=False to apply supported explicit safe fixes.",
        }
    if not supported:
        return {
            "success": True,
            "project_path": project_or_schematic_path,
            "schematic_path": plan.get("schematic_path"),
            "dry_run": False,
            "applied_fixes": [],
            "applied_fix_count": 0,
            "unsupported_or_manual": unsupported + plan.get("manual_decisions", []),
            "accepted_warnings": plan.get("accepted_warnings", []),
            "message": "No supported safe ERC fixes were available to apply.",
        }

    schematic_path = plan["schematic_path"]
    result = _apply_transactional_schematic_authoring(
        schematic_path,
        lambda schematic: {"applied_fixes": _apply_supported_safe_fixes(schematic, supported)},
    )
    result["dry_run"] = False
    result["unsupported_or_manual"] = unsupported + plan.get("manual_decisions", [])
    result["accepted_warnings"] = plan.get("accepted_warnings", [])
    return result


def _partition_supported_safe_fixes(
    fixes: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    supported = []
    unsupported = []
    for fix in fixes:
        action = fix.get("action", fix.get("suggested_action", {}))
        kind = action.get("kind") or fix.get("kind")
        if kind == "delete_dangling_label" and fix.get("label_uuid"):
            supported.append(fix)
        else:
            unsupported.append(fix)
    return supported, unsupported


def _apply_supported_safe_fixes(
    schematic: KiCadSchematic, fixes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    applied = []
    for fix in fixes:
        action = fix.get("action", fix.get("suggested_action", {}))
        kind = action.get("kind") or fix.get("kind")
        if kind == "delete_dangling_label":
            label_uuid = fix["label_uuid"]
            applied.append(
                {
                    "kind": kind,
                    "label_uuid": label_uuid,
                    "result": schematic.delete_item("label", label_uuid),
                }
            )
    return applied


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
        native = _native_netlist_for_tool(files["schematic"])
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
            missing_pads: list[dict[str, Any]] = []
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


def _complete_pcb_from_schematic(
    project_path: str,
    board_width_mm: float,
    board_height_mm: float,
    placement_style: str,
    preserve_existing_placement: bool,
    place_pcb: bool,
    placement_rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a generic PCB completion stage: sync, place, expose ratsnest."""
    sync = _pcb_sync_from_schematic(
        project_path,
        board_width_mm,
        board_height_mm,
        placement_style,
        preserve_existing_placement=preserve_existing_placement,
    )
    if not sync.get("success"):
        return {
            "success": False,
            "project_path": project_path,
            "stage": "sync",
            "sync": sync,
        }

    files = get_project_files(validate_local_path(project_path, "project", must_exist=True))
    placement = None
    if place_pcb:
        placement = _apply_transactional_pcb_edit(
            files["pcb"],
            lambda pcb: _apply_functional_placement(
                pcb, board_width_mm, board_height_mm, placement_rules
            ),
        )
        if not placement.get("success"):
            return {
                "success": False,
                "project_path": project_path,
                "stage": "placement",
                "sync": sync,
                "placement": placement,
            }

    pcb = KiCadPcb.from_file(files["pcb"])
    ratsnest = _build_ratsnest(project_path, files["pcb"], pcb)
    quality = _pcb_quality_report(project_path, files["pcb"], pcb)
    placement_objects = placement.get("changed_objects", {}) if placement else {}
    return {
        "success": True,
        "tool": "pcb_complete_from_schematic",
        "project_path": project_path,
        "pcb_path": files["pcb"],
        "stage": "placed" if place_pcb else "synced",
        "status": {
            "schematic_complete": bool(
                sync.get("native_netlist", {}).get("connectivity_complete", False)
            ),
            "pcb_synced": True,
            "pcb_placed": bool(place_pcb),
            "routing_complete": False,
            "routing_status": quality.get("routing_status", "unknown_needs_drc"),
            "completion_scope": "sync_and_initial_placement_only",
        },
        "sync": sync,
        "placement": placement,
        "ratsnest": {
            "net_count": ratsnest.get("net_count", 0),
            "connection_count": ratsnest.get("connection_count", 0),
        },
        "quality": quality,
        "warnings": {
            "overlap_warnings": placement_objects.get("overlap_warnings", []),
            "keepout_warnings": placement_objects.get("keepout_warnings", []),
        },
    }


async def _pcb_sync_place_and_report(
    project_path: str,
    board_width_mm: float,
    board_height_mm: float,
    placement_style: str,
    placement_rules: dict[str, Any] | None,
    run_drc: bool,
) -> dict[str, Any]:
    completed = _complete_pcb_from_schematic(
        project_path,
        board_width_mm,
        board_height_mm,
        placement_style,
        preserve_existing_placement=True,
        place_pcb=True,
        placement_rules=placement_rules,
    )
    if not completed.get("success"):
        completed["tool"] = "pcb_sync_place_and_report"
        return completed
    files = get_project_files(validate_local_path(project_path, "project", must_exist=True))
    thumbnail = await _generate_pcb_thumbnail_impl(project_path, None)
    drc = {"success": True, "skipped": True, "reason": "run_drc=False"}
    if run_drc and "pcb" in files:
        drc = await run_drc_via_cli(files["pcb"], None)
    return {
        "success": True,
        "tool": "pcb_sync_place_and_report",
        "stage": completed.get("stage"),
        "project_path": project_path,
        "pcb_path": files.get("pcb"),
        "changed": True,
        "backup_path": completed.get("placement", {}).get("backup_path")
        or completed.get("sync", {}).get("backup_path"),
        "diff": completed.get("placement", {}).get("diff") or completed.get("sync", {}).get("diff"),
        "sync": completed.get("sync"),
        "placement": completed.get("placement"),
        "ratsnest": completed.get("ratsnest"),
        "quality": completed.get("quality"),
        "thumbnail": thumbnail,
        "validation": {"drc": drc},
        "warnings": completed.get("warnings", {}),
        "recommended_next_tool": "pcb_get_ratsnest",
        "recommended_next_arguments": {"project_path": project_path},
    }


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
    placement_rules: dict[str, Any] | None = None,
) -> tuple[float, float, float]:
    role = _infer_component_role(reference, component)
    rule_position = _placement_rule_position(
        reference, role, placement_rules, board_width_mm, board_height_mm
    )
    if rule_position is not None:
        return rule_position
    if placement_style == "grid":
        columns = max(1, int(board_width_mm // 20))
        return 10.0 + (index % columns) * 20.0, 10.0 + (index // columns) * 20.0, 0.0
    return _role_lane_position(role, index, board_width_mm, board_height_mm)


def _placement_rule_position(
    reference: str,
    role: str,
    placement_rules: dict[str, Any] | None,
    board_width_mm: float,
    board_height_mm: float,
) -> tuple[float, float, float] | None:
    if not placement_rules:
        return None
    rule = None
    references = placement_rules.get("references", {})
    roles = placement_rules.get("roles", {})
    if isinstance(references, dict):
        rule = references.get(reference) or references.get(reference.upper())
    if rule is None and reference in placement_rules:
        rule = placement_rules.get(reference)
    if rule is None and isinstance(roles, dict):
        rule = roles.get(role)
    if not isinstance(rule, dict):
        return None
    x = rule.get("x")
    y = rule.get("y")
    if x is None or y is None:
        return None
    angle = rule.get("angle", 0.0)
    return (
        min(max(float(x), 1.0), board_width_mm - 1.0),
        min(max(float(y), 1.0), board_height_mm - 1.0),
        float(angle),
    )


def _infer_component_role(reference: str, component: dict[str, Any]) -> str:
    ref = reference.upper()
    text = (
        f"{reference} {component.get('value', '')} "
        f"{component.get('footprint', '')} {component.get('footprint_name', '')} "
        f"{component.get('lib_id', '')}"
    ).lower()
    if ref.startswith("#") or "power:" in text:
        return "power_symbol"
    if "usb" in text:
        return "usb_connector"
    if any(token in text for token in ("display", "lcd", "oled", "tft", "nhd")):
        return "display"
    if "regulator" in text or "ldo" in text or "1117" in text or "sot-223" in text:
        return "regulator"
    if ref.startswith("D") and any(token in text for token in ("tvs", "esd", "smf", "sm6", "protection")):
        return "protection"
    if any(token in text for token in ("esp", "stm32", "rp2040", "nrf", "mcu", "microcontroller")):
        return "primary_controller"
    if ref.startswith(("SW", "S")):
        return "button"
    if ref.startswith(("J", "P")) or "connector" in text or "header" in text:
        return "connector"
    if ref.startswith("C"):
        return "capacitor"
    if ref.startswith(("R", "RV")):
        return "resistor"
    if ref.startswith("U"):
        return "ic"
    return "other"


def _role_lane_position(
    role: str, index: int, board_width_mm: float, board_height_mm: float
) -> tuple[float, float, float]:
    offset = index % 6
    row = index // 6
    if role == "usb_connector":
        return 6.0, max(12.0, board_height_mm * 0.25 + offset * 4.0), 90.0
    if role == "protection":
        return board_width_mm * 0.18, board_height_mm * 0.25 + offset * 6.0, 0.0
    if role == "regulator":
        return board_width_mm * 0.28, board_height_mm * 0.25 + offset * 6.0, 0.0
    if role == "display":
        return board_width_mm * 0.68, board_height_mm * 0.52 + offset * 3.0, 0.0
    if role == "primary_controller":
        return board_width_mm * 0.46, board_height_mm * 0.35 + offset * 3.0, 0.0
    if role == "connector":
        return board_width_mm * (0.25 + (offset % 4) * 0.18), board_height_mm - 12.0 - row * 10.0, 0.0
    if role == "button":
        return board_width_mm * (0.25 + offset * 0.12), board_height_mm - 10.0 - row * 10.0, 0.0
    if role in {"resistor", "capacitor"}:
        return board_width_mm * 0.30 + (offset * 10.0), board_height_mm * 0.22 + row * 8.0, 0.0
    if role == "ic":
        return board_width_mm * 0.48 + (offset % 3) * 16.0, board_height_mm * 0.50 + row * 12.0, 0.0
    return board_width_mm * 0.5 + (offset % 4) * 12.0, board_height_mm * 0.45 + row * 10.0, 0.0


def _apply_functional_placement(
    pcb: KiCadPcb,
    board_width_mm: float,
    board_height_mm: float,
    placement_rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    outline = pcb.create_board_outline(board_width_mm, board_height_mm)
    moved = []
    occupied: list[dict[str, float]] = []
    overlap_warnings = []
    footprints = sorted(pcb.list_footprints(), key=_placement_priority)
    for index, footprint in enumerate(footprints):
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
            placement_rules,
        )
        role = _infer_component_role(
            ref,
            {
                "value": footprint.get("value", ""),
                "footprint": footprint.get("footprint_name", ""),
            },
        )
        placed_without_overlap = False
        for _attempt in range(25):
            pcb.move_footprint(ref, x, y, angle)
            node = pcb.find_footprint(ref)
            bounds = pcb.footprint_bounds(cast(Any, node)) if node is not None else {}
            if not any(_bounds_intersect(bounds, other, padding=1.0) for other in occupied):
                occupied.append(bounds)
                placed_without_overlap = True
                break
            x += 8.0
            if x > board_width_mm - 8.0:
                x = 10.0
                y += 8.0
        if not placed_without_overlap:
            overlap_warnings.append({"reference": ref, "warning": "Could not find non-overlapping placement"})
        moved.append({"reference": ref, "role": role, "position": {"x": x, "y": y, "angle": angle}})
    keepout_warnings = _esp_antenna_keepout_warnings(pcb)
    return {
        "outline": outline,
        "moved_footprints": moved,
        "overlap_warnings": overlap_warnings,
        "keepout_warnings": keepout_warnings,
        "overlap_warning_count": len(overlap_warnings),
        "keepout_warning_count": len(keepout_warnings),
        "placement_valid": not overlap_warnings and not keepout_warnings,
    }


def _placement_priority(footprint: dict[str, Any]) -> tuple[int, str]:
    ref = footprint.get("reference") or ""
    role = _infer_component_role(
        ref,
        {
            "value": footprint.get("value", ""),
            "footprint": footprint.get("footprint_name", ""),
        },
    )
    priorities = {
        "usb_connector": 0,
        "display": 1,
        "primary_controller": 2,
        "protection": 3,
        "regulator": 4,
        "connector": 5,
        "button": 6,
        "ic": 7,
        "capacitor": 8,
        "resistor": 9,
        "other": 10,
    }
    return (priorities.get(role, 10), ref)


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
        "ratsnest_type": "geometric_pad_ratsnest",
        "net_count": len(pads_by_net),
        "connection_count": len(connections),
        "connections": connections,
    }


def _pcb_quality_report(project_path: str, pcb_path: str, pcb: KiCadPcb) -> dict[str, Any]:
    pads = pcb.footprint_pad_positions()
    assigned_pads = [pad for pad in pads if pad.get("net_name")]
    unassigned_pads = [pad for pad in pads if not pad.get("net_name")]
    ratsnest = _build_ratsnest(project_path, pcb_path, pcb)
    footprints = pcb.list_footprints()
    overlap_warnings = _footprint_overlap_warnings(footprints)
    keepout_warnings = _esp_antenna_keepout_warnings(pcb)
    track_count = len(pcb._top_level("segment"))
    if not assigned_pads:
        routing_status = "unrouted"
        routing_confidence = "low"
    elif track_count == 0 and ratsnest.get("connection_count", 0) > 0:
        routing_status = "unrouted"
        routing_confidence = "medium"
    elif track_count > 0:
        routing_status = "unknown_needs_drc"
        routing_confidence = "medium"
    else:
        routing_status = "unknown_needs_drc"
        routing_confidence = "low"
    return {
        "success": True,
        "project_path": project_path,
        "pcb_path": pcb_path,
        "footprint_count": len(footprints),
        "net_count": max(0, len(pcb.list_nets()) - 1),
        "pad_count": len(pads),
        "assigned_pad_count": len(assigned_pads),
        "unassigned_pad_count": len(unassigned_pads),
        "track_count": track_count,
        "routing_status": routing_status,
        "routing_complete": False,
        "routing_confidence": routing_confidence,
        "requires_drc_for_final_answer": True,
        "ratsnest_connection_count": ratsnest.get("connection_count", 0),
        "overlap_warnings": overlap_warnings,
        "overlap_warning_count": len(overlap_warnings),
        "keepout_warnings": keepout_warnings,
        "keepout_warning_count": len(keepout_warnings),
        "placement_valid": not overlap_warnings and not keepout_warnings,
    }


def _footprint_overlap_warnings(footprints: list[dict[str, Any]]) -> list[dict[str, str]]:
    warnings = []
    for index, first in enumerate(footprints):
        for second in footprints[index + 1:]:
            if _bounds_intersect(first.get("bounds", {}), second.get("bounds", {}), padding=1.0):
                warnings.append(
                    {
                        "reference": first.get("reference", ""),
                        "overlaps": second.get("reference", ""),
                    }
                )
    return warnings


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


def _route_between_pads(
    pcb: KiCadPcb,
    from_ref: str,
    from_pad: str,
    to_ref: str,
    to_pad: str,
    net_name: str | None,
    layer: str,
    width_mm: float,
    strategy: str,
    clearance_mm: float,
) -> dict[str, Any]:
    if strategy != "manhattan":
        raise ValueError("Only strategy='manhattan' is currently supported")
    pads = pcb.footprint_pad_positions()
    start = _find_pad(pads, from_ref, from_pad)
    end = _find_pad(pads, to_ref, to_pad)
    resolved_net = net_name or start.get("net_name") or end.get("net_name")
    if not resolved_net:
        raise ValueError("net_name is required when neither pad has an assigned net")
    assigned_nets: set[str] = set()
    for pad in (start, end):
        if pad.get("net_name"):
            assigned_nets.add(str(pad["net_name"]))
    if len(assigned_nets) > 1 and net_name is None:
        raise ValueError(
            f"Pads are assigned to different nets: {', '.join(sorted(assigned_nets))}"
        )
    route = pcb.add_track(
        resolved_net,
        _manhattan_points([start["position"], end["position"]]),
        layer,
        width_mm,
    )
    route.update(
        {
            "from": {"reference": from_ref, "pad": from_pad, "position": start["position"]},
            "to": {"reference": to_ref, "pad": to_pad, "position": end["position"]},
            "strategy": strategy,
            "clearance_mm": clearance_mm,
        }
    )
    return route


def _find_pad(pads: list[dict[str, Any]], reference: str, pad_number: str) -> dict[str, Any]:
    for pad in pads:
        if pad.get("reference") == reference and str(pad.get("pad")) == str(pad_number):
            return pad
    raise ValueError(f"Pad not found: {reference}.{pad_number}")


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
    *,
    run_cli_validation: bool = True,
    run_drc: bool = False,
    post_write_validator: Callable[[str], dict[str, Any]] | None = None,
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
        cli_export = (
            _validate_pcb_with_cli_export(validated_path)
            if run_cli_validation
            else {"success": True, "skipped": True, "reason": "PCB CLI validation disabled"}
        )
        if not cli_export.get("success"):
            raise ValueError(cli_export.get("stderr") or cli_export.get("error") or "PCB CLI export failed")
        drc = (
            _run_pcb_drc_sync(validated_path)
            if run_drc
            else {"success": True, "skipped": True, "reason": "run_drc=False"}
        )
        if run_drc and not drc.get("success"):
            raise ValueError(drc.get("error", "PCB DRC failed"))
        post_write = (
            post_write_validator(validated_path)
            if post_write_validator is not None
            else {"success": True, "skipped": True, "reason": "Post-write validation disabled"}
        )
        if not post_write.get("success"):
            raise ValueError(post_write.get("error") or post_write.get("reason") or "PCB post-write validation failed")
        diff_result = get_file_diff_against_backup(validated_path, backup["backup_path"])
        return {
            "success": True,
            "tool": "pcb_transactional_edit",
            "stage": "pcb_authoring",
            "changed": True,
            "pcb_path": validated_path,
            "backup_path": backup["backup_path"],
            "changed_objects": change_result,
            "validation": {
                "before": before_validation,
                "after": after_validation,
                "cli_export": cli_export,
                "drc": drc,
                "post_write": post_write,
            },
            "warnings": [],
            "recommended_next_tool": "pcb_quality_report",
            "recommended_next_arguments": {},
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
            "recoverable": True,
            "recommended_next_tool": "pcb_quality_report",
            "debug": {},
            "restore_result": restore_result,
        }


def _validate_pcb_with_cli_export(pcb_path: str) -> dict[str, Any]:
    cli_path = get_kicad_cli_path(required=False)
    if cli_path is None:
        return {"success": True, "skipped": True, "reason": "KiCad CLI is not available"}
    pcb_dir = os.path.dirname(pcb_path) or "."
    with tempfile.TemporaryDirectory(prefix=".kicad_mcp_pcb_validate_", dir=pcb_dir) as temp_dir:
        output_path = os.path.join(temp_dir, "pcb_validation.svg")
        try:
            process = subprocess.run(
                [
                    cli_path,
                    "pcb",
                    "export",
                    "svg",
                    "--output",
                    output_path,
                    "--layers",
                    "F.Cu,B.Cu,Edge.Cuts",
                    pcb_path,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "KiCad CLI PCB SVG export timed out"}
        return {
            "success": process.returncode == 0,
            "skipped": False,
            "returncode": process.returncode,
            "stdout": process.stdout,
            "stderr": process.stderr,
        }


def _run_pcb_drc_sync(pcb_path: str) -> dict[str, Any]:
    cli_path = get_kicad_cli_path(required=False)
    if cli_path is None:
        return {"success": True, "skipped": True, "reason": "KiCad CLI is not available"}
    with tempfile.TemporaryDirectory() as temp_dir:
        output_path = os.path.join(temp_dir, "drc_report.json")
        try:
            process = subprocess.run(
                [cli_path, "pcb", "drc", "--format", "json", "--output", output_path, pcb_path],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "KiCad CLI PCB DRC timed out"}
        if process.returncode != 0:
            return {
                "success": False,
                "error": process.stderr or process.stdout or "KiCad CLI PCB DRC failed",
                "returncode": process.returncode,
            }
        report = json.loads(Path(output_path).read_text(encoding="utf-8")) if os.path.exists(output_path) else {}
        violations = report.get("violations", [])
        return {
            "success": True,
            "total_violations": len(violations),
            "violations": violations,
            "report": report,
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
