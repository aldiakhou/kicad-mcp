"""Schematic writer using KiUtils/kicad-skip for .kicad_sch generation.

Consumes SheetPlan, CanonicalCircuit, and PlacementInfo to produce
complete KiCad schematic files in a temporary project directory.

Uses:
- KiUtils for structured reading/writing and format safety (preferred)
- kicad-skip where its ergonomic helpers are better
- Falls back to S-expression text generation when libraries unavailable
"""

from __future__ import annotations

import logging
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

# Check optional dependencies
_KIUTILS_AVAILABLE = False
_KICAD_SKIP_AVAILABLE = False

try:
    import kiutils  # noqa: F401
    _KIUTILS_AVAILABLE = True
except ImportError:
    pass

try:
    import kicad_skip  # noqa: F401
    _KICAD_SKIP_AVAILABLE = True
except ImportError:
    pass


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

        if _KIUTILS_AVAILABLE:
            return self._write_with_kiutils(canonical, sheet_plan)
        return self._write_fallback(canonical, sheet_plan)

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
                self._add_no_connects_kiutils(sch, canonical, refs)

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
            logger.error("KiUtils write failed: %s, falling back", e)
            return self._write_fallback(canonical, sheet_plan)

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
        """Add net labels to a KiUtils schematic."""
        try:
            from kiutils.items.common import Position
            from kiutils.items.schitems import GlobalLabel, NetLabel

            ref_set = set(refs)
            placed_labels: set[str] = set()

            for ep in canonical.endpoints:
                if ep.ref not in ref_set:
                    continue
                if ep.net in placed_labels:
                    continue

                placement = sheet_plan.placements.get(ep.ref)
                if not placement:
                    continue

                # Use global labels for cross-sheet nets, local otherwise
                is_global = ep.net in sheet_plan.cross_sheet_nets

                label_x = placement.x + 15.0  # Offset from symbol
                label_y = placement.y

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

                placed_labels.add(ep.net)
        except Exception as e:
            logger.warning("Failed to add labels via KiUtils: %s", e)

    def _add_no_connects_kiutils(
        self,
        sch: Any,
        canonical: CanonicalCircuit,
        refs: list[str],
    ) -> None:
        """Add no-connect markers."""
        # No-connects require pin position resolution which needs symbol data
        # For now, skip - they will be added during visual lint pass
        pass

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

    def _write_fallback(
        self,
        canonical: CanonicalCircuit,
        sheet_plan: SheetPlan,
    ) -> dict[str, Any]:
        """Write schematics using direct S-expression generation (fallback)."""
        generated_files: list[str] = []

        try:
            # Generate each sheet
            for sheet_name, refs in sheet_plan.sheets.items():
                filename = (
                    f"{self.project_name}.kicad_sch"
                    if sheet_name == "root"
                    else f"{sheet_name}.kicad_sch"
                )
                filepath = os.path.join(self.output_dir, filename)
                paper_size = sheet_plan.sheet_sizes.get(sheet_name, "A3")

                content = self._generate_sexpr_schematic(
                    canonical, refs, sheet_plan, sheet_name, paper_size
                )
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
                generated_files.append(filename)

            # Write root with hierarchy if multi-sheet
            if "root" not in sheet_plan.sheets and len(sheet_plan.sheets) > 1:
                root_path = os.path.join(
                    self.output_dir, f"{self.project_name}.kicad_sch"
                )
                content = self._generate_root_sexpr(sheet_plan)
                with open(root_path, "w", encoding="utf-8") as f:
                    f.write(content)
                generated_files.insert(0, f"{self.project_name}.kicad_sch")

            # Write project file
            self._write_project_file()
            generated_files.append(f"{self.project_name}.kicad_pro")

            return {
                "success": True,
                "files": generated_files,
                "method": "sexpr_fallback",
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Fallback writer failed: {e}",
                "files": generated_files,
            }

    def _generate_sexpr_schematic(
        self,
        canonical: CanonicalCircuit,
        refs: list[str],
        sheet_plan: SheetPlan,
        sheet_name: str,
        paper_size: str,
    ) -> str:
        """Generate KiCad schematic S-expression content."""
        lines = [
            '(kicad_sch (version 20231120) (generator "kicad_mcp")',
            f'  (paper "{paper_size}")',
            "",
            "  (lib_symbols",
            "  )",
            "",
        ]

        # Add symbols
        ref_set = set(refs)
        for ref in refs:
            part = canonical.part_by_ref(ref)
            placement = sheet_plan.placements.get(ref)
            if part and placement:
                lines.append(self._symbol_sexpr(part, placement))

        # Add net labels
        placed_labels: set[str] = set()
        for ep in canonical.endpoints:
            if ep.ref not in ref_set:
                continue
            if ep.net in placed_labels:
                continue
            placement = sheet_plan.placements.get(ep.ref)
            if placement:
                is_global = ep.net in sheet_plan.cross_sheet_nets
                lines.append(self._label_sexpr(
                    ep.net, placement.x + 15.0, placement.y, is_global
                ))
                placed_labels.add(ep.net)

        # Add no-connect markers
        for nc_ref, _nc_pin in canonical.no_connects:
            if nc_ref in ref_set:
                placement = sheet_plan.placements.get(nc_ref)
                if placement:
                    lines.append(self._no_connect_sexpr(placement.x - 5.0, placement.y))

        lines.append(")")
        return "\n".join(lines)

    def _generate_root_sexpr(self, sheet_plan: SheetPlan) -> str:
        """Generate root schematic with hierarchical sheet references."""
        lines = [
            '(kicad_sch (version 20231120) (generator "kicad_mcp")',
            '  (paper "A3")',
            "",
            "  (lib_symbols",
            "  )",
            "",
        ]

        x_offset = 50.0
        y_offset = 50.0
        sheet_w = 40.0
        sheet_h = 20.0
        gap = 10.0

        for i, (sheet_name, _refs) in enumerate(sheet_plan.sheets.items()):
            if sheet_name == "root":
                continue
            x = x_offset + (i % 3) * (sheet_w + gap)
            y = y_offset + (i // 3) * (sheet_h + gap)
            sheet_uuid = uuid.uuid4()
            lines.append(f"  (sheet (at {x:.2f} {y:.2f}) (size {sheet_w:.2f} {sheet_h:.2f})")
            lines.append('    (stroke (width 0.001) (type solid) (color 0 0 0 1))')
            lines.append('    (fill (color 255 255 255 1))')
            lines.append(f'    (uuid "{sheet_uuid}")')
            lines.append(f'    (property "Sheetname" "{sheet_name}"')
            lines.append(f'      (at {x + 1:.2f} {y - 1:.2f} 0)')
            lines.append("      (effects (font (size 1.27 1.27)))")
            lines.append("    )")
            lines.append(f'    (property "Sheetfile" "{sheet_name}.kicad_sch"')
            lines.append(f'      (at {x + 1:.2f} {y + sheet_h + 1:.2f} 0)')
            lines.append("      (effects (font (size 1.27 1.27)))")
            lines.append("    )")
            lines.append("  )")

        lines.append(")")
        return "\n".join(lines)

    def _symbol_sexpr(self, part: CircuitPart, placement: PlacementInfo) -> str:
        """Generate S-expression for a symbol placement."""
        sym_uuid = uuid.uuid4()
        lines = [
            f'  (symbol (lib_id "{part.lib_id}") (at {placement.x:.2f} {placement.y:.2f} '
            f'{placement.angle:.0f})',
            f'    (uuid "{sym_uuid}")',
        ]

        # Properties
        lines.append(f'    (property "Reference" "{part.ref}"')
        lines.append(f'      (at {placement.x:.2f} {placement.y - 2.54:.2f} 0)')
        lines.append("      (effects (font (size 1.27 1.27)))")
        lines.append("    )")

        lines.append(f'    (property "Value" "{part.value}"')
        lines.append(f'      (at {placement.x:.2f} {placement.y + 2.54:.2f} 0)')
        lines.append("      (effects (font (size 1.27 1.27)))")
        lines.append("    )")

        if part.footprint:
            lines.append(f'    (property "Footprint" "{part.footprint}"')
            lines.append(f'      (at {placement.x:.2f} {placement.y + 5.08:.2f} 0)')
            lines.append("      (effects (font (size 1.27 1.27)) hide)")
            lines.append("    )")

        # MCP metadata
        for key, value in part.properties.items():
            if key.startswith("KICAD_MCP_"):
                lines.append(f'    (property "{key}" "{value}"')
                lines.append(f'      (at {placement.x:.2f} {placement.y:.2f} 0)')
                lines.append("      (effects (font (size 1.27 1.27)) hide)")
                lines.append("    )")

        lines.append("  )")
        return "\n".join(lines)

    def _label_sexpr(
        self, net: str, x: float, y: float, is_global: bool
    ) -> str:
        """Generate S-expression for a net label."""
        label_uuid = uuid.uuid4()
        if is_global:
            return (
                f'  (global_label "{net}" (shape passive) (at {x:.2f} {y:.2f} 0)\n'
                f'    (effects (font (size 1.27 1.27)))\n'
                f'    (uuid "{label_uuid}")\n'
                f"  )"
            )
        return (
            f'  (label "{net}" (at {x:.2f} {y:.2f} 0)\n'
            f'    (effects (font (size 1.27 1.27)))\n'
            f'    (uuid "{label_uuid}")\n'
            f"  )"
        )

    def _no_connect_sexpr(self, x: float, y: float) -> str:
        """Generate S-expression for a no-connect marker."""
        nc_uuid = uuid.uuid4()
        return f'  (no_connect (at {x:.2f} {y:.2f}) (uuid "{nc_uuid}"))'

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
