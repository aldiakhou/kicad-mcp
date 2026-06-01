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

    def test_missing_lib_id_or_pins_raises(self):
        """Parts without lib_id or custom pins raise ValueError."""
        intent = {"parts": [{"ref": "R1", "value": "10k"}]}
        with pytest.raises(ValueError, match="missing 'lib_id' or custom 'pins'"):
            normalize_design_intent("/tmp/test.kicad_pro", intent)

    def test_pins_only_custom_part(self):
        """Pins-only custom parts are accepted and converted to inline symbols."""
        intent = {
            "parts": [
                {
                    "ref": "U3",
                    "value": "DPS310",
                    "footprint": "Package_LGA:LGA-8_2x2mm_P0.5mm",
                    "pins": [
                        {"number": "1", "name": "SCL", "pintype": "input"},
                        {"number": "2", "name": "SDA", "pintype": "bidirectional"},
                        {"number": "3", "name": "GND", "pintype": "power_in"},
                    ],
                }
            ]
        }

        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)

        part = canonical.part_by_ref("U3")
        assert part is not None
        assert part.lib_id == "kicad_mcp:DPS310"
        assert "KICAD_MCP_CUSTOM_PINS" in part.properties

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

    def test_object_style_rails(self):
        """Object-style rails from the public schema are accepted."""
        intent = {
            "parts": [{"ref": "U1", "lib_id": "Device:R", "value": "10k"}],
            "rails": {
                "+3V3": {"pins": [["U1", "1"]]},
                "GND": {"pins": [["U1", "2"]]},
            },
        }

        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)

        assert {ep.net for ep in canonical.endpoints} == {"+3V3", "GND"}

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

    def test_bulk_connections_public_pins_shorthand(self):
        """Public schema pins shorthand generates endpoints."""
        intent = {
            "parts": [{"ref": "U1", "lib_id": "Device:R", "value": "10k"}],
            "bulk_connections": [{"net": "MY_NET", "pins": [["U1", "1"], "U1:2"]}],
        }
        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)
        assert {(ep.ref, ep.pin, ep.net) for ep in canonical.endpoints} == {
            ("U1", "1", "MY_NET"),
            ("U1", "2", "MY_NET"),
        }

    def test_i2c_interface_public_schema(self):
        """Controller/devices I2C schema generates SCL/SDA endpoints."""
        intent = {
            "parts": [
                {"ref": "U1", "lib_id": "Device:R", "value": "MCU"},
                {"ref": "U2", "lib_id": "Device:R", "value": "Sensor"},
            ],
            "interfaces": [
                {
                    "type": "i2c",
                    "name": "SENSOR_I2C",
                    "controller": {"ref": "U1", "scl": "1", "sda": "2"},
                    "devices": [{"ref": "U2", "scl": "1", "sda": "2"}],
                }
            ],
        }
        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)
        assert len([ep for ep in canonical.endpoints if ep.net == "SENSOR_I2C_SCL"]) == 2
        assert len([ep for ep in canonical.endpoints if ep.net == "SENSOR_I2C_SDA"]) == 2

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

    def test_no_connect_rules_match_expand_and_skip_connected_pins(self):
        """Match-based no-connect rules resolve symbol pins before writing."""
        intent = {
            "parts": [
                {
                    "ref": "U1",
                    "value": "CUSTOM_MCU",
                    "pins": [
                        {"number": "1", "name": "PB6", "pintype": "bidirectional"},
                        {"number": "2", "name": "PB7", "pintype": "bidirectional"},
                        {"number": "3", "name": "PA5", "pintype": "bidirectional"},
                    ],
                }
            ],
            "bulk_connections": [{"net": "I2C_SCL", "pins": [["U1", "PB6"]]}],
            "no_connect_rules": [
                {
                    "ref": "U1",
                    "match": {"name_regex": "^(PA|PB)[0-9]+$"},
                    "except": ["PB7"],
                    "action": "mark_no_connect",
                }
            ],
        }

        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)

        assert canonical.no_connects == [("U1", "PA5")]
        assert canonical.no_connect_summary["emitted_count"] == 1
        assert canonical.no_connect_summary["skipped_connected_count"] == 1

    def test_no_connect_rules_report_zero_pin_matches(self):
        intent = {
            "parts": [
                {
                    "ref": "U1",
                    "value": "CUSTOM_MCU",
                    "pins": [
                        {"number": "1", "name": "PB6", "pintype": "bidirectional"},
                    ],
                }
            ],
            "no_connect_rules": [
                {
                    "ref": "U1",
                    "match": {"name_regex": "^PC[0-9]+$"},
                    "action": "mark_no_connect",
                }
            ],
        }

        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)

        assert canonical.no_connect_summary["matched_zero_pins_count"] == 1
        assert canonical.no_connect_summary["matched_zero_pins"][0]["ref"] == "U1"
        assert canonical.no_connect_summary["unmatched_rule_count"] == 1

    def test_usb_d_plus_and_d_minus_pin_keys_do_not_collapse(self):
        intent = {
            "parts": [
                {
                    "ref": "J1",
                    "value": "USB_C",
                    "pins": [
                        {"number": "A6", "name": "D+", "pintype": "bidirectional"},
                        {"number": "A7", "name": "D-", "pintype": "bidirectional"},
                    ],
                }
            ],
            "bulk_connections": [
                {"net": "USB_D_P", "pins": [["J1", "D+"]]},
                {"net": "USB_D_N", "pins": [["J1", "D-"]]},
            ],
        }

        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)

        assert {endpoint.net for endpoint in canonical.endpoints} == {
            "USB_D_P",
            "USB_D_N",
        }

    def test_conflicting_pin_alias_assignments_raise(self):
        """The same physical pin cannot be assigned to two different nets."""
        intent = {
            "parts": [
                {
                    "ref": "U1",
                    "value": "REG",
                    "pins": [{"number": "5", "name": "VOUT", "pintype": "power_out"}],
                }
            ],
            "bulk_connections": [
                {"net": "+3V3", "pins": [["U1", "VOUT"]]},
                {"net": "GND", "pins": [["U1", "5"]]},
            ],
        }

        with pytest.raises(ValueError, match="same ref/pin assigned to multiple nets"):
            normalize_design_intent("/tmp/test.kicad_pro", intent)

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

    def test_grouped_support_circuits_do_not_crash(self):
        """Grouped support_circuits object is normalized before expansion."""
        intent = {
            "parts": [{"ref": "U1", "lib_id": "Device:R", "value": "10k"}],
            "support_circuits": {
                "decoupling": [
                    {"target": "U1", "rail": "+3V3", "ground": "GND", "capacitors": ["100n"]}
                ],
                "pullup": [
                    {"target": "U1", "pin": "1", "net": "RESET_N", "rail": "+3V3", "value": "10k"}
                ],
            },
        }

        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)

        assert [part.ref for part in canonical.parts if part.role == "decoupling"] == ["C1"]
        assert [part.ref for part in canonical.parts if part.role == "pullup"] == ["R1"]
        assert any(ep.ref == "U1" and ep.pin == "1" and ep.net == "RESET_N" for ep in canonical.endpoints)

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
        assert crystal_parts[0].ref == "Y1"
        assert {part.ref for part in load_cap_parts} == {"C1", "C2"}

    def test_duplicate_crystals_allocate_unique_refs(self):
        """Generated crystal support parts use unique standard refs across targets."""
        intent = {
            "parts": [
                {"ref": "U1", "lib_id": "MCU_ST:STM32G431KBTx", "value": "STM32"},
                {"ref": "U5", "lib_id": "MCU_ST:STM32G431KBTx", "value": "STM32"},
            ],
            "support_circuits": [
                {"type": "crystal", "target": "U1", "pins": ["PF0", "PF1"], "load_capacitors": "18pF"},
                {"type": "crystal", "target": "U5", "pins": ["PF0", "PF1"], "load_capacitors": "18pF"},
            ],
        }

        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)

        refs = [part.ref for part in canonical.parts]
        assert len(refs) == len(set(refs))
        assert {part.ref for part in canonical.parts if part.role == "crystal"} == {"Y1", "Y2"}

    def test_duplicate_decoupling_groups_allocate_unique_refs(self):
        """Decoupling groups allocate unique capacitor refs without target-derived names."""
        intent = {
            "parts": [
                {"ref": "U1", "lib_id": "MCU_ST:STM32G431KBTx", "value": "STM32"},
                {"ref": "U5", "lib_id": "MCU_ST:STM32G431KBTx", "value": "STM32"},
            ],
            "support_circuits": [
                {"type": "decoupling", "target": "U1", "capacitors": ["100n", "1u"]},
                {"type": "decoupling", "target": "U5", "capacitors": ["100n"]},
            ],
        }

        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)

        decap_refs = [part.ref for part in canonical.parts if part.role == "decoupling"]
        assert decap_refs == ["C1", "C2", "C3"]

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
