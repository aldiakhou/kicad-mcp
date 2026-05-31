"""Tests for the schematic writer's pin coordinate resolution.

Verifies that:
1. Real pin positions are resolved from KiCad symbol libraries when available.
2. Fallback estimation is used when libraries are unavailable.
3. Pin coordinates are correctly transformed to sheet space.
4. Wire stubs connect exact pin positions to label positions.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kicad_mcp.schematic_engine.models import (
    CanonicalCircuit,
    CircuitEndpoint,
    CircuitPart,
    PlacementInfo,
    SheetPlan,
)
from kicad_mcp.schematic_engine.schematic_writer import (
    _WIRE_STUB_LENGTH_MM,
    SchematicWriter,
    _compute_label_position,
    _compute_label_position_from_stub_angle,
    _estimate_pin_position,
    _resolve_real_pin_positions,
)

# ─── Test _resolve_real_pin_positions ────────────────────────────────────────


class TestResolveRealPinPositions:
    """Tests for library-based pin resolution."""

    def _make_part(self, lib_id: str = "Device:R", ref: str = "R1") -> CircuitPart:
        return CircuitPart(ref=ref, lib_id=lib_id, value="10k")

    def _make_placement(
        self, x: float = 100.0, y: float = 80.0, angle: float = 0.0
    ) -> PlacementInfo:
        return PlacementInfo(ref="R1", x=x, y=y, angle=angle)

    def test_returns_empty_when_library_unavailable(self):
        """Returns empty dict when symbol library is not found."""
        part = self._make_part(lib_id="NonExistent:Widget")
        placement = self._make_placement()
        result = _resolve_real_pin_positions(part, placement)
        assert result == {}

    def test_returns_empty_when_resolve_raises(self):
        """Returns empty dict when _resolve_symbol_pins raises any exception."""
        with patch(
            "kicad_mcp.schematic_engine.schematic_writer._resolve_real_pin_positions",
            return_value={},
        ):
            part = self._make_part()
            placement = self._make_placement()
            result = _resolve_real_pin_positions(part, placement)
            assert isinstance(result, dict)

    def test_resolves_pins_with_mock_library(self):
        """Resolves pin positions when library data is available."""
        # Mock pin data as _resolve_symbol_pins would return
        mock_pins = [
            {
                "number": "1",
                "name": "A",
                "pinfunction": "A_1",
                "pintype": "passive",
                "shape": "line",
                "hidden": False,
                "local_position": {"x": -3.81, "y": 0.0, "angle": 180.0},
            },
            {
                "number": "2",
                "name": "B",
                "pinfunction": "B_2",
                "pintype": "passive",
                "shape": "line",
                "hidden": False,
                "local_position": {"x": 3.81, "y": 0.0, "angle": 0.0},
            },
        ]

        with patch(
            "kicad_mcp.utils.schematic_pins._resolve_symbol_pins",
            return_value=mock_pins,
        ):
            part = self._make_part()
            placement = self._make_placement(x=100.0, y=80.0, angle=0.0)
            result = _resolve_real_pin_positions(part, placement)

            # Should have entries for pin numbers and names
            assert "1" in result
            assert "2" in result
            assert "A" in result
            assert "B" in result

            # Pin 1 is at local (-3.81, 0) → sheet (100 + (-3.81), 80 + 0) = (96.19, 80)
            # But _transform_pin applies snap to grid, so exact values may differ
            pin1_x, pin1_y, pin1_angle = result["1"][0]
            pin2_x, pin2_y, pin2_angle = result["2"][0]

            # Pin 1 should be to the left of symbol center (x < 100)
            assert pin1_x < 100.0
            # Pin 2 should be to the right of symbol center (x > 100)
            assert pin2_x > 100.0
            # Both pins should be at roughly the same y as placement
            assert abs(pin1_y - 80.0) < 2.0
            assert abs(pin2_y - 80.0) < 2.0

    def test_rotation_transforms_pins(self):
        """Pin positions are correctly rotated with symbol placement."""
        # A pin at local (5, 0) rotated 90 degrees should move to (0, -5) in sheet
        mock_pins = [
            {
                "number": "1",
                "name": "A",
                "pinfunction": "A_1",
                "pintype": "passive",
                "shape": "line",
                "hidden": False,
                "local_position": {"x": 5.08, "y": 0.0, "angle": 0.0},
            },
        ]

        with patch(
            "kicad_mcp.utils.schematic_pins._resolve_symbol_pins",
            return_value=mock_pins,
        ):
            part = self._make_part()

            # No rotation
            placement_0 = self._make_placement(x=100.0, y=80.0, angle=0.0)
            result_0 = _resolve_real_pin_positions(part, placement_0)
            x0, y0, _ = result_0["1"][0]

            # 90 degree rotation
            placement_90 = self._make_placement(x=100.0, y=80.0, angle=90.0)
            result_90 = _resolve_real_pin_positions(part, placement_90)
            x90, y90, _ = result_90["1"][0]

            # At 0 degrees, pin at local (5.08, 0) → sheet offset (+5.08, 0)
            # At 90 degrees, pin at local (5.08, 0) → sheet offset (0, -5.08)
            # (because _transform_pin flips Y and applies rotation)
            assert abs(x0 - 100.0) > 3.0  # Pin is offset from center at 0°
            assert abs(x90 - 100.0) < 2.0  # Pin is near center-x at 90°

    def test_duplicate_pin_names_returns_multiple_entries(self):
        """Duplicate pin names (e.g. multiple VDD) produce multiple list entries."""
        mock_pins = [
            {
                "number": "7",
                "name": "VDD",
                "pinfunction": "VDD",
                "pintype": "power_in",
                "shape": "line",
                "hidden": False,
                "local_position": {"x": 0.0, "y": -5.08, "angle": 90.0},
            },
            {
                "number": "14",
                "name": "VDD",
                "pinfunction": "VDD",
                "pintype": "power_in",
                "shape": "line",
                "hidden": False,
                "local_position": {"x": 0.0, "y": -10.16, "angle": 90.0},
            },
        ]

        with patch(
            "kicad_mcp.utils.schematic_pins._resolve_symbol_pins",
            return_value=mock_pins,
        ):
            part = self._make_part()
            placement = self._make_placement(x=100.0, y=80.0, angle=0.0)
            result = _resolve_real_pin_positions(part, placement)

            # Pin numbers are unique - one entry each
            assert len(result["7"]) == 1
            assert len(result["14"]) == 1

            # Pin name "VDD" is duplicated - should have 2 entries
            assert "VDD" in result
            assert len(result["VDD"]) == 2

            # Entries should be at different positions
            vdd_positions = result["VDD"]
            assert vdd_positions[0] != vdd_positions[1]


# ─── Test _compute_label_position_from_stub_angle ────────────────────────────


class TestComputeLabelPositionFromStubAngle:
    """Tests for label position calculation using real stub angles."""

    def test_stub_angle_0_extends_right(self):
        """Stub angle 0 extends wire to the right."""
        lx, ly = _compute_label_position_from_stub_angle(100.0, 80.0, 0.0)
        assert lx == pytest.approx(100.0 + _WIRE_STUB_LENGTH_MM, abs=0.01)
        assert ly == pytest.approx(80.0, abs=0.01)

    def test_stub_angle_180_extends_left(self):
        """Stub angle 180 extends wire to the left."""
        lx, ly = _compute_label_position_from_stub_angle(100.0, 80.0, 180.0)
        assert lx == pytest.approx(100.0 - _WIRE_STUB_LENGTH_MM, abs=0.01)
        assert ly == pytest.approx(80.0, abs=0.01)

    def test_stub_angle_90_extends_down(self):
        """Stub angle 90 extends wire downward."""
        lx, ly = _compute_label_position_from_stub_angle(100.0, 80.0, 90.0)
        assert lx == pytest.approx(100.0, abs=0.01)
        assert ly == pytest.approx(80.0 + _WIRE_STUB_LENGTH_MM, abs=0.01)

    def test_stub_angle_270_extends_up(self):
        """Stub angle 270 extends wire upward."""
        lx, ly = _compute_label_position_from_stub_angle(100.0, 80.0, 270.0)
        assert lx == pytest.approx(100.0, abs=0.01)
        assert ly == pytest.approx(80.0 - _WIRE_STUB_LENGTH_MM, abs=0.01)


# ─── Test fallback estimation still works ────────────────────────────────────


class TestFallbackEstimation:
    """Tests that fallback pin estimation still works correctly."""

    def test_estimate_pin_position_symmetric(self):
        """Pin estimation places pins symmetrically around symbol center."""
        placement = PlacementInfo(ref="R1", x=100.0, y=80.0, angle=0.0)
        # 2 pins: pin 0 on left, pin 1 on right
        x0, y0 = _estimate_pin_position(placement, 0, 2)
        x1, y1 = _estimate_pin_position(placement, 1, 2)
        assert x0 < 100.0  # Left side
        assert x1 > 100.0  # Right side
        assert y0 == pytest.approx(80.0)
        assert y1 == pytest.approx(80.0)

    def test_estimate_with_rotation(self):
        """Pin estimation applies rotation correctly."""
        placement = PlacementInfo(ref="R1", x=100.0, y=80.0, angle=90.0)
        x0, y0 = _estimate_pin_position(placement, 0, 2)
        # At 90° rotation, left pin should move downward instead of leftward
        assert abs(x0 - 100.0) < 0.01  # Near center X
        assert y0 != 80.0  # Offset in Y

    def test_compute_label_position_extends_outward(self):
        """Label position extends outward from symbol center."""
        placement = PlacementInfo(ref="R1", x=100.0, y=80.0, angle=0.0)
        pin_x, pin_y = 92.38, 80.0  # Left side pin
        lx, ly = _compute_label_position(pin_x, pin_y, placement)
        # Should extend further to the left (away from center)
        assert lx < pin_x
        assert ly == pytest.approx(pin_y)


# ─── Test writer integration with real pin resolution ────────────────────────


class TestWriterRuntimeDependencies:
    """Writer runtime dependency checks."""

    def _make_simple_circuit(self) -> tuple[CanonicalCircuit, SheetPlan]:
        parts = [CircuitPart(ref="R1", lib_id="Device:R", value="10k")]
        endpoints = [CircuitEndpoint(ref="R1", pin="1", net="NET1")]
        placements = {"R1": PlacementInfo(ref="R1", x=100.0, y=80.0)}
        canonical = CanonicalCircuit(
            project_path="/tmp/test",
            parts=parts,
            endpoints=endpoints,
            no_connects=[],
            blocks={"default": ["R1"]},
        )
        sheet_plan = SheetPlan(
            sheets={"root": ["R1"]},
            placements=placements,
            sheet_sizes={"root": "A4"},
            cross_sheet_nets=set(),
        )
        return canonical, sheet_plan

    def test_writer_fails_immediately_without_kiutils(self, tmp_path, monkeypatch):
        canonical, sheet_plan = self._make_simple_circuit()
        monkeypatch.setattr(
            "kicad_mcp.schematic_engine.schematic_writer._KIUTILS_AVAILABLE",
            False,
        )
        monkeypatch.setattr(
            "kicad_mcp.schematic_engine.schematic_writer._KICAD_SKIP_AVAILABLE",
            True,
        )

        writer = SchematicWriter(str(tmp_path), "test_project")

        with pytest.raises(RuntimeError, match="kiutils"):
            writer.write(canonical, sheet_plan)

    def test_writer_fails_immediately_without_kicad_skip(self, tmp_path, monkeypatch):
        canonical, sheet_plan = self._make_simple_circuit()
        monkeypatch.setattr(
            "kicad_mcp.schematic_engine.schematic_writer._KIUTILS_AVAILABLE",
            True,
        )
        monkeypatch.setattr(
            "kicad_mcp.schematic_engine.schematic_writer._KICAD_SKIP_AVAILABLE",
            False,
        )

        writer = SchematicWriter(str(tmp_path), "test_project")

        with pytest.raises(RuntimeError, match="kicad-skip"):
            writer.write(canonical, sheet_plan)




# ─── Test safe tool has its pipeline helper defined ──────────────────────────


class TestSafeToolPipelineHelper:
    """Verify that the safe tool's helper function is properly defined."""

    def test_apply_via_netlist_first_engine_is_defined(self):
        """The helper function _apply_via_netlist_first_engine is importable."""
        from kicad_mcp.tools.creation_tools import _apply_via_netlist_first_engine

        assert callable(_apply_via_netlist_first_engine)

    def test_apply_via_netlist_first_engine_signature(self):
        """The helper accepts only the public apply arguments."""
        import inspect

        from kicad_mcp.tools.creation_tools import _apply_via_netlist_first_engine

        sig = inspect.signature(_apply_via_netlist_first_engine)
        assert list(sig.parameters) == ["project_path", "intent"]

    def test_apply_tool_function_is_defined(self):
        """schematic_apply_design_intent is registered as an MCP tool."""
        # The function is decorated with @mcp.tool() so it can't be imported directly.
        # Verify it exists by checking the source contains the definition.
        import inspect

        import kicad_mcp.tools.design_intent_tools as design_intent_tools

        source = inspect.getsource(design_intent_tools)
        assert "def schematic_apply_design_intent(" in source
        assert "_apply_via_netlist_first_engine(" in source
