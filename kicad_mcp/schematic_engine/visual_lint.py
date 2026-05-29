"""Visual lint for generated schematics.

Independent from ERC - checks visual quality issues that ERC cannot detect.
A schematic can pass ERC while being visually unusable.
"""

from __future__ import annotations

import logging

from kicad_mcp.schematic_engine.models import (
    CanonicalCircuit,
    PlacementInfo,
    SheetPlan,
    VisualLintIssue,
    VisualLintResult,
)

logger = logging.getLogger(__name__)

# Visual lint check types
LINT_LABEL_INSIDE_SYMBOL = "label_inside_symbol"
LINT_LABEL_OVERLAPS_PIN = "label_overlaps_pin"
LINT_SYMBOL_OVERLAP = "symbol_overlap"
LINT_UNPLACED_SYMBOL = "unplaced_symbol"
LINT_FAR_FROM_TARGET = "generated_component_far_from_target"
LINT_DANGLING_POWER_FLAG = "dangling_power_flag"
LINT_TOO_MANY_POWER_FLAGS = "too_many_power_flags_per_net"
LINT_SHEET_OVERFLOW = "sheet_overflow"
LINT_CONNECTOR_NOT_AT_EDGE = "connector_not_at_sheet_edge"
LINT_DECOUPLING_NOT_NEAR_TARGET = "decoupling_not_near_target"

# Thresholds
MAX_DECOUPLING_DISTANCE_MM = 30.0
SYMBOL_BOUNDING_BOX_ESTIMATE_MM = 15.0
SHEET_MARGIN_MM = 10.0

# Paper sizes for overflow check
PAPER_SIZES_MM: dict[str, tuple[float, float]] = {
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
}


def visual_lint(
    canonical: CanonicalCircuit,
    sheet_plan: SheetPlan,
    *,
    check_overlap: bool = True,
    check_labels: bool = True,
    check_decoupling: bool = True,
    check_overflow: bool = True,
) -> VisualLintResult:
    """Run visual lint checks on a planned schematic.

    Args:
        canonical: The canonical circuit.
        sheet_plan: The sheet plan with placements.
        check_overlap: Check for symbol overlaps.
        check_labels: Check for label placement issues.
        check_decoupling: Check decoupling cap placement.
        check_overflow: Check for sheet boundary overflow.

    Returns:
        VisualLintResult with all issues found.
    """
    issues: list[VisualLintIssue] = []

    # Check 1: Unplaced symbols
    issues.extend(_check_unplaced_symbols(canonical, sheet_plan))

    # Check 2: Symbol overlaps
    if check_overlap:
        issues.extend(_check_symbol_overlaps(sheet_plan))

    # Check 3: Decoupling near target
    if check_decoupling:
        issues.extend(_check_decoupling_placement(canonical, sheet_plan))

    # Check 4: Sheet overflow
    if check_overflow:
        issues.extend(_check_sheet_overflow(sheet_plan))

    # Check 5: Labels inside symbols (basic check based on placement)
    if check_labels:
        issues.extend(_check_label_placement(canonical, sheet_plan))

    blocking_count = sum(1 for i in issues if i.severity == "blocking")
    warning_count = sum(1 for i in issues if i.severity == "warning")

    return VisualLintResult(
        success=blocking_count == 0,
        blocking_count=blocking_count,
        warning_count=warning_count,
        issues=issues,
    )


def _check_unplaced_symbols(
    canonical: CanonicalCircuit,
    sheet_plan: SheetPlan,
) -> list[VisualLintIssue]:
    """Check for parts in the canonical circuit without placements."""
    issues: list[VisualLintIssue] = []
    placed_refs = set(sheet_plan.placements.keys())

    for part in canonical.parts:
        if part.ref not in placed_refs:
            issues.append(VisualLintIssue(
                type=LINT_UNPLACED_SYMBOL,
                ref=part.ref,
                severity="blocking",
                message=f"Symbol {part.ref} ({part.lib_id}) has no placement",
            ))

    return issues


def _check_symbol_overlaps(
    sheet_plan: SheetPlan,
) -> list[VisualLintIssue]:
    """Check for symbol placement overlaps within each sheet."""
    issues: list[VisualLintIssue] = []

    # Group placements by sheet
    sheet_placements: dict[str, list[PlacementInfo]] = {}
    for placement in sheet_plan.placements.values():
        sheet_placements.setdefault(placement.sheet, []).append(placement)

    for sheet_name, placements in sheet_placements.items():
        for i, p1 in enumerate(placements):
            for p2 in placements[i + 1:]:
                distance = ((p1.x - p2.x) ** 2 + (p1.y - p2.y) ** 2) ** 0.5
                if distance < SYMBOL_BOUNDING_BOX_ESTIMATE_MM:
                    issues.append(VisualLintIssue(
                        type=LINT_SYMBOL_OVERLAP,
                        ref=p1.ref,
                        sheet=sheet_name,
                        severity="blocking",
                        message=(
                            f"Symbol {p1.ref} overlaps with {p2.ref} "
                            f"(distance: {distance:.1f}mm)"
                        ),
                    ))

    return issues


def _check_decoupling_placement(
    canonical: CanonicalCircuit,
    sheet_plan: SheetPlan,
) -> list[VisualLintIssue]:
    """Check that decoupling caps are placed near their target ICs."""
    issues: list[VisualLintIssue] = []

    for part in canonical.parts:
        if part.role != "decoupling":
            continue

        target_ref = part.properties.get("KICAD_MCP_TARGET", "")
        if not target_ref:
            continue

        part_placement = sheet_plan.placements.get(part.ref)
        target_placement = sheet_plan.placements.get(target_ref)

        if not part_placement or not target_placement:
            continue

        distance = (
            (part_placement.x - target_placement.x) ** 2
            + (part_placement.y - target_placement.y) ** 2
        ) ** 0.5

        if distance > MAX_DECOUPLING_DISTANCE_MM:
            issues.append(VisualLintIssue(
                type=LINT_DECOUPLING_NOT_NEAR_TARGET,
                ref=part.ref,
                sheet=part_placement.sheet,
                severity="blocking",
                message=(
                    f"Decoupling cap {part.ref} is {distance:.1f}mm from "
                    f"target {target_ref} (max: {MAX_DECOUPLING_DISTANCE_MM}mm)"
                ),
            ))

    return issues


def _check_sheet_overflow(
    sheet_plan: SheetPlan,
) -> list[VisualLintIssue]:
    """Check that all symbols are within sheet boundaries."""
    issues: list[VisualLintIssue] = []

    # Group placements by sheet
    sheet_placements: dict[str, list[PlacementInfo]] = {}
    for placement in sheet_plan.placements.values():
        sheet_placements.setdefault(placement.sheet, []).append(placement)

    for sheet_name, placements in sheet_placements.items():
        paper_size = sheet_plan.sheet_sizes.get(sheet_name, "A3")
        paper_w, paper_h = PAPER_SIZES_MM.get(paper_size, (420.0, 297.0))

        for placement in placements:
            if (
                placement.x < SHEET_MARGIN_MM
                or placement.y < SHEET_MARGIN_MM
                or placement.x > paper_w - SHEET_MARGIN_MM
                or placement.y > paper_h - SHEET_MARGIN_MM
            ):
                issues.append(VisualLintIssue(
                    type=LINT_SHEET_OVERFLOW,
                    ref=placement.ref,
                    sheet=sheet_name,
                    severity="blocking",
                    message=(
                        f"Symbol {placement.ref} at ({placement.x:.1f}, "
                        f"{placement.y:.1f}) is outside sheet bounds "
                        f"({paper_w}x{paper_h}mm)"
                    ),
                ))

    return issues


def _check_label_placement(
    canonical: CanonicalCircuit,
    sheet_plan: SheetPlan,
) -> list[VisualLintIssue]:
    """Basic check for label placement issues.

    Checks if label positions would overlap with symbol bounding boxes.
    This is a heuristic check - full check requires rendered geometry.
    """
    issues: list[VisualLintIssue] = []

    # For each net, check if label position is too close to a different symbol
    ref_set_by_sheet: dict[str, set[str]] = {}
    for sheet_name, refs in sheet_plan.sheets.items():
        ref_set_by_sheet[sheet_name] = set(refs)

    # Labels are placed at (ref.x + 15, ref.y) by convention
    # Check if any label position falls inside another symbol's bounding box
    for ep in canonical.endpoints:
        placement = sheet_plan.placements.get(ep.ref)
        if not placement:
            continue

        label_x = placement.x + 15.0
        label_y = placement.y

        # Check against all other symbols in the same sheet
        for other_ref, other_placement in sheet_plan.placements.items():
            if other_ref == ep.ref:
                continue
            if other_placement.sheet != placement.sheet:
                continue

            # Simple bounding box check
            half_box = SYMBOL_BOUNDING_BOX_ESTIMATE_MM / 2
            if (
                abs(label_x - other_placement.x) < half_box
                and abs(label_y - other_placement.y) < half_box
            ):
                issues.append(VisualLintIssue(
                    type=LINT_LABEL_INSIDE_SYMBOL,
                    ref=other_ref,
                    label=ep.net,
                    sheet=placement.sheet,
                    severity="warning",
                    message=(
                        f"Label '{ep.net}' for {ep.ref} may overlap "
                        f"symbol {other_ref}"
                    ),
                ))
                break  # Only report once per label

    return issues
