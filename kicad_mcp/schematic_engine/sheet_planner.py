"""Sheet planner with block-based placement.

Distributes parts across functional sheets and plans symbol placement
with professional visual rules.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from kicad_mcp.schematic_engine.models import (
    CanonicalCircuit,
    CircuitPart,
    PlacementInfo,
    SheetPlan,
)

logger = logging.getLogger(__name__)

# Schematic grid size in mm
GRID_MM = 1.27

# Sheet-level placement parameters (mm)
SHEET_MARGIN_X = 25.0
SHEET_MARGIN_Y = 25.0
BLOCK_GAP_X = 50.0
BLOCK_GAP_Y = 40.0
SYMBOL_GAP_X = 30.0
SYMBOL_GAP_Y = 25.0
DECOUPLING_OFFSET_X = 10.0
DECOUPLING_OFFSET_Y = 15.0

# Paper sizes in mm (width, height)
PAPER_SIZES: dict[str, tuple[float, float]] = {
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
    "A0": (1189.0, 841.0),
    "USLetter": (279.4, 215.9),
}

# Default block assignment rules
DEFAULT_BLOCK_PATTERNS: dict[str, dict[str, Any]] = {
    "power": {
        "ref_patterns": [r"^J.*PWR", r"^J.*USB", r"^U.*LDO", r"^U.*REG", r"^FB"],
        "role_patterns": ["usb_c_power", "ferrite", "ldo", "regulator"],
        "flow": "left_to_right",
    },
    "mcu": {
        "ref_patterns": [r"^U1$", r"^Y", r"^SW.*RST", r"^SW.*BOOT"],
        "role_patterns": ["mcu", "crystal", "reset_button", "boot"],
        "flow": "center",
    },
    "sensors": {
        "ref_patterns": [r"^U[2-9]", r"^U1[0-9]"],
        "role_patterns": ["sensor", "imu", "barometer", "magnetometer"],
        "flow": "vertical_stack",
    },
    "interfaces": {
        "ref_patterns": [r"^J(?!.*PWR|.*USB)", r"^U.*PCA", r"^U.*LEVEL"],
        "role_patterns": ["level_shifter", "header", "connector", "interface"],
        "flow": "left_to_right",
    },
}

# Maximum parts per sheet before splitting
MAX_PARTS_PER_SHEET = 40


def plan_sheets(
    canonical: CanonicalCircuit,
    *,
    style: str = "professional_blocks",
    max_parts_per_sheet: int = MAX_PARTS_PER_SHEET,
    paper_size: str = "A3",
    block_rules: dict[str, dict[str, Any]] | None = None,
) -> SheetPlan:
    """Plan schematic sheet distribution and symbol placement.

    Args:
        canonical: The canonical circuit.
        style: Layout style ("professional_blocks", "single_sheet", "auto").
        max_parts_per_sheet: Maximum parts before creating a new sheet.
        paper_size: Default paper size.
        block_rules: Custom block assignment rules.

    Returns:
        SheetPlan with sheets, placements, and net routing info.
    """
    rules = block_rules or DEFAULT_BLOCK_PATTERNS

    # Step 1: Assign parts to blocks
    block_assignments = _assign_parts_to_blocks(canonical, rules)

    # Step 2: Decide sheet strategy
    if style == "single_sheet" or len(canonical.parts) <= max_parts_per_sheet:
        sheets = {"root": [p.ref for p in canonical.parts]}
    else:
        sheets = _distribute_to_sheets(block_assignments, canonical, max_parts_per_sheet)

    # Step 3: Determine cross-sheet nets
    cross_sheet_nets = _find_cross_sheet_nets(sheets, canonical)

    # Step 4: Determine local nets per sheet
    local_nets = _find_local_nets(sheets, canonical, cross_sheet_nets)

    # Step 5: Plan placements
    placements = _plan_placements(sheets, canonical, paper_size, rules)

    # Step 6: Determine sheet sizes
    sheet_sizes = _determine_sheet_sizes(sheets, paper_size)

    return SheetPlan(
        sheets=sheets,
        placements=placements,
        sheet_sizes=sheet_sizes,
        cross_sheet_nets=cross_sheet_nets,
        local_nets=local_nets,
    )


def _assign_parts_to_blocks(
    canonical: CanonicalCircuit,
    rules: dict[str, dict[str, Any]],
) -> dict[str, list[str]]:
    """Assign parts to functional blocks based on rules and intent metadata."""
    assignments: dict[str, list[str]] = {}
    assigned_refs: set[str] = set()

    # First pass: use block assignments from canonical if available
    for block_name, refs in canonical.blocks.items():
        if block_name != "default":
            assignments.setdefault(block_name, []).extend(refs)
            assigned_refs.update(refs)

    # Second pass: use rules to assign unassigned parts
    for part in canonical.parts:
        if part.ref in assigned_refs:
            continue

        matched_block = _match_part_to_block(part, rules)
        if matched_block:
            assignments.setdefault(matched_block, []).append(part.ref)
            assigned_refs.add(part.ref)

    # Third pass: assign remaining to "default" block
    for part in canonical.parts:
        if part.ref not in assigned_refs:
            assignments.setdefault("default", []).append(part.ref)

    return assignments


def _match_part_to_block(
    part: CircuitPart,
    rules: dict[str, dict[str, Any]],
) -> str | None:
    """Match a part to a block using pattern rules."""
    for block_name, block_rules in rules.items():
        # Check role patterns
        role_patterns = block_rules.get("role_patterns", [])
        if part.role and any(pat in (part.role or "").lower() for pat in role_patterns):
            return block_name

        # Check ref patterns
        ref_patterns = block_rules.get("ref_patterns", [])
        for pattern in ref_patterns:
            if re.match(pattern, part.ref):
                return block_name

    return None


def _distribute_to_sheets(
    block_assignments: dict[str, list[str]],
    canonical: CanonicalCircuit,
    max_parts_per_sheet: int,
) -> dict[str, list[str]]:
    """Distribute blocks to sheets, splitting large blocks."""
    sheets: dict[str, list[str]] = {}

    for block_name, refs in block_assignments.items():
        sheet_name = block_name if block_name != "default" else "misc"

        # Include decoupling/support parts near their target
        full_refs = list(refs)
        for part in canonical.parts:
            if part.ref not in refs:
                target = part.properties.get("KICAD_MCP_TARGET", "")
                if target in refs and part.ref not in full_refs:
                    full_refs.append(part.ref)

        if len(full_refs) <= max_parts_per_sheet:
            sheets[sheet_name] = full_refs
        else:
            # Split into sub-sheets
            for i in range(0, len(full_refs), max_parts_per_sheet):
                chunk = full_refs[i:i + max_parts_per_sheet]
                suffix = f"_{i // max_parts_per_sheet + 1}" if i > 0 else ""
                sheets[f"{sheet_name}{suffix}"] = chunk

    return sheets


def _find_cross_sheet_nets(
    sheets: dict[str, list[str]],
    canonical: CanonicalCircuit,
) -> set[str]:
    """Find nets that span multiple sheets."""
    net_sheets: dict[str, set[str]] = {}

    for sheet_name, refs in sheets.items():
        ref_set = set(refs)
        for ep in canonical.endpoints:
            if ep.ref in ref_set:
                net_sheets.setdefault(ep.net, set()).add(sheet_name)

    # Also include power rails as cross-sheet
    cross_sheet = {net for net, sheet_set in net_sheets.items() if len(sheet_set) > 1}
    cross_sheet.update(canonical.rails)

    return cross_sheet


def _find_local_nets(
    sheets: dict[str, list[str]],
    canonical: CanonicalCircuit,
    cross_sheet_nets: set[str],
) -> dict[str, set[str]]:
    """Find nets that are local to each sheet."""
    local_nets: dict[str, set[str]] = {}

    for sheet_name, refs in sheets.items():
        ref_set = set(refs)
        sheet_nets: set[str] = set()
        for ep in canonical.endpoints:
            if ep.ref in ref_set and ep.net not in cross_sheet_nets:
                sheet_nets.add(ep.net)
        local_nets[sheet_name] = sheet_nets

    return local_nets


def _plan_placements(
    sheets: dict[str, list[str]],
    canonical: CanonicalCircuit,
    paper_size: str,
    rules: dict[str, dict[str, Any]],
) -> dict[str, PlacementInfo]:
    """Plan symbol placements within each sheet."""
    placements: dict[str, PlacementInfo] = {}
    paper_w, paper_h = PAPER_SIZES.get(paper_size, PAPER_SIZES["A3"])

    for sheet_name, refs in sheets.items():
        flow = _get_block_flow(sheet_name, rules)
        sheet_placements = _place_on_sheet(
            refs, canonical, sheet_name, paper_w, paper_h, flow
        )
        placements.update(sheet_placements)

    return placements


def _get_block_flow(sheet_name: str, rules: dict[str, dict[str, Any]]) -> str:
    """Get the flow direction for a sheet/block."""
    for block_name, block_rules in rules.items():
        if block_name in sheet_name:
            return block_rules.get("flow", "left_to_right")
    return "left_to_right"


def _place_on_sheet(
    refs: list[str],
    canonical: CanonicalCircuit,
    sheet_name: str,
    paper_w: float,
    paper_h: float,
    flow: str,
) -> dict[str, PlacementInfo]:
    """Place symbols on a single sheet according to flow rules."""
    placements: dict[str, PlacementInfo] = {}
    usable_w = paper_w - 2 * SHEET_MARGIN_X
    usable_h = paper_h - 2 * SHEET_MARGIN_Y

    # Separate primary parts from support parts
    primary_refs = []
    support_refs = []
    for ref in refs:
        part = canonical.part_by_ref(ref)
        if part and part.role in (
            "decoupling", "load_capacitor", "cc_pulldown",
            "pullup", "pulldown", "current_limit",
        ):
            support_refs.append(ref)
        else:
            primary_refs.append(ref)

    # Place primary parts
    if flow == "center":
        placements.update(
            _place_center_flow(primary_refs, sheet_name, usable_w, usable_h)
        )
    elif flow == "vertical_stack":
        placements.update(
            _place_vertical_flow(primary_refs, sheet_name, usable_w, usable_h)
        )
    else:  # left_to_right
        placements.update(
            _place_horizontal_flow(primary_refs, sheet_name, usable_w, usable_h)
        )

    # Place support parts near their targets
    placements.update(
        _place_support_near_targets(support_refs, canonical, placements, sheet_name)
    )

    return placements


def _place_horizontal_flow(
    refs: list[str],
    sheet_name: str,
    usable_w: float,
    usable_h: float,
) -> dict[str, PlacementInfo]:
    """Place parts in horizontal left-to-right flow."""
    placements: dict[str, PlacementInfo] = {}
    x = SHEET_MARGIN_X
    y = SHEET_MARGIN_Y + 20.0  # Leave room for title

    cols = max(1, int(usable_w / SYMBOL_GAP_X))
    for i, ref in enumerate(refs):
        col = i % cols
        row = i // cols
        px = _snap_to_grid(x + col * SYMBOL_GAP_X)
        py = _snap_to_grid(y + row * SYMBOL_GAP_Y)
        placements[ref] = PlacementInfo(ref=ref, x=px, y=py, sheet=sheet_name)

    return placements


def _place_vertical_flow(
    refs: list[str],
    sheet_name: str,
    usable_w: float,
    usable_h: float,
) -> dict[str, PlacementInfo]:
    """Place parts in vertical stack flow."""
    placements: dict[str, PlacementInfo] = {}
    x = SHEET_MARGIN_X + usable_w / 3  # Center horizontally
    y = SHEET_MARGIN_Y + 20.0

    for i, ref in enumerate(refs):
        px = _snap_to_grid(x)
        py = _snap_to_grid(y + i * SYMBOL_GAP_Y)
        placements[ref] = PlacementInfo(ref=ref, x=px, y=py, sheet=sheet_name)

    return placements


def _place_center_flow(
    refs: list[str],
    sheet_name: str,
    usable_w: float,
    usable_h: float,
) -> dict[str, PlacementInfo]:
    """Place the main part at center with support parts around it."""
    placements: dict[str, PlacementInfo] = {}

    if not refs:
        return placements

    # First ref is the main IC, place at center
    center_x = SHEET_MARGIN_X + usable_w / 2
    center_y = SHEET_MARGIN_Y + usable_h / 2

    placements[refs[0]] = PlacementInfo(
        ref=refs[0],
        x=_snap_to_grid(center_x),
        y=_snap_to_grid(center_y),
        sheet=sheet_name,
    )

    # Place remaining parts around center
    for i, ref in enumerate(refs[1:], 1):
        angle = (i * 45.0) % 360
        import math
        distance = 40.0 + (i // 8) * 20.0
        px = center_x + distance * math.cos(math.radians(angle))
        py = center_y + distance * math.sin(math.radians(angle))
        placements[ref] = PlacementInfo(
            ref=ref,
            x=_snap_to_grid(px),
            y=_snap_to_grid(py),
            sheet=sheet_name,
        )

    return placements


def _place_support_near_targets(
    support_refs: list[str],
    canonical: CanonicalCircuit,
    existing_placements: dict[str, PlacementInfo],
    sheet_name: str,
) -> dict[str, PlacementInfo]:
    """Place support parts (decoupling, pullups) near their target ICs."""
    placements: dict[str, PlacementInfo] = {}
    target_offsets: dict[str, int] = {}  # Track how many support parts per target

    for ref in support_refs:
        part = canonical.part_by_ref(ref)
        if not part:
            continue

        target_ref = part.properties.get("KICAD_MCP_TARGET", "")
        target_placement = existing_placements.get(target_ref)

        if target_placement:
            offset_idx = target_offsets.get(target_ref, 0)
            target_offsets[target_ref] = offset_idx + 1

            # Place near target with offset
            offset_x = DECOUPLING_OFFSET_X * (1 + offset_idx % 3)
            offset_y = DECOUPLING_OFFSET_Y + (offset_idx // 3) * 10.0

            placements[ref] = PlacementInfo(
                ref=ref,
                x=_snap_to_grid(target_placement.x + offset_x),
                y=_snap_to_grid(target_placement.y + offset_y),
                sheet=sheet_name,
            )
        else:
            # No target found, place at end of sheet
            placements[ref] = PlacementInfo(
                ref=ref,
                x=_snap_to_grid(SHEET_MARGIN_X + len(placements) * 15.0),
                y=_snap_to_grid(SHEET_MARGIN_Y + 150.0),
                sheet=sheet_name,
            )

    return placements


def _determine_sheet_sizes(
    sheets: dict[str, list[str]],
    default_size: str,
) -> dict[str, str]:
    """Determine paper size for each sheet based on part count."""
    sizes: dict[str, str] = {}
    for sheet_name, refs in sheets.items():
        count = len(refs)
        if count > 60:
            sizes[sheet_name] = "A2"
        elif count > 30:
            sizes[sheet_name] = "A3"
        else:
            sizes[sheet_name] = default_size
    return sizes


def _snap_to_grid(value: float) -> float:
    """Snap a value to the schematic grid."""
    return round(value / GRID_MM) * GRID_MM
