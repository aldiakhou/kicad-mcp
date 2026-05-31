"""Schematic writer using KiUtils/kicad-skip for .kicad_sch generation.

Consumes SheetPlan, CanonicalCircuit, and PlacementInfo to produce
complete KiCad schematic files in a temporary project directory.

Uses:
- KiUtils for structured reading/writing and format safety
- kicad-skip as a required schematic runtime dependency

Key design: Every CircuitEndpoint becomes a real KiCad connection via a wire
stub from the exact KiCad symbol pin coordinate to a net label placed at the
stub end. Pin coordinates are resolved from the KiCad symbol library using the
same geometry pipeline as the rest of the MCP tools. If library resolution fails
(e.g., libraries not installed), falls back to estimation.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any
import uuid

from kicad_mcp.schematic_engine.models import (
    CanonicalCircuit,
    CircuitPart,
    PlacementInfo,
    SheetPlan,
)

logger = logging.getLogger(__name__)

# Required runtime dependencies. Server startup also checks these, but the writer
# validates again so direct use fails before generating a partial schematic.
_KIUTILS_AVAILABLE = False
_KICAD_SKIP_AVAILABLE = False

try:
    import kiutils  # noqa: F401
    _KIUTILS_AVAILABLE = True
except ImportError:
    pass

try:
    import skip  # noqa: F401
    _KICAD_SKIP_AVAILABLE = True
except ImportError:
    pass

# ─── Pin coordinate constants ───────────────────────────────────────────────

_PIN_GRID_MM = 2.54  # KiCad standard pin grid (100mil)
_WIRE_STUB_LENGTH_MM = 10.0  # Length of wire from pin to label
_SYMBOL_HALF_WIDTH_MM = 7.62  # Estimated half-width of symbol body (fallback only)


# ─── Real pin resolution from KiCad symbol libraries ────────────────────────


def _resolve_real_pin_positions(
    part: CircuitPart,
    placement: PlacementInfo,
) -> dict[str, list[tuple[float, float, float]]]:
    """Resolve exact pin positions from KiCad symbol library.

    Returns a dict mapping pin identifier (number or name) to a *list* of
    (connection_x, connection_y, stub_angle) tuples in sheet coordinates.

    Pin numbers are unique per KiCad symbol, so their lists always have one
    entry.  Pin *names* may be duplicated (e.g. multiple VDD pins on an MCU),
    so their lists can have multiple entries — one per physical pin.

    Uses the same pin resolution and coordinate transform pipeline as
    get_symbol_pin_map_from_schematic in schematic_pins.py.

    Returns empty dict if library resolution fails (caller should fall back
    to estimation).
    """
    try:
        from kicad_mcp.utils.schematic_pins import _resolve_symbol_pins, _transform_pin
    except ImportError:
        return {}

    try:
        pins = _resolve_symbol_pins(part.lib_id)
    except Exception:
        # Library not available or symbol not found - fall back
        return {}

    if not pins:
        return {}

    # Transform each library pin to sheet coordinates using the placement
    result: dict[str, list[tuple[float, float, float]]] = {}
    for pin in pins:
        transformed = _transform_pin(
            pin, placement.x, placement.y, placement.angle
        )
        cp = transformed["connection_point"]
        stub_angle = transformed["position"].get("angle", 0.0)
        pin_coord = (cp["x"], cp["y"], stub_angle)

        # Index by pin number (primary) — numbers are unique per symbol
        pin_number = pin.get("number", "")
        pin_name = pin.get("name", "")
        if pin_number:
            result.setdefault(pin_number, []).append(pin_coord)
        if pin_name and pin_name != pin_number:
            # Also index by name — may have multiple entries for same name
            result.setdefault(pin_name, []).append(pin_coord)

    return result


def _compute_label_position_from_stub_angle(
    pin_x: float, pin_y: float, stub_angle: float
) -> tuple[float, float]:
    """Compute label position using the real pin stub direction.

    The stub_angle from _transform_pin indicates the direction the wire
    should extend from the pin (outward from the symbol body).
    """
    rad = math.radians(stub_angle)
    label_x = pin_x + math.cos(rad) * _WIRE_STUB_LENGTH_MM
    label_y = pin_y + math.sin(rad) * _WIRE_STUB_LENGTH_MM
    return (label_x, label_y)


# ─── Fallback pin coordinate estimation ─────────────────────────────────────
# Used only when KiCad symbol libraries are not available on the system.


def _estimate_pin_position(
    placement: PlacementInfo,
    pin_index: int,
    total_pins_on_ref: int,
) -> tuple[float, float]:
    """Estimate a pin's absolute coordinate based on symbol placement.

    Pins are distributed along the left and right edges of the symbol.
    Even-indexed pins go on the left, odd-indexed on the right.

    Returns (x, y) of the estimated pin connection point.

    NOTE: This is a fallback only used when library resolution fails.
    """
    # Determine side: even pins left, odd pins right
    is_right = pin_index % 2 == 1
    side_index = pin_index // 2

    # Vertical distribution
    pins_per_side = max(1, (total_pins_on_ref + 1) // 2)
    y_offset = (side_index - (pins_per_side - 1) / 2.0) * _PIN_GRID_MM

    # Horizontal offset from symbol center
    x_offset = _SYMBOL_HALF_WIDTH_MM if is_right else -_SYMBOL_HALF_WIDTH_MM

    # Apply rotation
    angle_rad = math.radians(placement.angle)
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)

    rotated_x = x_offset * cos_a - y_offset * sin_a
    rotated_y = x_offset * sin_a + y_offset * cos_a

    return (placement.x + rotated_x, placement.y + rotated_y)


def _compute_label_position(
    pin_x: float, pin_y: float, placement: PlacementInfo
) -> tuple[float, float]:
    """Compute the label position at the end of a wire stub from a pin.

    The wire extends outward from the symbol.

    NOTE: This is a fallback only used when library resolution fails.
    """
    # Direction away from symbol center
    dx = pin_x - placement.x
    dy = pin_y - placement.y
    length = math.sqrt(dx * dx + dy * dy)
    if length < 0.01:
        # Pin at center, extend to the right
        return (pin_x + _WIRE_STUB_LENGTH_MM, pin_y)

    # Normalize and extend
    nx = dx / length
    ny = dy / length
    return (pin_x + nx * _WIRE_STUB_LENGTH_MM, pin_y + ny * _WIRE_STUB_LENGTH_MM)


class SchematicWriter:
    """Writes KiCad schematic files from planned circuit data.

    Responsibilities:
    1. Create root sheet and hierarchical sheet symbols.
    2. Create per-sheet symbols with proper placement.
    3. Place symbols according to sheet plan.
    4. Place decoupling and support parts near target.
    5. Add net labels or wires.
    6. Add no-connect markers.
    7. Assign footprints.
    8. Write title blocks.
    9. Add generated metadata properties.
    """

    def __init__(self, output_dir: str, project_name: str):
        """Initialize writer.

        Args:
            output_dir: Directory to write schematic files.
            project_name: Project name (used for root schematic filename).
        """
        self.output_dir = output_dir
        self.project_name = project_name

    def write(
        self,
        canonical: CanonicalCircuit,
        sheet_plan: SheetPlan,
    ) -> dict[str, Any]:
        """Write complete schematic files.

        Args:
            canonical: The canonical circuit definition.
            sheet_plan: The sheet distribution and placement plan.

        Returns:
            Dict with success status and list of generated files.
        """
        os.makedirs(self.output_dir, exist_ok=True)

        if not _KIUTILS_AVAILABLE or not _KICAD_SKIP_AVAILABLE:
            missing: list[str] = []
            if not _KIUTILS_AVAILABLE:
                missing.append("kiutils")
            if not _KICAD_SKIP_AVAILABLE:
                missing.append("kicad-skip")
            raise RuntimeError(
                "Required schematic writer dependencies are missing: "
                + ", ".join(missing)
            )
        return self._write_with_kiutils(canonical, sheet_plan)

    def _write_with_kiutils(
        self,
        canonical: CanonicalCircuit,
        sheet_plan: SheetPlan,
    ) -> dict[str, Any]:
        """Write schematics using KiUtils library."""
        try:
            from kiutils.schematic import Schematic

            generated_files: list[str] = []

            # Generate each sheet
            for sheet_name, refs in sheet_plan.sheets.items():
                filename = (
                    f"{self.project_name}.kicad_sch"
                    if sheet_name == "root"
                    else f"{sheet_name}.kicad_sch"
                )
                filepath = os.path.join(self.output_dir, filename)

                # Create a new schematic
                sch = Schematic.create_new()

                # Set paper size
                paper_size = sheet_plan.sheet_sizes.get(sheet_name, "A3")
                sch.paper = paper_size

                # Add symbols for each part in this sheet
                for ref in refs:
                    part = canonical.part_by_ref(ref)
                    placement = sheet_plan.placements.get(ref)
                    if part and placement:
                        self._add_symbol_kiutils(sch, part, placement)

                # Add net labels
                self._add_labels_kiutils(sch, canonical, refs, sheet_plan, sheet_name)

                # Add no-connect markers
                self._add_no_connects_kiutils(sch, canonical, refs, sheet_plan)

                # Write schematic
                sch.to_file(filepath)
                generated_files.append(filename)

            # Write root sheet with hierarchical sheet references
            if "root" not in sheet_plan.sheets and len(sheet_plan.sheets) > 1:
                root_path = os.path.join(
                    self.output_dir, f"{self.project_name}.kicad_sch"
                )
                root_sch = Schematic.create_new()
                root_sch.paper = "A3"
                self._add_hierarchical_sheets_kiutils(root_sch, sheet_plan)
                root_sch.to_file(root_path)
                generated_files.insert(0, f"{self.project_name}.kicad_sch")

            # Write project file
            self._write_project_file()
            generated_files.append(f"{self.project_name}.kicad_pro")

            return {
                "success": True,
                "files": generated_files,
                "method": "kiutils",
            }
        except Exception as e:
            logger.error("KiUtils write failed: %s", e)
            return {
                "success": False,
                "error": f"KiUtils writer failed: {e}",
                "method": "kiutils",
            }

    def _add_symbol_kiutils(
        self,
        sch: Any,
        part: CircuitPart,
        placement: PlacementInfo,
    ) -> None:
        """Add a symbol to a KiUtils schematic."""
        try:
            from kiutils.items.common import Position, Property
            from kiutils.items.schitems import SchematicSymbol

            symbol = SchematicSymbol()
            symbol.libId = part.lib_id
            symbol.entryName = part.lib_id.split(":")[-1] if ":" in part.lib_id else part.lib_id
            symbol.position = Position(X=placement.x, Y=placement.y, angle=placement.angle)
            symbol.uuid = str(uuid.uuid4())

            # Add properties
            ref_prop = Property(key="Reference", value=part.ref)
            ref_prop.position = Position(X=placement.x, Y=placement.y - 2.54)
            symbol.properties.append(ref_prop)

            val_prop = Property(key="Value", value=part.value)
            val_prop.position = Position(X=placement.x, Y=placement.y + 2.54)
            symbol.properties.append(val_prop)

            if part.footprint:
                fp_prop = Property(key="Footprint", value=part.footprint)
                fp_prop.position = Position(X=placement.x, Y=placement.y + 5.08)
                symbol.properties.append(fp_prop)

            # Add MCP metadata properties
            for key, value in part.properties.items():
                if key.startswith("KICAD_MCP_"):
                    meta_prop = Property(key=key, value=value)
                    meta_prop.position = Position(X=placement.x, Y=placement.y)
                    symbol.properties.append(meta_prop)

            sch.schematicSymbols.append(symbol)
        except Exception as e:
            logger.warning("Failed to add symbol %s via KiUtils: %s", part.ref, e)

    def _add_labels_kiutils(
        self,
        sch: Any,
        canonical: CanonicalCircuit,
        refs: list[str],
        sheet_plan: SheetPlan,
        sheet_name: str,
    ) -> None:
        """Add net labels with wire stubs connecting to real symbol pin positions.

        For every endpoint on this sheet, we:
        1. Resolve the exact pin coordinate from the KiCad symbol library.
        2. Place a wire stub from the pin to a label position.
        3. Place the net label at the wire stub end.

        Falls back to estimated pin positions only when libraries are unavailable.
        This ensures KiCad recognizes electrical connectivity between pins and nets.
        """
        try:
            from kiutils.items.common import Position
            from kiutils.items.schitems import GlobalLabel, NetLabel

            ref_set = set(refs)

            # Resolve real pin positions for each part on this sheet.
            # Cache per-ref to avoid resolving the same symbol multiple times.
            resolved_pin_cache: dict[str, dict[str, list[tuple[float, float, float]]]] = {}
            for ref in refs:
                part = canonical.part_by_ref(ref)
                placement = sheet_plan.placements.get(ref)
                if part and placement:
                    pin_map = _resolve_real_pin_positions(part, placement)
                    resolved_pin_cache[ref] = pin_map

            # Pre-compute pin indices per ref for fallback estimation
            ref_pin_counts: dict[str, int] = {}
            ref_pin_indices: dict[tuple[str, str], int] = {}
            for ep in canonical.endpoints:
                if ep.ref not in ref_set:
                    continue
                if ep.ref not in ref_pin_counts:
                    ref_pin_counts[ep.ref] = 0
                ref_pin_indices[(ep.ref, ep.pin)] = ref_pin_counts[ep.ref]
                ref_pin_counts[ep.ref] += 1

            for ep in canonical.endpoints:
                if ep.ref not in ref_set:
                    continue

                placement = sheet_plan.placements.get(ep.ref)
                if not placement:
                    continue

                # Try real pin resolution first
                real_pins = resolved_pin_cache.get(ep.ref, {})
                real_pin_entries = real_pins.get(ep.pin, [])

                if real_pin_entries:
                    # Connect ALL matching pins.  When a pin name (e.g. "VDD")
                    # maps to multiple physical pins, each gets its own wire
                    # stub + label so KiCad recognizes every pin as connected.
                    for pin_x, pin_y, stub_angle in real_pin_entries:
                        label_x, label_y = _compute_label_position_from_stub_angle(
                            pin_x, pin_y, stub_angle
                        )
                        # Add wire from pin to label
                        self._add_wire_kiutils(sch, pin_x, pin_y, label_x, label_y)

                        # Use global labels for cross-sheet nets, local otherwise
                        is_global = ep.net in sheet_plan.cross_sheet_nets

                        if is_global:
                            label = GlobalLabel()
                            label.text = ep.net
                            label.position = Position(X=label_x, Y=label_y)
                            label.uuid = str(uuid.uuid4())
                            sch.globalLabels.append(label)
                        else:
                            label = NetLabel()
                            label.text = ep.net
                            label.position = Position(X=label_x, Y=label_y)
                            label.uuid = str(uuid.uuid4())
                            sch.netLabels.append(label)
                else:
                    # Fallback: estimate pin position when library unavailable
                    logger.debug(
                        "Pin %s.%s not resolved from library, using estimation",
                        ep.ref, ep.pin,
                    )
                    pin_idx = ref_pin_indices.get((ep.ref, ep.pin), 0)
                    total_pins = ref_pin_counts.get(ep.ref, 1)
                    pin_x, pin_y = _estimate_pin_position(placement, pin_idx, total_pins)
                    label_x, label_y = _compute_label_position(pin_x, pin_y, placement)

                    self._add_wire_kiutils(sch, pin_x, pin_y, label_x, label_y)

                    is_global = ep.net in sheet_plan.cross_sheet_nets
                    if is_global:
                        label = GlobalLabel()
                        label.text = ep.net
                        label.position = Position(X=label_x, Y=label_y)
                        label.uuid = str(uuid.uuid4())
                        sch.globalLabels.append(label)
                    else:
                        label = NetLabel()
                        label.text = ep.net
                        label.position = Position(X=label_x, Y=label_y)
                        label.uuid = str(uuid.uuid4())
                        sch.netLabels.append(label)
        except Exception as e:
            logger.warning("Failed to add labels via KiUtils: %s", e)

    def _add_wire_kiutils(
        self,
        sch: Any,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
    ) -> None:
        """Add a wire segment between two points in a KiUtils schematic."""
        try:
            from kiutils.items.common import Position
            from kiutils.items.schitems import Connection

            wire = Connection()
            wire.type = "wire"
            wire.startPoint = Position(X=x1, Y=y1)
            wire.endPoint = Position(X=x2, Y=y2)
            wire.uuid = str(uuid.uuid4())
            sch.connections.append(wire)
        except Exception as e:
            logger.warning("Failed to add wire via KiUtils: %s", e)

    def _add_no_connects_kiutils(
        self,
        sch: Any,
        canonical: CanonicalCircuit,
        refs: list[str],
        sheet_plan: SheetPlan | None = None,
    ) -> None:
        """Add no-connect markers at exact pin positions.

        For each (ref, pin) in canonical.no_connects that belongs to this sheet,
        resolve the real pin coordinate and place a no_connect marker there.
        Falls back to estimated position if library resolution is unavailable.
        """
        try:
            from kiutils.items.common import Position
            from kiutils.schematic import NoConnect

            ref_set = set(refs)

            for nc_ref, nc_pin in canonical.no_connects:
                if nc_ref not in ref_set:
                    continue

                placement = sheet_plan.placements.get(nc_ref) if sheet_plan else None
                if not placement:
                    continue

                # Try to resolve exact pin position
                nc_x: float | None = None
                nc_y: float | None = None

                part = canonical.part_by_ref(nc_ref)
                if part:
                    pin_map = _resolve_real_pin_positions(part, placement)
                    pin_entries = pin_map.get(nc_pin, [])
                    if pin_entries:
                        # Use first matching pin position for no-connect
                        nc_x, nc_y, _ = pin_entries[0]

                if nc_x is None or nc_y is None:
                    # Fallback: place near the symbol origin
                    nc_x = placement.x - 5.08
                    nc_y = placement.y

                no_connect = NoConnect()
                no_connect.position = Position(X=nc_x, Y=nc_y)
                no_connect.uuid = str(uuid.uuid4())
                sch.noConnects.append(no_connect)
        except Exception as e:
            logger.warning("Failed to add no-connects via KiUtils: %s", e)

    def _add_hierarchical_sheets_kiutils(
        self,
        sch: Any,
        sheet_plan: SheetPlan,
    ) -> None:
        """Add hierarchical sheet symbols to root schematic."""
        try:
            from kiutils.items.common import Position
            from kiutils.items.schitems import HierarchicalSheet

            x_offset = 50.0
            y_offset = 50.0
            sheet_width = 40.0
            sheet_height = 20.0
            gap = 10.0

            for i, (sheet_name, _refs) in enumerate(sheet_plan.sheets.items()):
                if sheet_name == "root":
                    continue

                sheet = HierarchicalSheet()
                sheet.fileName = f"{sheet_name}.kicad_sch"
                sheet.sheetName = sheet_name
                sheet.position = Position(
                    X=x_offset + (i % 3) * (sheet_width + gap),
                    Y=y_offset + (i // 3) * (sheet_height + gap),
                )
                sheet.uuid = str(uuid.uuid4())
                sch.hierarchicalSheets.append(sheet)
        except Exception as e:
            logger.warning("Failed to add hierarchical sheets: %s", e)


    def _write_project_file(self) -> None:
        """Write a minimal .kicad_pro file."""
        pro_path = os.path.join(self.output_dir, f"{self.project_name}.kicad_pro")
        if not os.path.exists(pro_path):
            content = (
                '{\n'
                '  "meta": {\n'
                '    "filename": "' + self.project_name + '.kicad_pro",\n'
                '    "version": 1\n'
                '  }\n'
                '}\n'
            )
            with open(pro_path, "w", encoding="utf-8") as f:
                f.write(content)
