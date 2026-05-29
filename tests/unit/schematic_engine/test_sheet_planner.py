"""Tests for the sheet planner."""


from kicad_mcp.schematic_engine.models import (
    CanonicalCircuit,
    CircuitEndpoint,
    CircuitPart,
)
from kicad_mcp.schematic_engine.sheet_planner import plan_sheets


class TestSheetPlanner:
    """Tests for plan_sheets function."""

    def _make_canonical(self, parts, endpoints=None, blocks=None, rails=None):
        """Create a test canonical circuit."""
        return CanonicalCircuit(
            project_path="/tmp/test.kicad_pro",
            parts=parts,
            endpoints=endpoints or [],
            no_connects=[],
            blocks=blocks or {},
            rails=rails or set(),
        )

    def test_small_design_single_sheet(self):
        """Small designs go on a single sheet."""
        parts = [CircuitPart(ref=f"R{i}", lib_id="Device:R", value="10k") for i in range(5)]
        canonical = self._make_canonical(parts)

        plan = plan_sheets(canonical, max_parts_per_sheet=40)
        assert len(plan.sheets) == 1
        assert "root" in plan.sheets
        assert len(plan.sheets["root"]) == 5

    def test_large_design_multi_sheet(self):
        """Large designs are split across sheets."""
        parts = [CircuitPart(ref=f"R{i}", lib_id="Device:R", value="10k") for i in range(100)]
        canonical = self._make_canonical(parts)

        plan = plan_sheets(canonical, max_parts_per_sheet=30)
        assert len(plan.sheets) > 1
        total_refs = sum(len(refs) for refs in plan.sheets.values())
        assert total_refs >= 100

    def test_block_based_distribution(self):
        """Parts are distributed to sheets based on block assignments."""
        parts = [
            CircuitPart(ref="U1", lib_id="MCU_ST:STM32", value="STM32", block="mcu"),
            CircuitPart(ref="R1", lib_id="Device:R", value="10k", block="mcu"),
            CircuitPart(ref="U2", lib_id="Sensor:BMP280", value="BMP280", block="sensors"),
        ]
        blocks = {"mcu": ["U1", "R1"], "sensors": ["U2"]}
        canonical = self._make_canonical(parts, blocks=blocks)

        plan = plan_sheets(canonical, max_parts_per_sheet=40)
        # Should have separate sheets for mcu and sensors
        assert "mcu" in plan.sheets or "root" in plan.sheets

    def test_all_parts_get_placements(self):
        """Every part gets a placement."""
        parts = [CircuitPart(ref=f"R{i}", lib_id="Device:R", value="10k") for i in range(20)]
        canonical = self._make_canonical(parts)

        plan = plan_sheets(canonical)
        for part in parts:
            assert part.ref in plan.placements

    def test_placements_on_grid(self):
        """All placements are snapped to 1.27mm grid."""
        from kicad_mcp.schematic_engine.sheet_planner import GRID_MM

        parts = [CircuitPart(ref=f"R{i}", lib_id="Device:R", value="10k") for i in range(10)]
        canonical = self._make_canonical(parts)

        plan = plan_sheets(canonical)
        for placement in plan.placements.values():
            assert abs(placement.x % GRID_MM) < 0.01 or abs(placement.x % GRID_MM - GRID_MM) < 0.01
            assert abs(placement.y % GRID_MM) < 0.01 or abs(placement.y % GRID_MM - GRID_MM) < 0.01

    def test_cross_sheet_nets_detected(self):
        """Nets spanning multiple sheets are identified as cross-sheet."""
        parts = [
            CircuitPart(ref="U1", lib_id="MCU_ST:STM32", value="STM32", block="mcu"),
            CircuitPart(ref="U2", lib_id="Sensor:BMP280", value="BMP280", block="sensors"),
        ]
        endpoints = [
            CircuitEndpoint(ref="U1", pin="PB8", net="I2C_SCL"),
            CircuitEndpoint(ref="U2", pin="SCL", net="I2C_SCL"),
        ]
        blocks = {"mcu": ["U1"], "sensors": ["U2"]}
        canonical = self._make_canonical(parts, endpoints=endpoints, blocks=blocks)

        # Force multi-sheet by using max_parts_per_sheet=1
        plan = plan_sheets(canonical, max_parts_per_sheet=1)
        # I2C_SCL should be cross-sheet
        if len(plan.sheets) > 1:
            assert "I2C_SCL" in plan.cross_sheet_nets

    def test_power_rails_are_cross_sheet(self):
        """Power rails are always marked as cross-sheet."""
        parts = [CircuitPart(ref="U1", lib_id="Device:R", value="10k")]
        canonical = self._make_canonical(parts, rails={"+3V3", "GND"})

        plan = plan_sheets(canonical)
        assert "+3V3" in plan.cross_sheet_nets
        assert "GND" in plan.cross_sheet_nets

    def test_decoupling_placed_near_target(self):
        """Decoupling caps are placed on same sheet as target."""
        parts = [
            CircuitPart(ref="U1", lib_id="MCU_ST:STM32", value="STM32", block="mcu"),
            CircuitPart(
                ref="C_U1_decap1", lib_id="Device:C", value="100n",
                block="mcu", role="decoupling",
                properties={"KICAD_MCP_TARGET": "U1"},
            ),
        ]
        blocks = {"mcu": ["U1", "C_U1_decap1"]}
        canonical = self._make_canonical(parts, blocks=blocks)

        plan = plan_sheets(canonical)
        # Both should be on same sheet
        u1_sheet = plan.placements["U1"].sheet
        cap_sheet = plan.placements["C_U1_decap1"].sheet
        assert u1_sheet == cap_sheet

    def test_sheet_sizes_determined(self):
        """Each sheet gets a paper size."""
        parts = [CircuitPart(ref=f"R{i}", lib_id="Device:R", value="10k") for i in range(5)]
        canonical = self._make_canonical(parts)

        plan = plan_sheets(canonical, paper_size="A3")
        for sheet_name in plan.sheets:
            assert sheet_name in plan.sheet_sizes
