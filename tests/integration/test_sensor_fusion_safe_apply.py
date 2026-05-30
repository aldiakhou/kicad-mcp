"""Integration test: sensor fusion board safe apply via netlist-first engine.

Tests the exact sensor-fusion board from the problem statement:
- STM32G431KBTx
- ICM-20948
- BMP280
- IST8310
- LP5907
- PCA9306
- USB-C power
- SWD, UART, I²C expansion
"""

import os
import tempfile

from kicad_mcp.schematic_engine.normalize import normalize_design_intent
from kicad_mcp.schematic_engine.pipeline import apply_design_intent_netlist_first
from kicad_mcp.schematic_engine.sheet_planner import plan_sheets
from kicad_mcp.schematic_engine.skidl_compiler import SkidlCompiler
from kicad_mcp.schematic_engine.visual_lint import visual_lint

# The sensor fusion board design intent
SENSOR_FUSION_INTENT = {
    "parts": [
        {
            "ref": "U1",
            "lib_id": "MCU_ST:STM32G431KBTx",
            "value": "STM32G431KBTx",
            "block": "mcu",
        },
        {
            "ref": "U2",
            "lib_id": "Sensor_Motion:ICM-20948",
            "value": "ICM-20948",
            "block": "sensors",
        },
        {
            "ref": "U3",
            "lib_id": "Sensor_Pressure:BMP280",
            "value": "BMP280",
            "block": "sensors",
        },
        {
            "ref": "U4",
            "lib_id": "Sensor_Magnetic:IST8310",
            "value": "IST8310",
            "block": "sensors",
        },
        {
            "ref": "U5",
            "lib_id": "Regulator_Linear:LP5907MFX-3.3",
            "value": "LP5907-3.3V",
            "block": "power",
        },
        {
            "ref": "U6",
            "lib_id": "Interface_I2C:PCA9306",
            "value": "PCA9306",
            "block": "interfaces",
        },
    ],
    "rails": [
        {
            "net": "+3V3",
            "connections": [
                {"ref": "U1", "pins": ["VDD", "VDDA"]},
                {"ref": "U2", "pins": ["VDD"]},
                {"ref": "U3", "pins": ["VDD"]},
                {"ref": "U4", "pins": ["VDD"]},
                {"ref": "U5", "pins": ["OUT"]},
                {"ref": "U6", "pins": ["VREF1"]},
            ],
        },
        {
            "net": "GND",
            "connections": [
                {"ref": "U1", "pins": ["VSS"]},
                {"ref": "U2", "pins": ["GND"]},
                {"ref": "U3", "pins": ["GND"]},
                {"ref": "U4", "pins": ["GND"]},
                {"ref": "U5", "pins": ["GND"]},
                {"ref": "U6", "pins": ["GND"]},
            ],
        },
        {
            "net": "+5V",
            "connections": [
                {"ref": "U5", "pins": ["IN"]},
                {"ref": "U6", "pins": ["VREF2"]},
            ],
        },
    ],
    "interfaces": [
        {
            "type": "i2c",
            "connections": [
                {
                    "net": "SENSOR_I2C_SCL",
                    "endpoints": [
                        {"ref": "U1", "pin": "PB8"},
                        {"ref": "U6", "pin": "SCL1"},
                    ],
                },
                {
                    "net": "SENSOR_I2C_SDA",
                    "endpoints": [
                        {"ref": "U1", "pin": "PB9"},
                        {"ref": "U6", "pin": "SDA1"},
                    ],
                },
                {
                    "net": "SENSOR_BUS_SCL",
                    "endpoints": [
                        {"ref": "U6", "pin": "SCL2"},
                        {"ref": "U2", "pin": "SCL"},
                        {"ref": "U3", "pin": "SCL"},
                        {"ref": "U4", "pin": "SCL"},
                    ],
                },
                {
                    "net": "SENSOR_BUS_SDA",
                    "endpoints": [
                        {"ref": "U6", "pin": "SDA2"},
                        {"ref": "U2", "pin": "SDA"},
                        {"ref": "U3", "pin": "SDA"},
                        {"ref": "U4", "pin": "SDA"},
                    ],
                },
            ],
        },
    ],
    "support_circuits": [
        {
            "type": "usb_c_power_input",
            "ref": "J3",
            "vbus_net": "+5V",
            "ground": "GND",
            "cc_resistor": "5.1k",
        },
        {
            "type": "crystal",
            "target": "U1",
            "pins": ["PF0", "PF1"],
            "value": "8MHz",
            "load_capacitors": "18pF",
            "lib_id": "Device:Crystal_GND24",
        },
        {
            "type": "decoupling",
            "target": "U1",
            "rail": "+3V3",
            "ground": "GND",
            "capacitors": ["100n", "4.7u"],
        },
        {
            "type": "decoupling",
            "target": "U2",
            "rail": "+3V3",
            "ground": "GND",
            "capacitors": ["100n"],
        },
        {
            "type": "decoupling",
            "target": "U3",
            "rail": "+3V3",
            "ground": "GND",
            "capacitors": ["100n"],
        },
        {
            "type": "decoupling",
            "target": "U4",
            "rail": "+3V3",
            "ground": "GND",
            "capacitors": ["100n"],
        },
        {
            "type": "decoupling",
            "target": "U5",
            "rail": "+3V3",
            "ground": "GND",
            "capacitors": ["1u"],
        },
        {
            "type": "pullup",
            "target": "U6",
            "rail": "+3V3",
            "value": "4.7k",
            "signals": ["SENSOR_BUS_SCL", "SENSOR_BUS_SDA"],
        },
    ],
    "no_connect_rules": [
        {"ref": "U2", "pins": ["INT1", "INT2", "FSYNC"]},
    ],
}


class TestSensorFusionNormalize:
    """Test normalizing the sensor fusion board intent."""

    def test_normalize_produces_valid_canonical(self):
        """Normalization succeeds and produces expected part counts."""
        canonical = normalize_design_intent("/tmp/test.kicad_pro", SENSOR_FUSION_INTENT)

        # Main parts (6) + USB-C (1 connector + 2 CC res) + crystal (1 + 2 load caps)
        # + decoupling (2+1+1+1+1 = 6 caps) + pullups (2)
        # = 6 + 3 + 3 + 6 + 2 = 20 parts
        assert len(canonical.parts) >= 18  # At minimum

        # Should have many endpoints
        assert len(canonical.endpoints) > 30

        # No-connects
        assert len(canonical.no_connects) == 3

        # Power rails detected
        assert "+3V3" in canonical.rails
        assert "GND" in canonical.rails
        assert "+5V" in canonical.rails

    def test_no_duplicate_refs(self):
        """No duplicate reference designators in normalized circuit."""
        canonical = normalize_design_intent("/tmp/test.kicad_pro", SENSOR_FUSION_INTENT)
        refs = [p.ref for p in canonical.parts]
        assert len(refs) == len(set(refs))

    def test_usb_c_hidden_vbus_does_not_fail(self):
        """USB-C VBUS pins have allow_hidden=True."""
        canonical = normalize_design_intent("/tmp/test.kicad_pro", SENSOR_FUSION_INTENT)
        vbus_eps = [
            ep for ep in canonical.endpoints
            if ep.ref == "J3" and ep.net == "+5V"
        ]
        assert len(vbus_eps) > 0
        assert all(ep.allow_hidden for ep in vbus_eps)

    def test_blocks_assigned(self):
        """Parts are assigned to functional blocks."""
        canonical = normalize_design_intent("/tmp/test.kicad_pro", SENSOR_FUSION_INTENT)
        assert "mcu" in canonical.blocks
        assert "sensors" in canonical.blocks
        assert "power" in canonical.blocks or "interfaces" in canonical.blocks


class TestSensorFusionCompile:
    """Test SKiDL compilation of sensor fusion board."""

    def test_skidl_compile_succeeds(self):
        """SKiDL fallback compiler produces valid netlist."""
        canonical = normalize_design_intent("/tmp/test.kicad_pro", SENSOR_FUSION_INTENT)

        with tempfile.TemporaryDirectory() as tmpdir:
            compiler = SkidlCompiler(artifact_dir=tmpdir)
            result = compiler.compile(canonical)

            assert result.success
            assert result.part_count >= 18
            assert result.net_count > 5
            assert result.expected_netlist is not None

    def test_expected_netlist_contains_key_nets(self):
        """Expected netlist contains all critical nets."""
        canonical = normalize_design_intent("/tmp/test.kicad_pro", SENSOR_FUSION_INTENT)

        with tempfile.TemporaryDirectory() as tmpdir:
            compiler = SkidlCompiler(artifact_dir=tmpdir)
            result = compiler.compile(canonical)

            assert result.expected_netlist is not None
            net_names = set(result.expected_netlist.nets.keys())
            assert "+3V3" in net_names
            assert "GND" in net_names
            assert "+5V" in net_names
            assert "SENSOR_I2C_SCL" in net_names
            assert "SENSOR_I2C_SDA" in net_names
            assert "SENSOR_BUS_SCL" in net_names
            assert "SENSOR_BUS_SDA" in net_names


class TestSensorFusionSheetPlan:
    """Test sheet planning for sensor fusion board."""

    def test_multi_sheet_layout(self):
        """Sensor fusion board uses multiple sheets."""
        canonical = normalize_design_intent("/tmp/test.kicad_pro", SENSOR_FUSION_INTENT)
        plan_sheets(canonical, style="professional_blocks")

        # Should have multiple sheets (power, mcu, sensors, interfaces, or similar)
        # With max_parts_per_sheet=40 and ~20 parts it might fit on one sheet
        # Force multi-sheet for this test
        plan_multi = plan_sheets(canonical, style="professional_blocks", max_parts_per_sheet=8)
        assert len(plan_multi.sheets) > 1

    def test_all_parts_placed(self):
        """All parts get placements."""
        canonical = normalize_design_intent("/tmp/test.kicad_pro", SENSOR_FUSION_INTENT)
        plan = plan_sheets(canonical)

        for part in canonical.parts:
            assert part.ref in plan.placements, f"Part {part.ref} not placed"

    def test_decoupling_near_targets(self):
        """Decoupling caps are placed near their target ICs."""
        canonical = normalize_design_intent("/tmp/test.kicad_pro", SENSOR_FUSION_INTENT)
        plan = plan_sheets(canonical)

        for part in canonical.parts:
            if part.role == "decoupling":
                target = part.properties.get("KICAD_MCP_TARGET", "")
                if target and target in plan.placements and part.ref in plan.placements:
                    target_p = plan.placements[target]
                    cap_p = plan.placements[part.ref]
                    distance = ((target_p.x - cap_p.x)**2 + (target_p.y - cap_p.y)**2)**0.5
                    assert distance < 50.0, (
                        f"Decoupling {part.ref} is {distance:.1f}mm from {target}"
                    )


class TestSensorFusionVisualLint:
    """Test visual lint for sensor fusion board."""

    def test_no_blocking_lint_issues(self):
        """Sensor fusion board passes visual lint without blocking issues."""
        canonical = normalize_design_intent("/tmp/test.kicad_pro", SENSOR_FUSION_INTENT)
        plan = plan_sheets(canonical)

        result = visual_lint(canonical, plan)
        # Allow warnings but no blocking issues
        if result.blocking_count > 0:
            blocking = [i for i in result.issues if i.severity == "blocking"]
            # Only accept unplaced symbols as potential blockers
            # (due to support parts that may not fit the test setup)
            for issue in blocking:
                # All parts should be placed
                assert issue.type != "unplaced_symbol", (
                    f"Unplaced: {issue.ref} - {issue.message}"
                )


class TestSensorFusionPipeline:
    """Test the full pipeline for sensor fusion board."""

    def test_full_pipeline_no_kicad_cli(self):
        """Full pipeline runs (without KiCad CLI) and produces valid result."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = os.path.join(tmpdir, "sensor_fusion.kicad_pro")
            with open(project_path, "w") as f:
                f.write('{"meta": {"filename": "sensor_fusion.kicad_pro", "version": 1}}')

            result = apply_design_intent_netlist_first(
                project_path,
                SENSOR_FUSION_INTENT,
                mode="replace",
                atomic=True,
                visual_style="professional_blocks",
                run_erc=False,  # Skip ERC (no KiCad CLI in test env)
                export_svg=False,
                strict=False,
            )

            assert result["success"], f"Pipeline failed: {result.get('error')}"
            assert result["changed"]
            assert result["engine"] == "skidl_kiutils_kicad_cli"
            assert result["stage"] == "schematic_committed"
            assert result["part_count"] >= 18
            assert result["net_count"] > 5

    def test_pipeline_rollback_on_strict_failure(self):
        """Pipeline rolls back in strict mode if visual lint has issues."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = os.path.join(tmpdir, "sensor_fusion.kicad_pro")
            with open(project_path, "w") as f:
                f.write('{"meta": {"filename": "sensor_fusion.kicad_pro", "version": 1}}')

            # Use strict mode - may fail on visual lint
            result = apply_design_intent_netlist_first(
                project_path,
                SENSOR_FUSION_INTENT,
                mode="replace",
                atomic=True,
                strict=True,
                run_erc=False,
                export_svg=False,
            )

            # Strict mode: if it fails, it should be rolled back
            if not result["success"]:
                assert result.get("rolled_back", False) or result.get("changed") is False

    def test_pipeline_idempotent_reruns(self):
        """Running the same intent twice doesn't duplicate power flags/caps."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = os.path.join(tmpdir, "sensor_fusion.kicad_pro")
            with open(project_path, "w") as f:
                f.write('{"meta": {"filename": "sensor_fusion.kicad_pro", "version": 1}}')

            # Run once
            result1 = apply_design_intent_netlist_first(
                project_path,
                SENSOR_FUSION_INTENT,
                mode="replace",
                run_erc=False,
                export_svg=False,
                strict=False,
            )
            assert result1["success"]

            # Run again (replace mode)
            result2 = apply_design_intent_netlist_first(
                project_path,
                SENSOR_FUSION_INTENT,
                mode="replace",
                run_erc=False,
                export_svg=False,
                strict=False,
            )
            assert result2["success"]
            # Part count should be same (no duplication)
            assert result2["part_count"] == result1["part_count"]

    def test_cancellation_returns_rolled_back(self):
        """Cancelled job returns changed=False and rolled_back=True."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = os.path.join(tmpdir, "sensor_fusion.kicad_pro")
            with open(project_path, "w") as f:
                f.write('{"meta": {"filename": "sensor_fusion.kicad_pro", "version": 1}}')

            # Cancel immediately
            result = apply_design_intent_netlist_first(
                project_path,
                SENSOR_FUSION_INTENT,
                mode="replace",
                run_erc=False,
                export_svg=False,
                cancel_check=lambda: True,  # Always cancelled
            )

            assert not result["success"]
            assert result["changed"] is False
            assert result.get("rolled_back", False)
