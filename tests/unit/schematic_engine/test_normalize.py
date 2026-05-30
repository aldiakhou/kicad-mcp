"""Tests for the canonical intent normalizer."""

import pytest

from kicad_mcp.schematic_engine.normalize import normalize_design_intent


class TestNormalizeDesignIntent:
    """Tests for normalize_design_intent function."""

    def test_empty_intent(self):
        """Empty intent produces empty canonical circuit."""
        canonical = normalize_design_intent("/tmp/test.kicad_pro", {})
        assert canonical.parts == []
        assert canonical.endpoints == []
        assert canonical.no_connects == []

    def test_basic_parts(self):
        """Parts are correctly normalized."""
        intent = {
            "parts": [
                {"ref": "U1", "lib_id": "MCU_ST:STM32G431KBTx", "value": "STM32G431KBTx"},
                {"ref": "R1", "lib_id": "Device:R", "value": "10k"},
            ]
        }
        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)
        assert len(canonical.parts) == 2
        assert canonical.parts[0].ref == "U1"
        assert canonical.parts[0].lib_id == "MCU_ST:STM32G431KBTx"
        assert canonical.parts[1].ref == "R1"
        assert canonical.parts[1].footprint == "Resistor_SMD:R_0402_1005Metric"

    def test_duplicate_refs_raises(self):
        """Duplicate reference designators raise ValueError."""
        intent = {
            "parts": [
                {"ref": "U1", "lib_id": "Device:R", "value": "10k"},
                {"ref": "U1", "lib_id": "Device:C", "value": "100n"},
            ]
        }
        with pytest.raises(ValueError, match="Duplicate reference designator"):
            normalize_design_intent("/tmp/test.kicad_pro", intent)

    def test_missing_ref_raises(self):
        """Parts without ref raise ValueError."""
        intent = {"parts": [{"lib_id": "Device:R", "value": "10k"}]}
        with pytest.raises(ValueError, match="missing 'ref'"):
            normalize_design_intent("/tmp/test.kicad_pro", intent)

    def test_missing_lib_id_raises(self):
        """Parts without lib_id raise ValueError."""
        intent = {"parts": [{"ref": "R1", "value": "10k"}]}
        with pytest.raises(ValueError, match="missing 'lib_id'"):
            normalize_design_intent("/tmp/test.kicad_pro", intent)

    def test_rails(self):
        """Rails generate correct endpoints."""
        intent = {
            "parts": [{"ref": "U1", "lib_id": "MCU_ST:STM32G431KBTx", "value": "STM32"}],
            "rails": [
                {
                    "net": "+3V3",
                    "connections": [
                        {"ref": "U1", "pins": ["VDD", "VDDA"]},
                    ],
                }
            ],
        }
        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)
        assert "+3V3" in canonical.rails
        vdd_eps = [ep for ep in canonical.endpoints if ep.net == "+3V3"]
        assert len(vdd_eps) == 2
        assert vdd_eps[0].ref == "U1"
        assert vdd_eps[0].pin == "VDD"

    def test_interfaces(self):
        """Interfaces with connections generate endpoints."""
        intent = {
            "parts": [
                {"ref": "U1", "lib_id": "MCU_ST:STM32G431KBTx", "value": "STM32"},
                {"ref": "U2", "lib_id": "Sensor:ICM-20948", "value": "ICM-20948"},
            ],
            "interfaces": [
                {
                    "type": "i2c",
                    "connections": [
                        {
                            "net": "SENSOR_I2C_SCL",
                            "endpoints": [
                                {"ref": "U1", "pin": "PB8"},
                                {"ref": "U2", "pin": "SCL"},
                            ],
                        },
                    ],
                }
            ],
        }
        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)
        scl_eps = [ep for ep in canonical.endpoints if ep.net == "SENSOR_I2C_SCL"]
        assert len(scl_eps) == 2

    def test_bulk_connections(self):
        """Bulk connections generate endpoints."""
        intent = {
            "parts": [{"ref": "U1", "lib_id": "Device:R", "value": "10k"}],
            "bulk_connections": [
                {
                    "net": "MY_NET",
                    "endpoints": [
                        {"ref": "U1", "pin": "1"},
                    ],
                }
            ],
        }
        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)
        assert len(canonical.endpoints) == 1
        assert canonical.endpoints[0].net == "MY_NET"

    def test_no_connect_rules(self):
        """No-connect rules are captured."""
        intent = {
            "parts": [{"ref": "U1", "lib_id": "Device:R", "value": "10k"}],
            "no_connect_rules": [
                {"ref": "U1", "pins": ["NC1", "NC2"]},
            ],
        }
        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)
        assert ("U1", "NC1") in canonical.no_connects
        assert ("U1", "NC2") in canonical.no_connects

    def test_decoupling_support_circuit(self):
        """Decoupling support circuits generate parts and endpoints."""
        intent = {
            "parts": [{"ref": "U2", "lib_id": "Sensor:BMP280", "value": "BMP280"}],
            "support_circuits": [
                {
                    "type": "decoupling",
                    "target": "U2",
                    "rail": "+3V3",
                    "ground": "GND",
                    "capacitors": ["100n", "1u"],
                }
            ],
        }
        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)
        # Should have U2 + 2 decoupling caps
        assert len(canonical.parts) == 3
        decap_parts = [p for p in canonical.parts if p.role == "decoupling"]
        assert len(decap_parts) == 2
        assert decap_parts[0].properties["KICAD_MCP_TARGET"] == "U2"

    def test_crystal_support_circuit(self):
        """Crystal support circuits generate crystal + load caps."""
        intent = {
            "parts": [{"ref": "U1", "lib_id": "MCU_ST:STM32G431KBTx", "value": "STM32"}],
            "support_circuits": [
                {
                    "type": "crystal",
                    "target": "U1",
                    "pins": ["PF0", "PF1"],
                    "value": "8MHz",
                    "load_capacitors": "18pF",
                }
            ],
        }
        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)
        crystal_parts = [p for p in canonical.parts if p.role == "crystal"]
        assert len(crystal_parts) == 1
        load_cap_parts = [p for p in canonical.parts if p.role == "load_capacitor"]
        assert len(load_cap_parts) == 2

    def test_usb_c_power_support_circuit(self):
        """USB-C power input generates connector + CC pulldowns."""
        intent = {
            "parts": [],
            "support_circuits": [
                {
                    "type": "usb_c_power_input",
                    "ref": "J3",
                    "vbus_net": "+5V",
                    "ground": "GND",
                    "cc_resistor": "5.1k",
                }
            ],
        }
        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)
        # Should have connector + 2 CC pulldown resistors
        assert len(canonical.parts) == 3
        connector = canonical.part_by_ref("J3")
        assert connector is not None
        assert connector.role == "usb_c_power"
        cc_parts = [p for p in canonical.parts if p.role == "cc_pulldown"]
        assert len(cc_parts) == 2

        # USB-C VBUS pins should have allow_hidden=True
        vbus_eps = [
            ep for ep in canonical.endpoints
            if ep.net == "+5V" and ep.ref == "J3"
        ]
        assert len(vbus_eps) > 0
        assert all(ep.allow_hidden for ep in vbus_eps)

    def test_power_nets_detected_from_endpoints(self):
        """Power nets are automatically detected from endpoint names."""
        intent = {
            "parts": [{"ref": "U1", "lib_id": "Device:R", "value": "10k"}],
            "bulk_connections": [
                {"net": "+3V3", "endpoints": [{"ref": "U1", "pin": "1"}]},
                {"net": "GND", "endpoints": [{"ref": "U1", "pin": "2"}]},
            ],
        }
        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)
        assert "+3V3" in canonical.rails
        assert "GND" in canonical.rails

    def test_block_assignment(self):
        """Parts are assigned to blocks."""
        intent = {
            "parts": [
                {"ref": "U1", "lib_id": "MCU_ST:STM32G431KBTx", "value": "STM32", "block": "mcu"},
                {"ref": "U2", "lib_id": "Sensor:BMP280", "value": "BMP280", "block": "sensors"},
            ],
        }
        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)
        assert "mcu" in canonical.blocks
        assert "sensors" in canonical.blocks
        assert "U1" in canonical.blocks["mcu"]
        assert "U2" in canonical.blocks["sensors"]

    def test_pin_rules(self):
        """Pin rules generate explicit endpoints."""
        intent = {
            "parts": [{"ref": "U1", "lib_id": "Device:R", "value": "10k"}],
            "pin_rules": [
                {"ref": "U1", "pins": ["1", "2"], "net": "MY_NET"},
            ],
        }
        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)
        assert len(canonical.endpoints) == 2
        assert all(ep.source == "pin_rules" for ep in canonical.endpoints)
