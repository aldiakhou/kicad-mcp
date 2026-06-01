"""Tests for the canonical intent normalizer."""

import pytest

import kicad_mcp.schematic_engine.normalize as normalize
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

    def test_grouped_interfaces_expand_documented_shorthand(self):
        """Grouped public interface schema must materialize real endpoints."""
        intent = {
            "parts": [
                {
                    "ref": "U1",
                    "value": "MCU",
                    "pins": [
                        {"number": "1", "name": "PB6", "pintype": "bidirectional"},
                        {"number": "2", "name": "PB7", "pintype": "bidirectional"},
                        {"number": "3", "name": "PA9", "pintype": "output"},
                        {"number": "4", "name": "PA10", "pintype": "input"},
                        {"number": "5", "name": "PA13", "pintype": "bidirectional"},
                        {"number": "6", "name": "PA14", "pintype": "bidirectional"},
                        {"number": "7", "name": "NRST", "pintype": "input"},
                    ],
                },
                {
                    "ref": "U2",
                    "value": "SENSOR",
                    "pins": [
                        {"number": "1", "name": "SCL", "pintype": "input"},
                        {"number": "2", "name": "SDA", "pintype": "bidirectional"},
                    ],
                },
                {
                    "ref": "J2",
                    "value": "UART",
                    "pins": [
                        {"number": "2", "name": "RX", "pintype": "input"},
                        {"number": "3", "name": "TX", "pintype": "output"},
                    ],
                },
            ],
            "interfaces": {
                "i2c": [
                    {
                        "name": "SENSOR_I2C",
                        "controller": {"ref": "U1", "scl": "PB6", "sda": "PB7"},
                        "devices": [{"ref": "U2", "scl": "SCL", "sda": "SDA"}],
                        "pullups": {"rail": "+3V3", "value": "4.7k"},
                    }
                ],
                "uart": [
                    {
                        "name": "DEBUG_UART",
                        "controller": {"ref": "U1", "tx": "PA9", "rx": "PA10"},
                        "device": {"ref": "J2", "rx": "2", "tx": "3"},
                    }
                ],
                "swd": [
                    {
                        "target": "U1",
                        "swdio": "PA13",
                        "swclk": "PA14",
                        "reset": "NRST",
                        "rail": "+3V3",
                        "ground": "GND",
                        "header": {"ref": "J1"},
                    }
                ],
            },
        }

        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)
        endpoints = {(ep.net, ep.ref, ep.pin) for ep in canonical.endpoints}

        assert ("SENSOR_I2C_SCL", "U1", "PB6") in endpoints
        assert ("SENSOR_I2C_SCL", "U2", "SCL") in endpoints
        assert ("SENSOR_I2C_SDA", "U1", "PB7") in endpoints
        assert ("DEBUG_UART_TX", "U1", "PA9") in endpoints
        assert ("DEBUG_UART_TX", "J2", "2") in endpoints
        assert ("DEBUG_UART_RX", "U1", "PA10") in endpoints
        assert ("SWDIO", "U1", "PA13") in endpoints
        assert ("SWDIO", "J1", "2") in endpoints
        assert ("RESET_N", "U1", "NRST") in endpoints
        assert ("RESET_N", "J1", "5") in endpoints
        assert canonical.part_by_ref("J1").role == "swd_header"
        assert len([part for part in canonical.parts if part.role == "i2c_pullup"]) == 2

    def test_swd_interface_can_use_existing_header_ref(self):
        intent = {
            "parts": [
                {
                    "ref": "U1",
                    "value": "MCU",
                    "pins": [
                        {"number": "1", "name": "PA13", "pintype": "bidirectional"},
                        {"number": "2", "name": "PA14", "pintype": "bidirectional"},
                        {"number": "3", "name": "NRST", "pintype": "input"},
                    ],
                },
                {
                    "ref": "J2",
                    "lib_id": "Connector_Generic:Conn_01x05",
                    "value": "DEBUG",
                },
            ],
            "interfaces": [
                {
                    "type": "swd",
                    "target": "U1",
                    "header_ref": "J2",
                    "swdio": "PA13",
                    "swclk": "PA14",
                    "reset": "NRST",
                    "rail": "+3V3",
                    "ground": "GND",
                }
            ],
        }

        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)
        endpoints = {(ep.net, ep.ref, ep.pin) for ep in canonical.endpoints}

        assert [part.ref for part in canonical.parts if part.role == "swd_header"] == []
        assert ("SWDIO", "J2", "2") in endpoints
        assert ("SWCLK", "J2", "4") in endpoints
        assert ("RESET_N", "J2", "5") in endpoints

    def test_grouped_interface_low_level_connections_still_work(self):
        intent = {
            "parts": [{"ref": "U1", "lib_id": "Device:R", "value": "10k"}],
            "interfaces": {
                "gpio": [
                    {
                        "net": "GPIO1",
                        "pins": [["U1", "1"]],
                    }
                ]
            },
        }

        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)

        assert {(ep.ref, ep.pin, ep.net) for ep in canonical.endpoints} == {
            ("U1", "1", "GPIO1")
        }

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

    def test_no_connect_exclude_action_skips_matching_markers_regardless_order(self):
        intent = {
            "parts": [
                {
                    "ref": "U1",
                    "value": "CUSTOM_MCU",
                    "pins": [
                        {"number": "1", "name": "PB6", "pintype": "bidirectional"},
                        {"number": "2", "name": "PA5", "pintype": "bidirectional"},
                    ],
                }
            ],
            "no_connect_rules": [
                {"ref": "U1", "match": {"name_regex": "^P[A-B][0-9]+$"}},
                {"ref": "U1", "match": {"name": "PB6"}, "action": "exclude"},
            ],
        }

        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)

        assert canonical.no_connects == [("U1", "PA5")]
        assert canonical.no_connect_summary["excluded_count"] == 1
        assert canonical.no_connect_summary["skipped_excluded_count"] == 1

    def test_no_connect_skip_hidden_false_includes_hidden_matches(self, monkeypatch):
        def fake_part_pins(_part):
            return [
                {"number": "1", "name": "RESV", "pintype": "no_connect", "hidden": True},
                {"number": "2", "name": "GPIO", "pintype": "bidirectional", "hidden": False},
            ]

        monkeypatch.setattr(normalize, "_part_pins", fake_part_pins)
        intent = {
            "parts": [{"ref": "U1", "lib_id": "Test:Hidden", "value": "Hidden"}],
            "no_connect_rules": [
                {
                    "ref": "U1",
                    "match": {"name_regex": "RESV|GPIO"},
                    "skip_hidden": False,
                }
            ],
        }

        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)

        assert canonical.no_connects == [("U1", "RESV"), ("U1", "GPIO")]
        assert canonical.no_connect_summary["skipped_hidden_count"] == 0

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

        with pytest.raises(ValueError, match="currently on net") as excinfo:
            normalize_design_intent("/tmp/test.kicad_pro", intent)
        assert "attempted assignment to GND" in str(excinfo.value)

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

    def test_capacitor_support_circuit_helpers(self):
        intent = {
            "parts": [
                {
                    "ref": "U1",
                    "value": "MCU",
                    "pins": [
                        {"number": "1", "name": "VCAP1", "pintype": "power_in"},
                        {"number": "2", "name": "OSC_IN", "pintype": "input"},
                        {"number": "3", "name": "OSC_OUT", "pintype": "output"},
                    ],
                },
                {
                    "ref": "U4",
                    "value": "MAG",
                    "pins": [
                        {"number": "1", "name": "SETP", "pintype": "passive"},
                        {"number": "2", "name": "SETC", "pintype": "passive"},
                    ],
                },
            ],
            "support_circuits": [
                {
                    "type": "capacitor_to_gnd",
                    "target": "U1",
                    "pin": "VCAP1",
                    "value": "4.7uF",
                    "ground": "GND",
                },
                {
                    "type": "capacitor_between",
                    "pins": [["U4", "SETP"], ["U4", "SETC"]],
                    "value": "0.22uF",
                },
                {
                    "type": "crystal_load_caps",
                    "target": "U1",
                    "xin": "OSC_IN",
                    "xout": "OSC_OUT",
                    "value": "22pF",
                    "ground": "GND",
                },
            ],
        }

        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)
        endpoints = {(ep.net, ep.ref, ep.pin) for ep in canonical.endpoints}

        assert ("VCAP1", "U1", "VCAP1") in endpoints
        assert ("VCAP1", "C1", "1") in endpoints
        assert ("GND", "C1", "2") in endpoints
        assert ("U4_SETP", "U4", "SETP") in endpoints
        assert ("U4_SETC", "U4", "SETC") in endpoints
        assert ("XTAL_U1_IN", "U1", "OSC_IN") in endpoints
        assert ("XTAL_U1_OUT", "U1", "OSC_OUT") in endpoints
        assert len([part for part in canonical.parts if part.role == "load_capacitor"]) == 2

    def test_crystal_load_caps_reuse_crystal_generated_nets_for_target_pins(self):
        intent = {
            "parts": [
                {
                    "ref": "U1",
                    "value": "MCU",
                    "pins": [
                        {"number": "1", "name": "PH0", "pintype": "input"},
                        {"number": "2", "name": "PH1", "pintype": "output"},
                    ],
                }
            ],
            "support_circuits": [
                {"type": "crystal", "target": "U1", "pins": ["PH0", "PH1"]},
                {"type": "crystal_load_caps", "target": "U1", "xin": "PH0", "xout": "PH1"},
            ],
        }

        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)
        target_nets = {(ep.ref, ep.pin, ep.net) for ep in canonical.endpoints if ep.ref == "U1"}

        assert ("U1", "PH0", "XTAL_U1_IN") in target_nets
        assert ("U1", "PH1", "XTAL_U1_OUT") in target_nets
        assert ("U1", "PH0", "PH0") not in target_nets
        assert ("U1", "PH1", "PH1") not in target_nets

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

    def test_crystal_gnd24_symbol_maps_signals_to_non_ground_pins(self, monkeypatch):
        def fake_part_pins(part):
            if part.ref == "Y?":
                return [
                    {"number": "1", "name": "XTAL1", "pintype": "passive"},
                    {"number": "2", "name": "GND", "pintype": "passive"},
                    {"number": "3", "name": "XTAL2", "pintype": "passive"},
                    {"number": "4", "name": "GND", "pintype": "passive"},
                ]
            return [
                {"number": "1", "name": "PH0", "pintype": "input"},
                {"number": "2", "name": "PH1", "pintype": "output"},
            ]

        monkeypatch.setattr(normalize, "_part_pins", fake_part_pins)
        intent = {
            "parts": [{"ref": "U1", "lib_id": "Test:MCU", "value": "MCU"}],
            "support_circuits": [
                {
                    "type": "crystal",
                    "target": "U1",
                    "lib_id": "Device:Crystal_GND24_Small",
                    "pins": ["PH0", "PH1"],
                    "ground": "GND",
                }
            ],
        }

        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)
        endpoints = {(ep.ref, ep.pin, ep.net) for ep in canonical.endpoints}

        assert ("Y1", "1", "XTAL_U1_IN") in endpoints
        assert ("Y1", "3", "XTAL_U1_OUT") in endpoints
        assert ("Y1", "2", "GND") in endpoints
        assert ("Y1", "4", "GND") in endpoints
        assert ("Y1", "2", "XTAL_U1_OUT") not in endpoints

    def test_crystal_gnd2_numeric_symbol_maps_pin2_to_ground(self, monkeypatch):
        def fake_part_pins(part):
            if part.ref == "Y?":
                return [
                    {"number": "1", "name": "1", "pintype": "passive"},
                    {"number": "2", "name": "2", "pintype": "passive"},
                    {"number": "3", "name": "3", "pintype": "passive"},
                ]
            return [
                {"number": "1", "name": "PH0", "pintype": "input"},
                {"number": "2", "name": "PH1", "pintype": "output"},
            ]

        monkeypatch.setattr(normalize, "_part_pins", fake_part_pins)
        intent = {
            "parts": [{"ref": "U1", "lib_id": "Test:MCU", "value": "MCU"}],
            "support_circuits": [
                {
                    "type": "crystal",
                    "target": "U1",
                    "lib_id": "Device:Crystal_GND2_Small",
                    "pins": ["PH0", "PH1"],
                    "ground": "GND",
                }
            ],
        }

        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)
        endpoints = {(ep.ref, ep.pin, ep.net) for ep in canonical.endpoints}

        assert ("Y1", "1", "XTAL_U1_IN") in endpoints
        assert ("Y1", "3", "XTAL_U1_OUT") in endpoints
        assert ("Y1", "2", "GND") in endpoints
        assert ("Y1", "4", "GND") not in endpoints

    def test_crystal_pin_map_overrides_symbol_defaults(self):
        intent = {
            "parts": [
                {
                    "ref": "U1",
                    "value": "MCU",
                    "pins": [
                        {"number": "1", "name": "PH0", "pintype": "input"},
                        {"number": "2", "name": "PH1", "pintype": "output"},
                    ],
                }
            ],
            "support_circuits": [
                {
                    "type": "crystal",
                    "target": "U1",
                    "lib_id": "Device:Crystal_GND24_Small",
                    "pins": ["PH0", "PH1"],
                    "pin_map": {"xin": "1", "xout": "3", "ground": ["2", "4"]},
                }
            ],
        }

        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)
        endpoints = {(ep.ref, ep.pin, ep.net) for ep in canonical.endpoints}

        assert ("Y1", "3", "XTAL_U1_OUT") in endpoints
        assert ("Y1", "4", "GND") in endpoints

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

    def test_reset_button_pullup_generates_resistor(self):
        intent = {
            "parts": [
                {
                    "ref": "U1",
                    "value": "MCU",
                    "pins": [{"number": "1", "name": "NRST", "pintype": "input"}],
                }
            ],
            "support_circuits": [
                {
                    "type": "reset_button",
                    "target": "U1",
                    "pin": "NRST",
                    "net": "RESET_N",
                    "pullup": "10k",
                    "rail": "+3V3",
                    "ground": "GND",
                }
            ],
        }

        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)
        pullups = [part for part in canonical.parts if part.role == "pullup"]
        endpoints = {(ep.ref, ep.pin, ep.net) for ep in canonical.endpoints}

        assert len(pullups) == 1
        assert (pullups[0].ref, "1", "+3V3") in endpoints
        assert (pullups[0].ref, "2", "RESET_N") in endpoints

    def test_ferrite_filter_respects_in_net_out_net_aliases(self):
        intent = {
            "support_circuits": [
                {
                    "type": "ferrite_filter",
                    "in_net": "+3V3",
                    "out_net": "+3V3A",
                    "value": "Ferrite",
                }
            ],
        }

        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)
        endpoints = {(ep.ref, ep.pin, ep.net) for ep in canonical.endpoints}

        assert ("FB1", "1", "+3V3") in endpoints
        assert ("FB1", "2", "+3V3A") in endpoints
        assert ("FB1", "1", "+5V") not in endpoints

    def test_power_flag_skipped_when_net_has_power_output_driver(self):
        intent = {
            "parts": [
                {
                    "ref": "U5",
                    "value": "LDO",
                    "pins": [
                        {"number": "1", "name": "OUT", "pintype": "power_out"},
                        {"number": "2", "name": "GND", "pintype": "power_in"},
                    ],
                }
            ],
            "bulk_connections": [{"net": "+3V3", "pins": [["U5", "OUT"]]}],
            "support_circuits": [{"type": "power_flag", "net": "+3V3"}],
        }

        canonical = normalize_design_intent("/tmp/test.kicad_pro", intent)

        assert [part for part in canonical.parts if part.role == "power_flag"] == []
        assert not any(ep.ref.startswith("#FLG") for ep in canonical.endpoints)

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
