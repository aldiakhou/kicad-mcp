"""Visual layout helpers for generated schematic v2 specs."""

from __future__ import annotations

from copy import deepcopy
import math
from typing import Any

from kicad_mcp.utils.kicad_s_expr import KiCadSchematic
from kicad_mcp.utils.schematic_pins import _resolve_symbol_pins

MARGIN_MM = 38.1
COL_GAP_MM = 45.72
ROW_GAP_MM = 35.56
LABEL_MARGIN_MM = 15.24
GRID_MM = 1.27


def apply_visual_layout_to_v2_spec(
    spec: dict[str, Any],
    *,
    page: str = "A3",
    style: str = "readable",
    spacing_mm: float = 12.7,
) -> dict[str, Any]:
    """Return a v2 spec with explicit readable symbol positions and layout hints."""
    laid_out = deepcopy(spec)
    laid_out["paper"] = laid_out.get("paper") or page
    parts = [part for part in laid_out.get("parts", []) if isinstance(part, dict)]
    page_width, page_height = KiCadSchematic.PAPER_SIZES_MM.get(
        laid_out["paper"],
        KiCadSchematic.PAPER_SIZES_MM["A3"],
    )
    bounds_by_ref = {str(part.get("ref")): estimate_symbol_bounds(part) for part in parts}
    generated = [part for part in parts if part.get("generated_by")]
    primary = [part for part in parts if not part.get("generated_by")]
    ordered_primary = sorted(
        primary,
        key=lambda part: (
            _part_role_priority(part, bounds_by_ref.get(str(part.get("ref")), {})),
            -bounds_by_ref.get(str(part.get("ref")), {}).get("pin_count", 0),
            str(part.get("ref") or ""),
        ),
    )
    placements: dict[str, dict[str, float]] = {}
    current_x = MARGIN_MM
    current_y = MARGIN_MM
    row_height = 0.0
    max_x = max(page_width - MARGIN_MM, page_width * 0.8)
    for part in ordered_primary:
        ref = str(part.get("ref") or "")
        bounds = bounds_by_ref.get(ref, _fallback_bounds(part))
        width = bounds["width"] + LABEL_MARGIN_MM * 2
        height = bounds["height"] + LABEL_MARGIN_MM
        if current_x > MARGIN_MM and current_x + width > max_x:
            current_x = MARGIN_MM
            current_y += row_height + ROW_GAP_MM
            row_height = 0.0
        placements[ref] = {
            "x": _snap(current_x + width / 2.0),
            "y": _snap(current_y + height / 2.0),
            "angle": float(part.get("angle", 0.0)),
        }
        current_x += width + max(COL_GAP_MM, spacing_mm * 2.0)
        row_height = max(row_height, height)

    generated_groups = _place_generated_parts(
        generated,
        placements,
        bounds_by_ref,
        page_width,
        page_height,
        spacing_mm,
    )
    unplaced_generated = [
        part for part in generated if str(part.get("ref") or "") not in placements
    ]
    for part in unplaced_generated:
        ref = str(part.get("ref") or "")
        bounds = bounds_by_ref.get(ref, _fallback_bounds(part))
        width = bounds["width"] + LABEL_MARGIN_MM
        height = bounds["height"] + LABEL_MARGIN_MM
        if current_x > MARGIN_MM and current_x + width > max_x:
            current_x = MARGIN_MM
            current_y += row_height + ROW_GAP_MM
            row_height = 0.0
        placements[ref] = {
            "x": _snap(current_x + width / 2.0),
            "y": _snap(current_y + height / 2.0),
            "angle": float(part.get("angle", 0.0)),
        }
        current_x += width + COL_GAP_MM
        row_height = max(row_height, height)

    for part in parts:
        ref = str(part.get("ref") or "")
        placement = placements.get(ref)
        if placement:
            part["x"] = placement["x"]
            part["y"] = placement["y"]
            part["angle"] = placement["angle"]

    layout_hints = laid_out.setdefault("layout_hints", {})
    layout_hints["label_strategy"] = "external_stubs" if style == "readable" else "pin_anchor"
    layout_hints["connection_style"] = "auto"
    layout_hints["label_clearance_mm"] = max(7.62, spacing_mm)
    layout_hints["generated_groups"] = generated_groups
    layout_hints["visual_layout"] = {
        "enabled": True,
        "style": style,
        "page": laid_out["paper"],
        "placed_symbol_count": len(placements),
        "generated_group_count": len(generated_groups),
        "estimated_overlap_count": _estimated_overlap_count(parts, placements, bounds_by_ref),
        "label_strategy": layout_hints["label_strategy"],
    }
    return laid_out


def estimate_symbol_bounds(part: dict[str, Any]) -> dict[str, Any]:
    """Estimate readable symbol bounds from resolved pins or custom pins."""
    pins = _part_pins(part)
    pin_count = len(pins)
    if pins and all(isinstance(pin.get("local_position"), dict) for pin in pins):
        xs = [float(pin["local_position"].get("x", 0.0)) for pin in pins]
        ys = [float(pin["local_position"].get("y", 0.0)) for pin in pins]
        left_count = sum(1 for x in xs if x <= 0.0)
        right_count = pin_count - left_count
        return {
            "width": max(25.4, (max(xs) - min(xs)) + LABEL_MARGIN_MM * 2),
            "height": max(20.32, (max(ys) - min(ys)) + LABEL_MARGIN_MM * 2),
            "pin_count": pin_count,
            "left_pin_count": left_count,
            "right_pin_count": right_count,
        }
    side_count = max(math.ceil(pin_count / 2), 1)
    return {
        "width": max(30.48, 35.56 if pin_count >= 20 else 25.4),
        "height": max(20.32, side_count * 5.08 + LABEL_MARGIN_MM),
        "pin_count": pin_count,
        "left_pin_count": side_count,
        "right_pin_count": max(pin_count - side_count, 0),
    }


def _part_pins(part: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(part.get("pins"), list):
        return [pin for pin in part["pins"] if isinstance(pin, dict)]
    lib_id = part.get("lib_id") or part.get("symbol") or part.get("kicad_symbol")
    if not lib_id:
        return []
    try:
        return _resolve_symbol_pins(str(lib_id))
    except Exception:
        return []


def _fallback_bounds(part: dict[str, Any]) -> dict[str, Any]:
    return {"width": 25.4, "height": 20.32, "pin_count": 2, "left_pin_count": 1, "right_pin_count": 1}


def _part_role_priority(part: dict[str, Any], bounds: dict[str, Any]) -> int:
    ref = str(part.get("ref") or "")
    lib_id = str(part.get("lib_id") or part.get("symbol") or "")
    value = str(part.get("value") or "")
    if ref.startswith("#"):
        return 9
    if ref.startswith(("J", "P", "H")) or "Connector" in lib_id:
        return 6
    if ref.startswith(("U", "IC")) and bounds.get("pin_count", 0) >= 20:
        return 2
    if "Regulator" in lib_id or any(token in value.upper() for token in ("LDO", "REG", "BUCK")):
        return 1
    if ref.startswith("U"):
        return 4
    return 5


def _place_generated_parts(
    generated: list[dict[str, Any]],
    placements: dict[str, dict[str, float]],
    bounds_by_ref: dict[str, dict[str, Any]],
    page_width: float,
    page_height: float,
    spacing_mm: float,
) -> dict[str, Any]:
    groups: dict[str, Any] = {}
    by_target: dict[str, list[dict[str, Any]]] = {}
    for part in generated:
        target = part.get("target")
        if target:
            by_target.setdefault(str(target), []).append(part)
    for target, parts in by_target.items():
        target_pos = placements.get(target)
        if not target_pos:
            continue
        target_bounds = bounds_by_ref.get(target, _fallback_bounds({}))
        groups[target] = {"target": target, "parts": []}
        x = min(
            max(
                target_pos["x"] + target_bounds.get("width", 25.4) / 2.0 + max(45.72, spacing_mm * 3),
                MARGIN_MM,
            ),
            page_width - MARGIN_MM,
        )
        step_y = max(17.78, spacing_mm * 1.5)
        start_y = max(
            MARGIN_MM,
            min(
                target_pos["y"] - ((len(parts) - 1) * step_y) / 2.0,
                page_height - MARGIN_MM - max(len(parts) - 1, 0) * step_y,
            ),
        )
        for index, part in enumerate(parts):
            ref = str(part.get("ref") or "")
            y = min(
                max(start_y + index * step_y, MARGIN_MM),
                page_height - MARGIN_MM,
            )
            placements[ref] = {"x": _snap(x), "y": _snap(y), "angle": float(part.get("angle", 0.0))}
            groups[target]["parts"].append(
                {
                    "ref": ref,
                    "generated_by": part.get("generated_by"),
                    "x": placements[ref]["x"],
                    "y": placements[ref]["y"],
                }
            )
    return groups


def _target_slots(
    target_pos: dict[str, float],
    target_bounds: dict[str, Any],
    page_width: float,
    page_height: float,
    spacing_mm: float,
) -> list[dict[str, float]]:
    x = target_pos["x"]
    y = target_pos["y"]
    half_w = target_bounds.get("width", 25.4) / 2.0
    half_h = target_bounds.get("height", 20.32) / 2.0
    raw = [
        {"x": x + half_w + spacing_mm * 1.5, "y": y - half_h},
        {"x": x + half_w + spacing_mm * 1.5, "y": y},
        {"x": x + half_w + spacing_mm * 1.5, "y": y + half_h},
        {"x": x, "y": y - half_h - spacing_mm * 1.5},
        {"x": x, "y": y + half_h + spacing_mm * 1.5},
        {"x": x - half_w - spacing_mm * 1.5, "y": y + half_h},
    ]
    return [
        {
            "x": min(max(slot["x"], MARGIN_MM), page_width - MARGIN_MM),
            "y": min(max(slot["y"], MARGIN_MM), page_height - MARGIN_MM),
        }
        for slot in raw
    ]


def _estimated_overlap_count(
    parts: list[dict[str, Any]],
    placements: dict[str, dict[str, float]],
    bounds_by_ref: dict[str, dict[str, Any]],
) -> int:
    boxes = []
    for part in parts:
        ref = str(part.get("ref") or "")
        placement = placements.get(ref)
        if not placement:
            continue
        bounds = bounds_by_ref.get(ref, _fallback_bounds(part))
        boxes.append(
            (
                ref,
                (
                    placement["x"] - bounds["width"] / 2.0,
                    placement["y"] - bounds["height"] / 2.0,
                    placement["x"] + bounds["width"] / 2.0,
                    placement["y"] + bounds["height"] / 2.0,
                ),
            )
        )
    count = 0
    for index, (_, first) in enumerate(boxes):
        for _, second in boxes[index + 1 :]:
            if _rects_intersect(first, second, padding=0.0):
                count += 1
    return count


def _rects_intersect(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    *,
    padding: float = 0.0,
) -> bool:
    return not (
        first[2] + padding <= second[0]
        or first[0] - padding >= second[2]
        or first[3] + padding <= second[1]
        or first[1] - padding >= second[3]
    )


def _snap(value: float) -> float:
    return round(round(value / GRID_MM) * GRID_MM, 6)
