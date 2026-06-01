"""
Project, schematic creation, library resolution, and conservative PCB authoring tools.
"""

from collections.abc import Callable, Sequence
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, cast

from fastmcp import FastMCP

from kicad_mcp.config import TIMEOUT_CONSTANTS
from kicad_mcp.tools.drc_impl.cli_drc import _drc_report_violations, run_drc_via_cli
from kicad_mcp.tools.export_tools import _generate_pcb_thumbnail_impl
from kicad_mcp.utils.file_utils import get_project_files
from kicad_mcp.utils.kicad_cli import get_kicad_cli_path
from kicad_mcp.utils.kicad_cli_batch import validate_schematic_batch
from kicad_mcp.utils.kicad_pcb_s_expr import KiCadPcb, validate_pcb_text
from kicad_mcp.utils.kicad_s_expr import (
    KiCadSchematic,
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
    find_symbols_batch as search_symbols_batch,
)
from kicad_mcp.utils.library_resolver import (
    resolve_footprint as resolve_footprint_node,
)
from kicad_mcp.utils.library_resolver import (
    resolve_symbol as resolve_symbol_node,
)
from kicad_mcp.utils.native_netlist import export_native_netlist
from kicad_mcp.utils.path_validator import PathValidationError, PathValidator
from kicad_mcp.utils.schematic_pins import (
    _resolve_symbol_pins,
)
from kicad_mcp.utils.secure_subprocess import SecureSubprocessError, SecureSubprocessRunner
from kicad_mcp.utils.transactional_edit import (
    atomic_write_text,
    create_file_backup,
    get_file_diff_against_backup,
    restore_backup_manifest,
    transactional_file_lock,
    validate_local_directory,
    validate_local_path,
    validate_schematic_with_cli_export,
)


def _heavy_tool_concurrency() -> int:
    try:
        return max(1, int(os.getenv("KICAD_MCP_HEAVY_TOOL_CONCURRENCY", "1")))
    except ValueError:
        return 1


_HEAVY_TOOL_SEMAPHORE = threading.BoundedSemaphore(_heavy_tool_concurrency())


def _run_heavy_library_tool(operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    with _HEAVY_TOOL_SEMAPHORE:
        return operation()


def _resolve_project_alias(
    project_path: str | None,
    schematic_path: str | None = None,
    path: str | None = None,
) -> str:
    candidate = project_path or path or schematic_path
    if not candidate:
        raise ValueError("project_path is required")
    resolved_path = Path(candidate)
    if project_path is None and resolved_path.suffix == ".kicad_sch":
        project_candidate = resolved_path.with_suffix(".kicad_pro")
        if project_candidate.exists():
            return str(project_candidate)
    return str(candidate)


def _suggested_library_queries(query: str, kind: str) -> list[str]:
    normalized = query.strip()
    if not normalized:
        return (
            ["resistor", "capacitor", "connector"]
            if kind == "symbol"
            else ["0603", "SOT-23", "PinHeader"]
        )
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


def _symbol_extends_chain(lib_id: str) -> list[str]:
    chain = [lib_id]
    seen = {lib_id}
    try:
        resolved = resolve_symbol_node(lib_id)
        parent = _sexpr_child_text(cast(SExprList, resolved["node"]), "extends")
        library = str(resolved.get("library") or lib_id.split(":", 1)[0])
        while parent:
            parent_lib_id = f"{library}:{parent}"
            if parent_lib_id in seen:
                break
            chain.append(parent_lib_id)
            seen.add(parent_lib_id)
            resolved = resolve_symbol_node(parent_lib_id)
            parent = _sexpr_child_text(cast(SExprList, resolved["node"]), "extends")
    except Exception:
        return chain
    return chain


def _sexpr_child_text(node: SExprList, head: str) -> str | None:
    child = node.first_child(head)
    if child is None or len(child.items) < 2:
        return None
    value = getattr(child.items[1], "value", None)
    return str(value) if value else None


def _resolve_symbol_id_alias(
    lib_id: str | None = None,
    symbol: str | None = None,
    symbol_id: str | None = None,
) -> str:
    resolved = lib_id or symbol_id or symbol
    if not resolved:
        raise KiCadLibraryError("lib_id is required")
    return str(resolved)


def _resolve_footprint_id_alias(
    footprint_id: str | None = None,
    footprint: str | None = None,
) -> str:
    resolved = footprint_id or footprint
    if not resolved:
        raise KiCadLibraryError("footprint_id is required")
    return str(resolved)


def _compact_symbol_pin(pin: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": str(pin.get("number") or ""),
        "name": str(pin.get("name") or ""),
        "pintype": str(pin.get("pintype") or ""),
    }


def _normalize_symbol_detail(detail: str) -> str:
    normalized = str(detail or "compact").strip().lower()
    if normalized not in {"compact", "pins", "full"}:
        raise ValueError("detail must be one of: compact, pins, full")
    return normalized


def _resolve_symbol_for_tool(
    lib_id: str,
    *,
    detail: str = "compact",
    include_source: bool = False,
    include_pins: bool = True,
) -> dict[str, Any]:
    normalized_detail = _normalize_symbol_detail(detail)
    result = resolve_symbol_node(lib_id)
    source = str(result.get("source") or "")
    public: dict[str, Any] = {
        "success": True,
        "lib_id": result.get("lib_id", lib_id),
        "library": result.get("library"),
        "symbol": result.get("symbol"),
        "path": result.get("path"),
        "detail": normalized_detail,
        "source_bytes": len(source.encode("utf-8")),
    }
    pins = _resolve_symbol_pins(lib_id)
    public["pin_count"] = len(pins)
    if include_pins:
        public["pins"] = (
            pins
            if normalized_detail in {"pins", "full"}
            else [_compact_symbol_pin(pin) for pin in pins]
        )
    else:
        public["pins_omitted"] = True
    public["extends_chain"] = _symbol_extends_chain(lib_id)
    should_include_source = include_source or normalized_detail == "full"
    if should_include_source:
        public["source"] = source
        public["source_omitted"] = False
    else:
        public["source_omitted"] = True
    return public


def _normalize_resolve_symbol_requests(
    lib_ids: Sequence[str] | None = None,
    symbols: Sequence[Any] | None = None,
    items: Sequence[Any] | None = None,
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for lib_id in lib_ids or []:
        requests.append({"lib_id": str(lib_id)})
    for item in [*(symbols or []), *(items or [])]:
        if isinstance(item, str):
            requests.append({"lib_id": item})
            continue
        if not isinstance(item, dict):
            requests.append({"lib_id": "", "error": "symbol entry must be a string or object"})
            continue
        resolved_lib_id = item.get("lib_id") or item.get("symbol_id") or item.get("symbol")
        requests.append(
            {
                "lib_id": str(resolved_lib_id or ""),
                "ref": item.get("ref") or item.get("reference"),
            }
        )
    return requests


def _normalize_resolve_footprint_requests(
    footprint_ids: Sequence[str] | None = None,
    footprints: Sequence[Any] | None = None,
    items: Sequence[Any] | None = None,
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for footprint_id in footprint_ids or []:
        requests.append({"footprint_id": str(footprint_id)})
    for item in [*(footprints or []), *(items or [])]:
        if isinstance(item, str):
            requests.append({"footprint_id": item})
            continue
        if not isinstance(item, dict):
            requests.append(
                {"footprint_id": "", "error": "footprint entry must be a string or object"}
            )
            continue
        resolved = item.get("footprint_id") or item.get("footprint")
        requests.append(
            {
                "footprint_id": str(resolved or ""),
                "ref": item.get("ref") or item.get("reference"),
            }
        )
    return requests


def _find_symbols_for_tool(
    query: str,
    max_results: int,
    library: str | None,
) -> dict[str, Any]:
    matches = search_symbols(query, max_results=max_results, library=library)
    return {
        "success": True,
        "query": query,
        "library": library,
        "count": len(matches),
        "matches": matches,
        "suggested_queries": _suggested_library_queries(query, "symbol") if not matches else [],
        "recommended_next_tool": "resolve_symbols" if len(matches) > 1 else "resolve_symbol",
    }


def _find_symbols_batch_for_tool(
    queries: Sequence[str],
    max_results: int,
    library: str | None,
) -> dict[str, Any]:
    requested = [str(item) for item in queries if str(item).strip()]
    batch_matches = search_symbols_batch(tuple(requested), max_results=max_results, library=library)
    results = [
        {
            "success": True,
            "query": item,
            "library": library,
            "count": len(batch_matches.get(item, [])),
            "matches": batch_matches.get(item, []),
            "suggested_queries": (
                _suggested_library_queries(item, "symbol")
                if not batch_matches.get(item, [])
                else []
            ),
            "recommended_next_tool": (
                "resolve_symbols" if len(batch_matches.get(item, [])) > 1 else "resolve_symbol"
            ),
        }
        for item in requested
    ]
    return {
        "success": bool(results),
        "queries": requested,
        "library": library,
        "result_count": len(results),
        "total_match_count": sum(int(item.get("count", 0)) for item in results),
        "results": results,
        "recommended_next_tool": "resolve_symbols",
    }


def _find_footprints_for_tool(
    query: str,
    max_results: int,
    library: str | None,
) -> dict[str, Any]:
    matches = search_footprints(query, max_results=max_results, library=library)
    return {
        "success": True,
        "query": query,
        "library": library,
        "count": len(matches),
        "matches": matches,
        "suggested_queries": _suggested_library_queries(query, "footprint") if not matches else [],
        "recommended_next_tool": "resolve_footprint",
    }


def _find_footprints_batch_for_tool(
    queries: Sequence[str],
    max_results: int,
    library: str | None,
) -> dict[str, Any]:
    requested = [str(item) for item in queries if str(item).strip()]
    results = [_find_footprints_for_tool(item, max_results, library) for item in requested]
    return {
        "success": bool(results),
        "queries": requested,
        "library": library,
        "result_count": len(results),
        "total_match_count": sum(int(item.get("count", 0)) for item in results),
        "results": results,
        "recommended_next_tool": "resolve_footprints",
    }


def _resolve_footprint_for_tool(
    footprint_id: str,
    *,
    detail: str = "compact",
    include_source: bool = False,
) -> dict[str, Any]:
    normalized_detail = str(detail or "compact").strip().lower()
    if normalized_detail not in {"compact", "full"}:
        raise ValueError("detail must be one of: compact, full")
    result = resolve_footprint_node(footprint_id)
    source = str(result.get("source") or "")
    public = {key: value for key, value in result.items() if key not in {"node", "source"}}
    public["detail"] = normalized_detail
    public["source_bytes"] = len(source.encode("utf-8"))
    should_include_source = include_source or normalized_detail == "full"
    if should_include_source:
        public["source"] = source
        public["source_omitted"] = False
    else:
        public["source_omitted"] = True
    return public


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
    """Register creation-related tools through focused modules."""
    from kicad_mcp.tools.library_lookup_tools import register_library_lookup_tools
    from kicad_mcp.tools.pcb_tools import register_pcb_tools
    from kicad_mcp.tools.project_creation_tools import register_project_creation_tools
    from kicad_mcp.tools.schematic_generation_tools import (
        register_schematic_generation_tools,
    )
    from kicad_mcp.tools.schematic_validation_tools import (
        register_schematic_validation_tools,
    )

    register_project_creation_tools(mcp)
    register_library_lookup_tools(mcp)
    register_schematic_generation_tools(mcp)
    register_schematic_validation_tools(mcp)
    register_pcb_tools(mcp)




def _preview_design_intent_netlist_first(
    project_path: str,
    intent: dict[str, Any],
    *,
    visual_style: str = "professional_blocks",
) -> dict[str, Any]:
    """Fast preview of intent structure and layout before authoritative apply."""
    from kicad_mcp.schematic_engine.intent_state import prepare_intent_for_action
    from kicad_mcp.schematic_engine.normalize import normalize_design_intent
    from kicad_mcp.schematic_engine.sheet_planner import plan_sheets
    from kicad_mcp.schematic_engine.visual_lint import visual_lint

    try:
        effective_intent, intent_action = prepare_intent_for_action(project_path, intent)
        canonical = normalize_design_intent(project_path, effective_intent)
    except (ValueError, KeyError) as e:
        return {
            "success": False,
            "tool": "schematic_preview_design_intent",
            "stage": "normalize_failed",
            "error": f"Intent normalization failed: {e}",
            "changed": False,
        }

    # Plan sheets
    sheet_plan = plan_sheets(canonical, style=visual_style)

    # Visual lint
    lint_result = visual_lint(canonical, sheet_plan)

    issues = [
        {
            "category": _preview_issue_category(issue.type, issue.severity),
            "type": issue.type,
            "ref": issue.ref,
            "message": issue.message,
            "severity": issue.severity,
        }
        for issue in lint_result.issues[:20]
    ]
    blocking_issues = [
        issue for issue in issues if issue["category"] != "visual_warning"
    ]

    return {
        "success": True,
        "tool": "schematic_preview_design_intent",
        "stage": "preview",
        "changed": False,
        "engine": "skidl_kiutils_kicad_cli",
        "intent_action": intent_action,
        "ready_to_apply": len(blocking_issues) == 0,
        "blocking_issue_count": len(blocking_issues),
        "issues": issues,
        "summary": {
            "generated_part_count": len(canonical.parts),
            "net_count": len({ep.net for ep in canonical.endpoints}),
            "sheet_count": len(sheet_plan.sheets),
            "sheets": list(sheet_plan.sheets.keys()),
            "visual_lint_blocking": lint_result.blocking_count,
            "visual_lint_warnings": lint_result.warning_count,
        },
        "verification": {
            "skidl_compile": "deferred_to_apply",
            "kicad_cli": "deferred_to_apply",
        },
        "visual_lint": {
            "blocking_count": lint_result.blocking_count,
            "warning_count": lint_result.warning_count,
            "issues": issues,
        },
        "recommended_apply_tool": "schematic_start_design_intent_job",
        "recommended_status_tool": "schematic_get_job_status",
    }


def _apply_via_netlist_first_engine(
    project_path: str,
    intent: dict[str, Any],
) -> dict[str, Any]:
    """Route design intent through the netlist-first schematic engine.

    This single production path guarantees:
    - No partial writes on failure
    - Netlist verification before commit
    - Visual lint before commit
    - Atomic commit or full rollback
    """
    from kicad_mcp.schematic_engine.pipeline import apply_design_intent_netlist_first

    result = apply_design_intent_netlist_first(
        project_path=project_path,
        intent=intent,
        export_svg=False,
    )
    result["tool"] = "schematic_apply_design_intent"
    return result


def _preview_issue_category(issue_type: str, severity: str) -> str:
    if severity == "blocking" and issue_type == "unplaced_symbol":
        return "blocking_generation_issue"
    if severity == "blocking":
        return "blocking_connectivity_issue"
    return "visual_warning"




def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _generated_schematic_report(schematic_path: str, *, run_erc: bool) -> dict[str, Any]:
    native = _native_netlist_for_tool(schematic_path)
    erc: dict[str, Any] = {"success": True, "skipped": True, "errors": 0, "warnings": 0}

    if run_erc:
        try:
            from kicad_mcp.schematic_engine.kicad_cli_verifier import KicadCliVerifier

            verified = KicadCliVerifier().verify(
                schematic_path,
                run_netlist=False,
                run_erc=True,
                export_svg=False,
            )
            erc = {
                "success": verified.erc_errors == 0,
                "skipped": False,
                "errors": verified.erc_errors,
                "warnings": verified.erc_warnings,
                "total": verified.erc_total,
                "path": verified.erc_path,
            }
        except Exception as exc:
            erc = {
                "success": False,
                "skipped": False,
                "errors": 1,
                "warnings": 0,
                "error": str(exc),
            }

    component_count = _safe_int(native.get("component_count"))
    net_count = _safe_int(native.get("net_count"))
    erc_errors = _safe_int(erc.get("errors", erc.get("total_violations")))
    blocking_counts: dict[str, int] = {}
    if not native.get("success"):
        blocking_counts["netlist_export_failed"] = 1
    if erc_errors:
        blocking_counts["erc_errors"] = erc_errors
    quality_passed = not blocking_counts

    return {
        "success": bool(native.get("success")) and (erc.get("skipped") or erc.get("success")),
        "schematic_path": schematic_path,
        "symbol_count": component_count,
        "component_count": component_count,
        "net_count": net_count,
        "quality_gate": {
            "passed": quality_passed,
            "blocking_counts": blocking_counts,
        },
        "erc": erc,
        "native_netlist": native,
        "validation_source": "skidl_kiutils_kicad_cli",
    }


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
            _generated_schematic_report(files["schematic"], run_erc=run_erc)
            if "schematic" in files
            else {"success": False, "error": "Schematic file not found"}
        )
        native = (
            schematic_report.get("native_netlist", {})
            if isinstance(schematic_report.get("native_netlist"), dict)
            else {}
        )
        erc = schematic_report.get("erc", {}) if isinstance(schematic_report.get("erc"), dict) else {}
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
        symbol_count = _safe_int(schematic_report.get("symbol_count"))
        component_count = _safe_int(native.get("component_count"))
        schematic_has_design_content = symbol_count > 0 or component_count > 0
        schematic_syntax_valid = bool(schematic_report.get("success"))
        erc_clean = bool(
            erc.get("skipped")
            or (erc.get("success") and _safe_int(erc.get("errors", erc.get("total_violations"))) == 0)
        )
        schematic_complete = bool(
            schematic_has_design_content
            and schematic_syntax_valid
            and quality_gate.get("passed")
            and native.get("success")
            and native.get("connectivity_complete", False)
            and erc_clean
        )
        pcb_synced = bool(
            pcb_quality
            and pcb_quality.get("success")
            and pcb_quality.get("net_count", 0) > 0
            and pcb_quality.get("assigned_pad_count", 0) > 0
        )
        routing_complete = bool(pcb_quality and pcb_quality.get("routing_complete", False))
        drc_clean = bool(
            drc.get("skipped") or (drc.get("success") and drc.get("total_violations", 0) == 0)
        )
        return {
            "success": True,
            "project_path": validated_project,
            "files": files,
            "status": {
                "schematic_syntax_valid": schematic_syntax_valid,
                "schematic_has_design_content": schematic_has_design_content,
                "schematic_complete": schematic_complete,
                "symbol_count": symbol_count,
                "component_count": component_count,
                "pcb_synced": pcb_synced,
                "placement_valid": bool(pcb_quality and pcb_quality.get("placement_valid", False)),
                "routing_complete": routing_complete,
                "drc_clean_or_skipped": drc_clean,
                "ready_for_pcb_sync": schematic_complete,
                "ready_for_routing": schematic_complete and pcb_synced,
                "ready_for_release": schematic_complete
                and pcb_synced
                and routing_complete
                and drc_clean,
            },
            "schematic": schematic_report,
            "native_netlist": {
                "success": native.get("success"),
                "component_count": component_count,
                "net_count": native.get("net_count"),
                "connectivity_complete": native.get("connectivity_complete"),
                "error": native.get("error"),
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
            "tools_to_avoid_now": [],
            "safe_to_continue": False,
            "debug": report,
        }
    status = report.get("status", {})
    pcb = report.get("pcb", {}) or {}
    drc = report.get("drc", {}) or {}
    schematic = report.get("schematic", {}) or {}
    native = report.get("native_netlist", {}) or {}
    symbol_count = _safe_int(status.get("symbol_count"), _safe_int(schematic.get("symbol_count")))
    component_count = _safe_int(
        status.get("component_count"),
        _safe_int(native.get("component_count")),
    )
    schematic_syntax_valid = bool(status.get("schematic_syntax_valid") or schematic.get("success"))
    blocking: list[str] = []
    if symbol_count == 0 and component_count == 0:
        stage = "empty_project"
        next_tool = "schematic_start_design_intent_job"
        next_args = {"project_path": report["project_path"]}
    elif not status.get("schematic_complete"):
        stage = "schematic_invalid"
        next_tool = "schematic_validate_generated_schematic"
        next_args = {"project_path": report["project_path"]}
        quality = report.get("schematic", {}).get("quality_gate", {})
        blocking.extend(
            f"{key}: {value}" for key, value in quality.get("blocking_counts", {}).items() if value
        )
    elif not status.get("pcb_synced"):
        stage = "schematic_valid"
        next_tool = "pcb_preview_layout_intent"
        next_args = {"project_path": report["project_path"]}
    elif not status.get("placement_valid"):
        stage = "pcb_synced"
        next_tool = "pcb_start_layout_job"
        next_args = {"project_path": report["project_path"]}
    elif pcb.get("routing_status") in {"unrouted", "partially_routed", "unknown_needs_drc"}:
        stage = "routing_needed"
        next_tool = "pcb_validate_layout"
        next_args = {"project_path": report["project_path"]}
    elif run_drc and drc.get("success") and drc.get("total_violations", 0) == 0:
        stage = "ready"
        next_tool = "project_design_state"
        next_args = {"project_path": report["project_path"], "run_drc": True}
    else:
        stage = "drc_needed"
        next_tool = "run_drc_check"
        next_args = {"project_path": report["project_path"]}
    tools_to_avoid: list[str] = []
    if stage == "empty_project":
        tools_to_avoid.append("pcb_sync_place_and_report")
    return {
        "success": True,
        "project_path": report["project_path"],
        "stage": stage,
        "blocking_issues": blocking,
        "recommended_next_tool": next_tool,
        "recommended_arguments": next_args,
        "tools_to_avoid_now": tools_to_avoid,
        "safe_to_continue": not blocking,
        "schematic_syntax_valid": schematic_syntax_valid,
        "schematic_complete": bool(status.get("schematic_complete")),
        "symbol_count": symbol_count,
        "component_count": component_count,
        "status": status,
        "summary": {
            "schematic_syntax_valid": schematic_syntax_valid,
            "schematic_complete": status.get("schematic_complete"),
            "symbol_count": symbol_count,
            "component_count": component_count,
            "pcb_synced": status.get("pcb_synced"),
            "placement_valid": status.get("placement_valid"),
            "routing_status": pcb.get("routing_status"),
        },
    }

def _next_actions_from_completion_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    actions = []
    schematic = report.get("schematic", {})
    quality_gate = schematic.get("quality_gate", {})
    blocking_counts = quality_gate.get("blocking_counts", {})
    erc = schematic.get("erc", {}) if isinstance(schematic.get("erc"), dict) else {}
    pcb = report.get("pcb", {}) or {}
    ratsnest = report.get("ratsnest", {}) or {}
    drc = report.get("drc", {}) or {}
    status = report.get("status", {})

    erc_error_count = _safe_int(erc.get("errors", erc.get("total_violations")))
    if erc_error_count > 0:
        actions.append(
            _next_action(
                "resolve_schematic_erc_errors",
                "Resolve schematic ERC errors",
                "schematic_start_design_intent_job",
                "high",
                "Generated schematic validation found ERC errors; rebuild from explicit design intent.",
                {"erc_errors": erc_error_count, "erc": erc},
            )
        )
    if not quality_gate.get("passed", False):
        actions.append(
            _next_action(
                "fix_schematic_quality_gate",
                "Validate generated schematic",
                "schematic_validate_generated_schematic",
                "high",
                "The schematic quality gate has blocking findings.",
                {"blocking_counts": blocking_counts},
            )
        )
    if not report.get("native_netlist", {}).get("connectivity_complete", False):
        actions.append(
            _next_action(
                "complete_native_netlist",
                "Rebuild schematic from intent",
                "schematic_start_design_intent_job",
                "high",
                "Native KiCad netlist extraction is incomplete; use the single netlist-first generation path.",
                {},
            )
        )
    if status.get("schematic_complete") and not status.get("pcb_synced"):
        actions.append(
            _next_action(
                "sync_pcb_from_schematic",
                "Sync PCB from schematic",
                "pcb_start_layout_job",
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
                "pcb_start_layout_job",
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
                "Review PCB ratsnest and routing status",
                "pcb_validate_layout",
                "high",
                "PCB is synced and placed, but copper routing is not complete. Use debug routing tools only for explicit route edits.",
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
    erc_warning_count = _safe_int(erc.get("warnings"))
    if erc_warning_count > 0:
        actions.append(
            _next_action(
                "optional_review_erc_warnings",
                "Review ERC warnings",
                "schematic_validate_generated_schematic",
                "low",
                "KiCad ERC reported warnings for the generated schematic.",
                {"erc_warnings": erc_warning_count, "erc": erc},
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
        atomic_write_text(schematic_path, schematic.to_text())
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
        atomic_write_text(project_path, json.dumps(_default_project_json(), indent=2))

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
            return {
                "success": False,
                "project_path": validated_project,
                "error": "No schematic file found",
            }
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
            ref: component for ref, component in components.items() if component.get("footprint")
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
                "synced_footprints": sorted(
                    set(footprint_refs) - {item["reference"] for item in missing_footprints}
                ),
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
            "routing_complete": bool(quality.get("routing_complete", False)),
            "routing_status": quality.get("routing_status", "unknown_needs_drc"),
            "completion_scope": "sync_placement_and_routing_status",
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
    if ref.startswith("D") and any(
        token in text for token in ("tvs", "esd", "smf", "sm6", "protection")
    ):
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
        return (
            board_width_mm * (0.25 + (offset % 4) * 0.18),
            board_height_mm - 12.0 - row * 10.0,
            0.0,
        )
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
            overlap_warnings.append(
                {"reference": ref, "warning": "Could not find non-overlapping placement"}
            )
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
    track_segments_by_net: dict[str, list[dict[str, Any]]] = {}
    for segment in pcb.list_track_segments():
        if segment.get("net_name"):
            track_segments_by_net.setdefault(str(segment["net_name"]), []).append(segment)
    vias_by_net: dict[str, list[dict[str, Any]]] = {}
    for via in pcb.list_vias():
        if via.get("net_name"):
            vias_by_net.setdefault(str(via["net_name"]), []).append(via)
    connections = []
    expected_connection_count = 0
    routed_net_count = 0
    for net_name, pads in sorted(pads_by_net.items()):
        if len(pads) < 2:
            routed_net_count += 1
            continue
        expected_connection_count += len(pads) - 1
        pad_components = _pad_routing_components(
            pads,
            track_segments_by_net.get(net_name, []),
            vias_by_net.get(net_name, []),
        )
        unique_components = {pad_components.get(_pad_key(pad)) for pad in pads}
        if len(unique_components) <= 1:
            routed_net_count += 1
        anchor = pads[0]
        anchor_component = pad_components.get(_pad_key(anchor))
        for pad in pads[1:]:
            if pad_components.get(_pad_key(pad)) == anchor_component:
                continue
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
        "ratsnest_type": "routed_pad_connectivity_ratsnest",
        "net_count": len(pads_by_net),
        "routed_net_count": routed_net_count,
        "expected_connection_count": expected_connection_count,
        "routed_connection_count": max(0, expected_connection_count - len(connections)),
        "unrouted_connection_count": len(connections),
        "connection_count": len(connections),
        "connections": connections,
    }


def _pad_key(pad: dict[str, Any]) -> tuple[str, str]:
    return str(pad.get("reference", "")), str(pad.get("pad", ""))


def _pad_routing_components(
    pads: list[dict[str, Any]],
    track_segments: list[dict[str, Any]],
    vias: list[dict[str, Any]],
) -> dict[tuple[str, str], tuple[int, int]]:
    parent: dict[tuple[int, int], tuple[int, int]] = {}

    def coord_key(point: dict[str, float]) -> tuple[int, int]:
        return (round(float(point["x"]) / 0.05), round(float(point["y"]) / 0.05))

    def find(node: tuple[int, int]) -> tuple[int, int]:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: tuple[int, int], b: tuple[int, int]) -> None:
        root_a = find(a)
        root_b = find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    pad_coords = {_pad_key(pad): coord_key(pad["position"]) for pad in pads}
    for node in pad_coords.values():
        find(node)
    for segment in track_segments:
        start = coord_key(segment["start"])
        end = coord_key(segment["end"])
        union(start, end)
    for via in vias:
        find(coord_key(via["position"]))
    return {pad_key: find(node) for pad_key, node in pad_coords.items()}


def _pcb_quality_report(project_path: str, pcb_path: str, pcb: KiCadPcb) -> dict[str, Any]:
    pads = pcb.footprint_pad_positions()
    assigned_pads = [pad for pad in pads if pad.get("net_name")]
    unassigned_pads = [pad for pad in pads if not pad.get("net_name")]
    ratsnest = _build_ratsnest(project_path, pcb_path, pcb)
    footprints = pcb.list_footprints()
    overlap_warnings = _footprint_overlap_warnings(footprints)
    keepout_warnings = _esp_antenna_keepout_warnings(pcb)
    track_count = len(pcb.list_track_segments())
    via_count = len(pcb.list_vias())
    unrouted_connection_count = int(ratsnest.get("connection_count", 0) or 0)
    expected_connection_count = int(ratsnest.get("expected_connection_count", 0) or 0)
    if not assigned_pads:
        routing_status = "unrouted"
        routing_confidence = "low"
    elif unrouted_connection_count == 0:
        routing_status = "routed_needs_drc" if track_count > 0 else "no_multinode_nets"
        routing_confidence = "high"
    elif track_count == 0 and unrouted_connection_count > 0:
        routing_status = "unrouted"
        routing_confidence = "medium"
    elif track_count > 0:
        routing_status = "partially_routed_needs_drc"
        routing_confidence = "medium"
    else:
        routing_status = "unknown_needs_drc"
        routing_confidence = "low"
    routing_complete = bool(assigned_pads) and unrouted_connection_count == 0
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
        "via_count": via_count,
        "routing_status": routing_status,
        "routing_complete": routing_complete,
        "routing_confidence": routing_confidence,
        "requires_drc_for_final_answer": True,
        "ratsnest_connection_count": unrouted_connection_count,
        "ratsnest_expected_connection_count": expected_connection_count,
        "ratsnest_routed_connection_count": ratsnest.get("routed_connection_count", 0),
        "overlap_warnings": overlap_warnings,
        "overlap_warning_count": len(overlap_warnings),
        "keepout_warnings": keepout_warnings,
        "keepout_warning_count": len(keepout_warnings),
        "placement_valid": not overlap_warnings and not keepout_warnings,
    }


def _footprint_overlap_warnings(footprints: list[dict[str, Any]]) -> list[dict[str, str]]:
    warnings = []
    for index, first in enumerate(footprints):
        for second in footprints[index + 1 :]:
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
        raise ValueError(f"Pads are assigned to different nets: {', '.join(sorted(assigned_nets))}")
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
        atomic_write_text(pcb_path, pcb.to_text())
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


def _apply_transactional_pcb_edit(
    pcb_path: str,
    mutator: Callable[[KiCadPcb], dict[str, Any]],
    *,
    run_cli_validation: bool = True,
    run_drc: bool = False,
    post_write_validator: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    validated_path = validate_local_path(pcb_path, "pcb", must_exist=True)
    with transactional_file_lock(validated_path):
        original_text = Path(validated_path).read_text(encoding="utf-8")
        backup = create_file_backup(validated_path)
        try:
            before_validation = validate_pcb_text(original_text)
            pcb = KiCadPcb.from_text(original_text)
            change_result = mutator(pcb)
            updated_text = pcb.to_text()
            after_validation = validate_pcb_text(updated_text)
            atomic_write_text(validated_path, updated_text)
            cli_export = (
                _validate_pcb_with_cli_export(validated_path)
                if run_cli_validation
                else {"success": True, "skipped": True, "reason": "PCB CLI validation disabled"}
            )
            if not cli_export.get("success"):
                raise ValueError(
                    cli_export.get("stderr") or cli_export.get("error") or "PCB CLI export failed"
                )
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
                raise ValueError(
                    post_write.get("error")
                    or post_write.get("reason")
                    or "PCB post-write validation failed"
                )
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
            _ = cli_path
            runner = SecureSubprocessRunner(
                path_validator=PathValidator(trusted_roots={pcb_dir, temp_dir})
            )
            process = runner.run_kicad_command(
                [
                    "pcb",
                    "export",
                    "svg",
                    "--output",
                    output_path,
                    "--layers",
                    "F.Cu,B.Cu,Edge.Cuts",
                    pcb_path,
                ],
                input_files=[pcb_path],
                output_files=[output_path],
                working_dir=pcb_dir,
                timeout=TIMEOUT_CONSTANTS["kicad_cli_export"],
            )
        except (PathValidationError, SecureSubprocessError) as exc:
            error = str(exc)
            if "timed out" in error.lower():
                error = "KiCad CLI PCB SVG export timed out"
            return {"success": False, "error": error}
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
    pcb_path = os.path.realpath(os.path.expanduser(pcb_path))
    with tempfile.TemporaryDirectory() as temp_dir:
        output_path = os.path.join(temp_dir, "drc_report.json")
        try:
            _ = cli_path
            pcb_dir = os.path.dirname(os.path.realpath(os.path.expanduser(pcb_path))) or os.getcwd()
            runner = SecureSubprocessRunner(
                path_validator=PathValidator(trusted_roots={pcb_dir, temp_dir})
            )
            process = runner.run_kicad_command(
                ["pcb", "drc", "--format", "json", "--output", output_path, pcb_path],
                input_files=[pcb_path],
                output_files=[output_path],
                working_dir=pcb_dir,
                timeout=TIMEOUT_CONSTANTS["kicad_cli_drc"],
            )
        except (PathValidationError, SecureSubprocessError) as exc:
            error = str(exc)
            if "timed out" in error.lower():
                error = "KiCad CLI PCB DRC timed out"
            return {"success": False, "error": error}
        if process.returncode != 0 and not os.path.exists(output_path):
            return {
                "success": False,
                "error": process.stderr or process.stdout or "KiCad CLI PCB DRC failed",
                "returncode": process.returncode,
            }
        report = (
            json.loads(Path(output_path).read_text(encoding="utf-8"))
            if os.path.exists(output_path)
            else {}
        )
        violations = _drc_report_violations(report)
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
