"""Visual layout helpers for generated schematic v2 specs."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Any

from kicad_mcp.utils.kicad_s_expr import KiCadSchematic
from kicad_mcp.utils.schematic_pins import _resolve_symbol_pins

MARGIN_MM = 38.1
COL_GAP_MM = 45.72
ROW_GAP_MM = 35.56
LABEL_MARGIN_MM = 15.24
GRID_MM = 1.27
PAPER_ORDER = ["A4", "A3", "A2", "A1"]


@dataclass(frozen=True)
class Rect:
    left: float
    top: float
    right: float
    bottom: float

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top

    @property
    def center_x(self) -> float:
        return (self.left + self.right) / 2.0

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2.0

    @classmethod
    def from_center(cls, x: float, y: float, width: float, height: float) -> Rect:
        return cls(x - width / 2.0, y - height / 2.0, x + width / 2.0, y + height / 2.0)

    def inflate(self, amount: float) -> Rect:
        return Rect(
            self.left - amount,
            self.top - amount,
            self.right + amount,
            self.bottom + amount,
        )

    def intersects(self, other: Rect, padding: float = 0.0) -> bool:
        return not (
            self.right + padding <= other.left
            or self.left - padding >= other.right
            or self.bottom + padding <= other.top
            or self.top - padding >= other.bottom
        )

    def contains(self, other: Rect) -> bool:
        return (
            other.left >= self.left
            and other.top >= self.top
            and other.right <= self.right
            and other.bottom <= self.bottom
        )

    def to_tuple(self) -> tuple[float, float, float, float]:
        return (self.left, self.top, self.right, self.bottom)


@dataclass
class PlacedBox:
    ref: str
    rect: Rect
    kind: str = "symbol"


@dataclass
class LayoutCanvas:
    paper: str
    width_mm: float
    height_mm: float
    margin_mm: float
    title_block_reserved_rect: Rect | None
    occupied: list[PlacedBox]

    @property
    def usable_rect(self) -> Rect:
        return Rect(
            self.margin_mm,
            self.margin_mm,
            self.width_mm - self.margin_mm,
            self.height_mm - self.margin_mm,
        )

    def can_place(self, rect: Rect, *, ignore_ref: str | None = None) -> bool:
        if not self.usable_rect.contains(rect):
            return False
        if self.title_block_reserved_rect and rect.intersects(self.title_block_reserved_rect):
            return False
        return not any(
            box.ref != ignore_ref and rect.intersects(box.rect)
            for box in self.occupied
        )

    def add(self, ref: str, rect: Rect, kind: str = "symbol") -> None:
        self.occupied.append(PlacedBox(ref=ref, rect=rect, kind=kind))


@dataclass
class _LayoutResult:
    success: bool
    placements: dict[str, dict[str, float]]
    boxes: dict[str, Rect]
    generated_groups: dict[str, Any]
    unplaced_refs: list[str]
    overflow_reason: str | None = None


def apply_visual_layout_to_v2_spec(
    spec: dict[str, Any],
    *,
    page: str = "A3",
    style: str = "readable",
    spacing_mm: float = 12.7,
) -> dict[str, Any]:
    """Return a v2 spec with explicit readable symbol positions and layout hints."""
    laid_out = deepcopy(spec)
    layout_hints = laid_out.setdefault("layout_hints", {})
    requested_paper = str(laid_out.get("paper") or page)
    fixed_paper = bool(layout_hints.get("fixed_paper", False))
    paper_strategy = str(layout_hints.get("paper_strategy") or ("fixed" if fixed_paper else "auto"))
    max_paper = str(layout_hints.get("max_paper") or "A1")
    candidate_papers = _candidate_papers(requested_paper, paper_strategy, max_paper)
    if fixed_paper:
        candidate_papers = [requested_paper]

    parts = [part for part in laid_out.get("parts", []) if isinstance(part, dict)]
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

    attempts = []
    result: _LayoutResult | None = None
    for attempt_index, paper in enumerate(candidate_papers):
        attempt_spacing = spacing_mm + attempt_index * 2.54
        page_width, page_height = KiCadSchematic.PAPER_SIZES_MM.get(
            paper,
            KiCadSchematic.PAPER_SIZES_MM["A3"],
        )
        candidate = _layout_on_canvas(
            paper,
            page_width,
            page_height,
            ordered_primary,
            generated,
            bounds_by_ref,
            attempt_spacing,
        )
        attempts.append(
            {
                "paper": paper,
                "success": candidate.success,
                "unplaced_refs": candidate.unplaced_refs,
                "overflow_reason": candidate.overflow_reason,
            }
        )
        if candidate.success:
            result = candidate
            laid_out["paper"] = paper
            break
    if result is None:
        result = candidate
        laid_out["paper"] = candidate_papers[-1] if candidate_papers else requested_paper

    for part in parts:
        ref = str(part.get("ref") or "")
        placement = result.placements.get(ref)
        if placement:
            part["x"] = placement["x"]
            part["y"] = placement["y"]
            part["angle"] = placement["angle"]

    layout_hints["label_strategy"] = "external_stubs" if style == "readable" else "pin_anchor"
    layout_hints["connection_style"] = "auto"
    layout_hints["label_clearance_mm"] = max(7.62, spacing_mm)
    layout_hints["generated_groups"] = result.generated_groups
    layout_hints["visual_layout"] = {
        "enabled": result.success,
        "style": style,
        "page": laid_out["paper"],
        "placed_symbol_count": len(result.placements),
        "generated_group_count": len(result.generated_groups),
        "estimated_overlap_count": _estimated_overlap_count_from_boxes(result.boxes),
        "unplaced_refs": result.unplaced_refs,
        "attempts": attempts,
        "paper_strategy": paper_strategy,
        "label_strategy": layout_hints["label_strategy"],
    }
    if not result.success:
        layout_hints["visual_layout"]["layout_failed"] = True
        layout_hints["visual_layout"]["reason"] = result.overflow_reason or "layout did not fit"
    return laid_out


def _candidate_papers(requested: str, strategy: str, max_paper: str) -> list[str]:
    if strategy != "auto":
        return [requested]
    if requested not in PAPER_ORDER:
        requested = "A4" if requested in KiCadSchematic.PAPER_SIZES_MM else "A3"
    if max_paper not in PAPER_ORDER:
        max_paper = "A1"
    start = PAPER_ORDER.index(requested)
    end = PAPER_ORDER.index(max_paper)
    if end < start:
        end = start
    return PAPER_ORDER[start : end + 1]


def _layout_on_canvas(
    paper: str,
    page_width: float,
    page_height: float,
    primary: list[dict[str, Any]],
    generated: list[dict[str, Any]],
    bounds_by_ref: dict[str, dict[str, Any]],
    spacing_mm: float,
) -> _LayoutResult:
    canvas = LayoutCanvas(
        paper=paper,
        width_mm=page_width,
        height_mm=page_height,
        margin_mm=min(MARGIN_MM, page_width * 0.11, page_height * 0.14),
        title_block_reserved_rect=_title_block_rect(paper, page_width, page_height),
        occupied=[],
    )
    placements: dict[str, dict[str, float]] = {}
    boxes: dict[str, Rect] = {}
    cursor_x = canvas.usable_rect.left
    cursor_y = canvas.usable_rect.top
    row_height = 0.0
    gap_x = max(25.4, spacing_mm * 1.5)

    for part in primary:
        ref = str(part.get("ref") or "")
        width, height = _occupied_size(part, bounds_by_ref.get(ref, _fallback_bounds(part)), spacing_mm)
        placed = False
        attempts = 0
        while attempts < 500:
            attempts += 1
            if cursor_x + width > canvas.usable_rect.right:
                cursor_x = canvas.usable_rect.left
                cursor_y += row_height + ROW_GAP_MM
                row_height = 0.0
            if cursor_y + height > canvas.usable_rect.bottom:
                return _LayoutResult(
                    success=False,
                    placements=placements,
                    boxes=boxes,
                    generated_groups={},
                    unplaced_refs=[ref, *[str(item.get("ref") or "") for item in generated]],
                    overflow_reason=f"{ref} would exceed {paper} usable page height",
                )
            center_x = _snap(cursor_x + width / 2.0)
            center_y = _snap(cursor_y + height / 2.0)
            rect = Rect.from_center(center_x, center_y, width, height)
            if canvas.can_place(rect):
                placements[ref] = {
                    "x": center_x,
                    "y": center_y,
                    "angle": float(part.get("angle", 0.0)),
                }
                boxes[ref] = rect
                canvas.add(ref, rect)
                cursor_x += width + gap_x
                row_height = max(row_height, height)
                placed = True
                break
            cursor_x += GRID_MM * 4.0
        if not placed:
            return _LayoutResult(
                success=False,
                placements=placements,
                boxes=boxes,
                generated_groups={},
                unplaced_refs=[ref, *[str(item.get("ref") or "") for item in generated]],
                overflow_reason=f"{ref} could not find a collision-free slot on {paper}",
            )

    generated_groups, unplaced = _place_generated_parts(
        generated,
        placements,
        boxes,
        canvas,
        bounds_by_ref,
        spacing_mm,
    )
    if unplaced:
        lane_groups, still_unplaced = _place_unassigned_parts_in_lane(
            [part for part in generated if str(part.get("ref") or "") in set(unplaced)],
            placements,
            boxes,
            canvas,
            bounds_by_ref,
            spacing_mm,
        )
        for target, group in lane_groups.items():
            generated_groups.setdefault(target, {"target": target, "parts": []})
            generated_groups[target]["parts"].extend(group["parts"])
        unplaced = still_unplaced
    return _LayoutResult(
        success=not unplaced,
        placements=placements,
        boxes=boxes,
        generated_groups=generated_groups,
        unplaced_refs=unplaced,
        overflow_reason="generated parts did not fit" if unplaced else None,
    )


def _title_block_rect(paper: str, page_width: float, page_height: float) -> Rect | None:
    if paper not in {"A4", "A3", "A2", "A1"}:
        return None
    width = 90.0 if paper in {"A4", "A3"} else 120.0
    height = 32.0 if paper in {"A4", "A3"} else 40.0
    return Rect(page_width - width - 5.0, page_height - height - 5.0, page_width - 5.0, page_height - 5.0)


def _occupied_size(part: dict[str, Any], bounds: dict[str, Any], clearance_mm: float) -> tuple[float, float]:
    ref = str(part.get("ref") or "")
    generated = bool(part.get("generated_by"))
    label_halo = LABEL_MARGIN_MM / 2.0 if generated else LABEL_MARGIN_MM
    property_halo = 10.16 if ref.startswith("#") else 15.24
    width = bounds["width"] + label_halo * 2.0 + max(clearance_mm, 7.62)
    height = bounds["height"] + property_halo + max(clearance_mm, 7.62)
    return width, height


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
    boxes: dict[str, Rect],
    canvas: LayoutCanvas,
    bounds_by_ref: dict[str, dict[str, Any]],
    spacing_mm: float,
) -> tuple[dict[str, Any], list[str]]:
    groups: dict[str, Any] = {}
    unplaced: list[str] = []
    by_target: dict[str, list[dict[str, Any]]] = {}
    for part in generated:
        target = part.get("target")
        if target:
            by_target.setdefault(str(target), []).append(part)
    for target, parts in by_target.items():
        target_pos = placements.get(target)
        if not target_pos:
            continue
        target_box = boxes.get(target)
        if target_box is None:
            continue
        groups[target] = {"target": target, "parts": []}
        for index, part in enumerate(parts):
            ref = str(part.get("ref") or "")
            width, height = _occupied_size(part, bounds_by_ref.get(ref, _fallback_bounds(part)), spacing_mm)
            placed = False
            for slot in _target_slots(target_box, width, height, spacing_mm, index):
                rect = Rect.from_center(slot["x"], slot["y"], width, height)
                if not canvas.can_place(rect):
                    continue
                placements[ref] = {
                    "x": _snap(slot["x"]),
                    "y": _snap(slot["y"]),
                    "angle": float(part.get("angle", 0.0)),
                }
                boxes[ref] = rect
                canvas.add(ref, rect, kind="generated")
                groups[target]["parts"].append(
                    {
                        "ref": ref,
                        "generated_by": part.get("generated_by"),
                        "x": placements[ref]["x"],
                        "y": placements[ref]["y"],
                    }
                )
                placed = True
                break
            if not placed:
                unplaced.append(ref)
    for part in generated:
        ref = str(part.get("ref") or "")
        if ref not in placements and ref not in unplaced:
            unplaced.append(ref)
    return groups, unplaced


def _target_slots(
    target_box: Rect,
    part_width: float,
    part_height: float,
    spacing_mm: float,
    index: int,
) -> list[dict[str, float]]:
    slots: list[dict[str, float]] = []
    step_y = max(part_height + spacing_mm, 17.78)
    step_x = max(part_width + spacing_mm, 17.78)
    ordinal = index + 1
    offsets_y = [0.0]
    offsets_x = [0.0]
    for step in range(1, 8):
        offsets_y.extend([-step * step_y, step * step_y])
        offsets_x.extend([-step * step_x, step * step_x])
    y_offset = offsets_y[index % len(offsets_y)]
    x_offset = offsets_x[index % len(offsets_x)]
    for ring in range(0, 8):
        distance = spacing_mm * (1.5 + ring) + (ordinal // 6) * spacing_mm
        slots.extend(
            [
                {
                    "x": _snap(target_box.right + distance + part_width / 2.0),
                    "y": _snap(target_box.center_y + y_offset),
                },
                {
                    "x": _snap(target_box.left - distance - part_width / 2.0),
                    "y": _snap(target_box.center_y + y_offset),
                },
                {
                    "x": _snap(target_box.center_x + x_offset),
                    "y": _snap(target_box.bottom + distance + part_height / 2.0),
                },
                {
                    "x": _snap(target_box.center_x + x_offset),
                    "y": _snap(target_box.top - distance - part_height / 2.0),
                },
            ]
        )
    return slots


def _place_unassigned_parts_in_lane(
    parts: list[dict[str, Any]],
    placements: dict[str, dict[str, float]],
    boxes: dict[str, Rect],
    canvas: LayoutCanvas,
    bounds_by_ref: dict[str, dict[str, Any]],
    spacing_mm: float,
) -> tuple[dict[str, Any], list[str]]:
    groups: dict[str, Any] = {}
    unplaced: list[str] = []
    for part in parts:
        ref = str(part.get("ref") or "")
        width, height = _occupied_size(part, bounds_by_ref.get(ref, _fallback_bounds(part)), spacing_mm)
        placed = False
        y = canvas.usable_rect.top
        while y + height <= canvas.usable_rect.bottom and not placed:
            x = canvas.usable_rect.left
            while x + width <= canvas.usable_rect.right:
                center_x = _snap(x + width / 2.0)
                center_y = _snap(y + height / 2.0)
                rect = Rect.from_center(center_x, center_y, width, height)
                if canvas.can_place(rect):
                    placements[ref] = {
                        "x": center_x,
                        "y": center_y,
                        "angle": float(part.get("angle", 0.0)),
                    }
                    boxes[ref] = rect
                    canvas.add(ref, rect, kind="generated_lane")
                    target = str(part.get("target") or "lane")
                    groups.setdefault(target, {"target": target, "parts": []})
                    groups[target]["parts"].append(
                        {
                            "ref": ref,
                            "generated_by": part.get("generated_by"),
                            "x": center_x,
                            "y": center_y,
                        }
                    )
                    placed = True
                    break
                x += max(GRID_MM * 4.0, width / 4.0)
            y += max(GRID_MM * 4.0, height / 4.0)
        if not placed:
            unplaced.append(ref)
    return groups, unplaced


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


def _estimated_overlap_count_from_boxes(boxes: dict[str, Rect]) -> int:
    refs_and_boxes = list(boxes.items())
    count = 0
    for index, (_, first) in enumerate(refs_and_boxes):
        for _, second in refs_and_boxes[index + 1 :]:
            if first.intersects(second):
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
