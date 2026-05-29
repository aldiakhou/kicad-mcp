"""Tests for visual lint."""


from kicad_mcp.schematic_engine.models import (
    CanonicalCircuit,
    CircuitPart,
    PlacementInfo,
    SheetPlan,
)
from kicad_mcp.schematic_engine.visual_lint import (
    LINT_DECOUPLING_NOT_NEAR_TARGET,
    LINT_SHEET_OVERFLOW,
    LINT_SYMBOL_OVERLAP,
    LINT_UNPLACED_SYMBOL,
    visual_lint,
)


class TestVisualLint:
    """Tests for visual_lint function."""

    def _make_canonical_and_plan(self, parts, placements_dict, endpoints=None):
        """Create canonical circuit and sheet plan for testing."""
        canonical = CanonicalCircuit(
            project_path="/tmp/test.kicad_pro",
            parts=parts,
            endpoints=endpoints or [],
            no_connects=[],
            blocks={},
            rails=set(),
        )
        placements = {
            ref: PlacementInfo(ref=ref, x=x, y=y, sheet="root")
            for ref, (x, y) in placements_dict.items()
        }
        plan = SheetPlan(
            sheets={"root": [p.ref for p in parts]},
            placements=placements,
            sheet_sizes={"root": "A3"},
        )
        return canonical, plan

    def test_no_issues(self):
        """Clean schematic passes visual lint."""
        parts = [
            CircuitPart(ref="R1", lib_id="Device:R", value="10k"),
            CircuitPart(ref="R2", lib_id="Device:R", value="4.7k"),
        ]
        placements = {"R1": (100.0, 100.0), "R2": (150.0, 100.0)}
        canonical, plan = self._make_canonical_and_plan(parts, placements)

        result = visual_lint(canonical, plan)
        assert result.success
        assert result.blocking_count == 0

    def test_unplaced_symbol(self):
        """Unplaced symbols are detected."""
        parts = [
            CircuitPart(ref="R1", lib_id="Device:R", value="10k"),
            CircuitPart(ref="R2", lib_id="Device:R", value="4.7k"),
        ]
        placements = {"R1": (100.0, 100.0)}  # R2 has no placement
        canonical, plan = self._make_canonical_and_plan(parts, placements)

        result = visual_lint(canonical, plan)
        assert not result.success
        assert result.blocking_count >= 1
        unplaced = [i for i in result.issues if i.type == LINT_UNPLACED_SYMBOL]
        assert len(unplaced) == 1
        assert unplaced[0].ref == "R2"

    def test_symbol_overlap(self):
        """Overlapping symbols are detected."""
        parts = [
            CircuitPart(ref="R1", lib_id="Device:R", value="10k"),
            CircuitPart(ref="R2", lib_id="Device:R", value="4.7k"),
        ]
        # Place very close together
        placements = {"R1": (100.0, 100.0), "R2": (105.0, 100.0)}
        canonical, plan = self._make_canonical_and_plan(parts, placements)

        result = visual_lint(canonical, plan)
        overlaps = [i for i in result.issues if i.type == LINT_SYMBOL_OVERLAP]
        assert len(overlaps) >= 1

    def test_decoupling_far_from_target(self):
        """Decoupling cap far from target IC is flagged."""
        parts = [
            CircuitPart(ref="U1", lib_id="MCU_ST:STM32", value="STM32"),
            CircuitPart(
                ref="C1", lib_id="Device:C", value="100n",
                role="decoupling",
                properties={"KICAD_MCP_TARGET": "U1"},
            ),
        ]
        # Place C1 very far from U1
        placements = {"U1": (100.0, 100.0), "C1": (300.0, 300.0)}
        canonical, plan = self._make_canonical_and_plan(parts, placements)

        result = visual_lint(canonical, plan)
        far_issues = [i for i in result.issues if i.type == LINT_DECOUPLING_NOT_NEAR_TARGET]
        assert len(far_issues) == 1
        assert far_issues[0].ref == "C1"

    def test_decoupling_near_target_passes(self):
        """Decoupling cap near target IC passes."""
        parts = [
            CircuitPart(ref="U1", lib_id="MCU_ST:STM32", value="STM32"),
            CircuitPart(
                ref="C1", lib_id="Device:C", value="100n",
                role="decoupling",
                properties={"KICAD_MCP_TARGET": "U1"},
            ),
        ]
        placements = {"U1": (100.0, 100.0), "C1": (115.0, 110.0)}
        canonical, plan = self._make_canonical_and_plan(parts, placements)

        result = visual_lint(canonical, plan)
        far_issues = [i for i in result.issues if i.type == LINT_DECOUPLING_NOT_NEAR_TARGET]
        assert len(far_issues) == 0

    def test_sheet_overflow(self):
        """Symbols outside sheet bounds are detected."""
        parts = [
            CircuitPart(ref="R1", lib_id="Device:R", value="10k"),
        ]
        # Place outside A3 bounds (420x297mm)
        placements = {"R1": (500.0, 100.0)}
        canonical, plan = self._make_canonical_and_plan(parts, placements)

        result = visual_lint(canonical, plan)
        overflow = [i for i in result.issues if i.type == LINT_SHEET_OVERFLOW]
        assert len(overflow) == 1

    def test_visual_lint_blocking_count(self):
        """Blocking count accurately reflects blocking issues."""
        parts = [
            CircuitPart(ref="R1", lib_id="Device:R", value="10k"),
            CircuitPart(ref="R2", lib_id="Device:R", value="4.7k"),
            CircuitPart(ref="R3", lib_id="Device:R", value="1k"),
        ]
        # R3 unplaced = 1 blocking issue
        placements = {"R1": (100.0, 100.0), "R2": (150.0, 100.0)}
        canonical, plan = self._make_canonical_and_plan(parts, placements)

        result = visual_lint(canonical, plan)
        assert result.blocking_count >= 1
        assert not result.success
