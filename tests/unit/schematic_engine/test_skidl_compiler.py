"""Tests for the SKiDL compiler."""

import json
import os
import tempfile

from kicad_mcp.schematic_engine.models import CanonicalCircuit, CircuitEndpoint, CircuitPart
from kicad_mcp.schematic_engine.skidl_compiler import SkidlCompiler


class TestSkidlCompiler:
    """Tests for the SkidlCompiler fallback (pure-Python) path."""

    def _make_canonical(self, parts=None, endpoints=None, no_connects=None):
        """Create a test canonical circuit."""
        return CanonicalCircuit(
            project_path="/tmp/test.kicad_pro",
            parts=parts or [],
            endpoints=endpoints or [],
            no_connects=no_connects or [],
            blocks={},
            rails=set(),
        )

    def test_empty_circuit(self):
        """Empty circuit compiles successfully."""
        compiler = SkidlCompiler(artifact_dir=tempfile.mkdtemp())
        canonical = self._make_canonical()
        result = compiler.compile(canonical)
        assert result.success
        assert result.part_count == 0
        assert result.net_count == 0

    def test_basic_circuit_with_nets(self):
        """Basic circuit with parts and endpoints."""
        parts = [
            CircuitPart(ref="R1", lib_id="Device:R", value="10k"),
            CircuitPart(ref="R2", lib_id="Device:R", value="4.7k"),
        ]
        endpoints = [
            CircuitEndpoint(ref="R1", pin="1", net="+3V3"),
            CircuitEndpoint(ref="R1", pin="2", net="SIG"),
            CircuitEndpoint(ref="R2", pin="1", net="SIG"),
            CircuitEndpoint(ref="R2", pin="2", net="GND"),
        ]
        canonical = self._make_canonical(parts=parts, endpoints=endpoints)

        with tempfile.TemporaryDirectory() as tmpdir:
            compiler = SkidlCompiler(artifact_dir=tmpdir)
            result = compiler.compile(canonical)

            assert result.success
            assert result.part_count == 2
            assert result.net_count == 3  # +3V3, SIG, GND
            assert result.endpoint_count == 4

    def test_expected_netlist_json_saved(self):
        """Expected netlist JSON artifact is saved."""
        parts = [CircuitPart(ref="R1", lib_id="Device:R", value="10k")]
        endpoints = [
            CircuitEndpoint(ref="R1", pin="1", net="+3V3"),
            CircuitEndpoint(ref="R1", pin="2", net="GND"),
        ]
        canonical = self._make_canonical(parts=parts, endpoints=endpoints)

        with tempfile.TemporaryDirectory() as tmpdir:
            compiler = SkidlCompiler(artifact_dir=tmpdir)
            result = compiler.compile(canonical)

            assert result.success
            assert result.expected_netlist_path is not None
            assert os.path.exists(result.expected_netlist_path)

            with open(result.expected_netlist_path) as f:
                data = json.load(f)
            assert "+3V3" in data["nets"]
            assert "GND" in data["nets"]
            assert data["metadata"]["part_count"] == 1

    def test_expected_netlist_content(self):
        """Expected netlist contains correct pin assignments."""
        parts = [
            CircuitPart(ref="U1", lib_id="MCU_ST:STM32", value="STM32"),
            CircuitPart(ref="U2", lib_id="Sensor:BMP280", value="BMP280"),
        ]
        endpoints = [
            CircuitEndpoint(ref="U1", pin="PB8", net="I2C_SCL"),
            CircuitEndpoint(ref="U2", pin="SCL", net="I2C_SCL"),
            CircuitEndpoint(ref="U1", pin="PB9", net="I2C_SDA"),
            CircuitEndpoint(ref="U2", pin="SDA", net="I2C_SDA"),
        ]
        canonical = self._make_canonical(parts=parts, endpoints=endpoints)

        with tempfile.TemporaryDirectory() as tmpdir:
            compiler = SkidlCompiler(artifact_dir=tmpdir)
            result = compiler.compile(canonical)

            assert result.success
            assert result.expected_netlist is not None
            netlist_dict = result.expected_netlist.to_dict()
            assert "I2C_SCL" in netlist_dict
            scl_entries = netlist_dict["I2C_SCL"]
            refs_pins = {(e["ref"], e["pin"]) for e in scl_entries}
            assert ("U1", "PB8") in refs_pins
            assert ("U2", "SCL") in refs_pins

    def test_large_circuit(self):
        """Compiler handles large circuits."""
        parts = [
            CircuitPart(ref=f"R{i}", lib_id="Device:R", value="10k")
            for i in range(100)
        ]
        endpoints = [
            CircuitEndpoint(ref=f"R{i}", pin="1", net=f"NET_{i // 10}")
            for i in range(100)
        ]
        canonical = self._make_canonical(parts=parts, endpoints=endpoints)

        with tempfile.TemporaryDirectory() as tmpdir:
            compiler = SkidlCompiler(artifact_dir=tmpdir)
            result = compiler.compile(canonical)

            assert result.success
            assert result.part_count == 100
            assert result.net_count == 10
