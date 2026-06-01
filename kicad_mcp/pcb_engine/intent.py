"""PCB layout intent schema and normalization."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

PCB_INTENT_SCHEMA: dict[str, Any] = {
    "overview": {
        "description": "PCB layout intent syncs footprints from the committed schematic, places them, optionally routes, and reports ratsnest status.",
        "workflow": [
            "pcb_preview_layout_intent",
            "pcb_start_layout_job",
            "pcb_get_layout_job_status until terminal",
            "pcb_get_layout_job_result",
            "pcb_validate_layout",
        ],
        "recovery_note": "If schematic generation produced candidate_schematic_artifacts, first promote one with schematic_export_candidate_to_project, then run PCB preview/layout.",
    },
    "board": {
        "description": "Physical board constraints.",
        "fields": {
            "width_mm": "Board width in millimeters.",
            "height_mm": "Board height in millimeters.",
            "shape": "Only rectangular is currently supported.",
        },
        "example": {"width_mm": 60.0, "height_mm": 40.0, "shape": "rectangular"},
    },
    "placement": {
        "description": "Functional placement constraints for footprints synced from schematic.",
        "fields": {
            "style": "functional or grid.",
            "preserve_existing_placement": "Keep existing footprint coordinates when syncing.",
            "components": "Optional explicit per-reference coordinates.",
            "rules": "Optional references/roles placement-rules map.",
        },
        "example": {
            "style": "functional",
            "preserve_existing_placement": True,
            "components": [
                {"ref": "J1", "x": 5.0, "y": 20.0, "angle": 90},
                {"ref": "U1", "x": 30.0, "y": 20.0, "angle": 0},
            ],
            "rules": {
                "roles": {
                    "connector": {"x": 8.0, "y": 35.0, "angle": 0},
                    "primary_controller": {"x": 30.0, "y": 20.0, "angle": 0},
                }
            },
        },
    },
    "routing": {
        "description": "Routing scope for the async PCB layout job.",
        "capability_notes": [
            "mode=report_only does not write routes; it reports ratsnest/topology after sync and placement.",
            "mode=auto uses the bounded obstacle-aware grid router for ordinary point-to-point copper on one selected layer.",
            "The router is not an RF, impedance, differential-pair, length-tuning, or dense mixed-signal signoff router.",
            "Vias, advanced layer changes, and high-density escape routing require manual or external routing.",
        ],
        "fields": {
            "mode": "none, report_only, or auto. auto runs the bounded obstacle-aware grid router.",
            "layer": "Copper layer to use for auto routing. Default: F.Cu.",
            "track_width_mm": "Track width for auto routing.",
            "clearance_mm": "Minimum routing keepout clearance around footprints and other-net copper.",
            "grid_mm": "Routing grid pitch. Smaller values can route tighter designs but take longer.",
            "max_connections": "Optional cap on routed ratsnest connections in one job. Use 0 or omit for no cap.",
        },
        "example": {
            "mode": "auto",
            "layer": "F.Cu",
            "track_width_mm": 0.25,
            "clearance_mm": 0.35,
            "grid_mm": 1.27,
        },
    },
    "validation": {
        "description": "Validation requested inside the async layout job.",
        "fields": {
            "run_drc": "Run KiCad CLI DRC inside the layout job.",
            "require_clean_drc": "Fail the job if DRC reports violations.",
        },
        "example": {"run_drc": False, "require_clean_drc": False},
    },
    "fabrication": {
        "description": "Defaults used by pcb_export_fabrication_package.",
        "fields": {
            "include_step": "Export a STEP model.",
            "include_ipc2581": "Export IPC-2581.",
            "run_drc": "Run DRC before generating fabrication artifacts.",
        },
        "example": {"include_step": False, "include_ipc2581": False, "run_drc": True},
    },
    "full_example": {
        "description": "Medium board example using sync, functional placement, ratsnest report, and no automatic routing.",
        "example": {
            "board": {"width_mm": 70.0, "height_mm": 45.0, "shape": "rectangular"},
            "placement": {
                "style": "functional",
                "preserve_existing_placement": True,
                "components": [
                    {"ref": "J1", "x": 6.0, "y": 22.0, "angle": 90},
                    {"ref": "U1", "x": 35.0, "y": 22.0, "angle": 0},
                    {"ref": "J2", "x": 64.0, "y": 22.0, "angle": 270},
                ],
            },
            "routing": {"mode": "report_only", "layer": "F.Cu"},
            "validation": {"run_drc": False, "require_clean_drc": False},
        },
    },
}


def pcb_design_intent_schema(section: str = "all") -> dict[str, Any]:
    """Return PCB layout intent schema guidance for agents."""
    normalized = str(section or "all").strip().lower()
    if normalized == "all":
        return {
            "success": True,
            "section": "all",
            "schemas": deepcopy(PCB_INTENT_SCHEMA),
            "recommended_preview_tool": "pcb_preview_layout_intent",
            "recommended_apply_tool": "pcb_start_layout_job",
            "recommended_status_tool": "pcb_get_layout_job_status",
            "recommended_result_tool": "pcb_get_layout_job_result",
        }
    if normalized in PCB_INTENT_SCHEMA:
        return {
            "success": True,
            "section": normalized,
            "schema": deepcopy(PCB_INTENT_SCHEMA[normalized]),
            "recommended_preview_tool": "pcb_preview_layout_intent",
            "recommended_apply_tool": "pcb_start_layout_job",
        }
    return {
        "success": False,
        "section": normalized,
        "error": "unknown PCB layout-intent schema section",
        "available_sections": sorted(PCB_INTENT_SCHEMA),
    }


def normalize_pcb_layout_intent(intent: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize PCB layout intent into internal options."""
    source = intent or {}
    board = source.get("board") if isinstance(source.get("board"), dict) else {}
    placement = source.get("placement") if isinstance(source.get("placement"), dict) else {}
    routing = source.get("routing") if isinstance(source.get("routing"), dict) else {}
    validation = source.get("validation") if isinstance(source.get("validation"), dict) else {}
    fabrication = source.get("fabrication") if isinstance(source.get("fabrication"), dict) else {}

    width = _positive_float(
        board.get("width_mm", source.get("board_width_mm", 80.0)),
        "board.width_mm",
    )
    height = _positive_float(
        board.get("height_mm", source.get("board_height_mm", 50.0)),
        "board.height_mm",
    )
    style = str(placement.get("style", source.get("placement_style", "functional"))).strip().lower()
    if style not in {"functional", "grid"}:
        raise ValueError("placement.style must be one of: functional, grid")

    routing_mode = str(routing.get("mode", "none")).strip().lower()
    if routing_mode not in {"none", "report_only", "auto"}:
        raise ValueError("routing.mode must be one of: none, report_only, auto")
    layer = str(routing.get("layer", "F.Cu")).strip() or "F.Cu"
    track_width = _positive_float(routing.get("track_width_mm", 0.25), "routing.track_width_mm")
    clearance = _nonnegative_float(routing.get("clearance_mm", 0.35), "routing.clearance_mm")
    grid = _positive_float(routing.get("grid_mm", 1.27), "routing.grid_mm")
    max_connections = routing.get("max_connections")
    if max_connections is not None:
        try:
            max_connections = int(max_connections)
        except (TypeError, ValueError) as exc:
            raise ValueError("routing.max_connections must be an integer") from exc
        if max_connections < 0:
            raise ValueError("routing.max_connections must be non-negative")
        if max_connections == 0:
            max_connections = None

    placement_rules = _merge_placement_rules(
        placement.get("rules"),
        source.get("placement_rules"),
        placement.get("components"),
    )
    return {
        "board": {
            "width_mm": width,
            "height_mm": height,
            "shape": str(board.get("shape", "rectangular")),
        },
        "placement": {
            "style": style,
            "preserve_existing_placement": bool(
                placement.get("preserve_existing_placement", True)
            ),
            "rules": placement_rules,
        },
        "routing": {
            "mode": routing_mode,
            "layer": layer,
            "track_width_mm": track_width,
            "clearance_mm": clearance,
            "grid_mm": grid,
            "max_connections": max_connections,
        },
        "validation": {
            "run_drc": bool(validation.get("run_drc", False)),
            "require_clean_drc": bool(validation.get("require_clean_drc", False)),
        },
        "fabrication": {
            "include_step": bool(fabrication.get("include_step", False)),
            "include_ipc2581": bool(fabrication.get("include_ipc2581", False)),
            "run_drc": bool(fabrication.get("run_drc", True)),
        },
    }


def _merge_placement_rules(*sources: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {"references": {}, "roles": {}}
    for source in sources:
        if isinstance(source, dict):
            references = source.get("references", {})
            roles = source.get("roles", {})
            if isinstance(references, dict):
                merged["references"].update(references)
            if isinstance(roles, dict):
                merged["roles"].update(roles)
            for key, value in source.items():
                if key not in {"references", "roles"} and isinstance(value, dict):
                    merged["references"][key] = value
        elif isinstance(source, list):
            for item in source:
                if not isinstance(item, dict):
                    continue
                ref = item.get("ref") or item.get("reference")
                if ref and "x" in item and "y" in item:
                    merged["references"][str(ref)] = {
                        "x": item["x"],
                        "y": item["y"],
                        "angle": item.get("angle", 0.0),
                    }
    return merged


def _positive_float(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if number <= 0:
        raise ValueError(f"{field} must be positive")
    return number


def _nonnegative_float(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if number < 0:
        raise ValueError(f"{field} must be zero or positive")
    return number
