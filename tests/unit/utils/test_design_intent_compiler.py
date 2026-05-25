from pathlib import Path

import kicad_mcp.utils.design_intent_compiler as compiler
from kicad_mcp.utils.design_intent_compiler import (
    ReferenceAllocator,
    compile_design_intent,
    select_pins,
)


def _base_parts() -> list[dict]:
    return [
        {
            "ref": "U1",
            "value": "MCU",
            "footprint": "Package_QFP:LQFP-48_7x7mm_P0.5mm",
            "pins": [
                {"number": "1", "name": "VDD", "type": "power_in"},
                {"number": "2", "name": "VDDA", "type": "power_in"},
                {"number": "3", "name": "VSS", "type": "power_in"},
                {"number": "4", "name": "VSSA", "type": "power_in"},
                {"number": "5", "name": "PB6", "type": "bidirectional"},
                {"number": "6", "name": "PB7", "type": "bidirectional"},
                {"number": "7", "name": "PA5", "type": "bidirectional"},
                {"number": "8", "name": "PA6", "type": "bidirectional"},
                {"number": "9", "name": "PA7", "type": "bidirectional"},
                {"number": "10", "name": "PA13", "type": "bidirectional"},
                {"number": "11", "name": "PA14", "type": "bidirectional"},
                {"number": "12", "name": "NRST", "type": "input"},
                {"number": "13", "name": "BOOT0", "type": "input"},
            ],
        },
        {
            "ref": "U2",
            "value": "SENSOR",
            "footprint": "Package_LGA:LGA-8_2.0x2.5mm_P0.65mm",
            "pins": [
                {"number": "1", "name": "SCL", "type": "bidirectional"},
                {"number": "2", "name": "SDA", "type": "bidirectional"},
                {"number": "3", "name": "GND", "type": "power_in"},
                {"number": "4", "name": "VDD", "type": "power_in"},
                {"number": "5", "name": "INT", "type": "output"},
                {"number": "6", "name": "AD0", "type": "input"},
            ],
        },
        {
            "ref": "U3",
            "value": "FLASH",
            "footprint": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
            "pins": [
                {"number": "1", "name": "~{CS}", "type": "input"},
                {"number": "2", "name": "SO", "type": "output"},
                {"number": "5", "name": "SI", "type": "input"},
                {"number": "6", "name": "SCK", "type": "input"},
                {"number": "4", "name": "GND", "type": "power_in"},
                {"number": "8", "name": "VCC", "type": "power_in"},
            ],
        },
    ]


def test_select_pins_supports_exact_regex_type_contains_and_exclude():
    pins = _base_parts()[0]["pins"]

    assert [pin["name"] for pin in select_pins(pins, {"pin": "VDD"})] == ["VDD"]
    assert {pin["name"] for pin in select_pins(pins, {"pins": ["VDD", "VDDA"]})} == {
        "VDD",
        "VDDA",
    }
    assert {pin["name"] for pin in select_pins(pins, {"name_regex": "^(VDD|VSS)$"})} == {
        "VDD",
        "VSS",
    }
    assert [pin["name"] for pin in select_pins(pins, {"number_regex": "^1$"})] == ["VDD"]
    assert "VSS" not in {
        pin["name"]
        for pin in select_pins(
            pins,
            {"pin_type": "power_in", "exclude": {"name_regex": "VSS|VSSA"}},
        )
    }
    assert {
        pin["name"]
        for pin in select_pins(
            pins,
            {
                "name_regex": "PA[0-9]+|PB[0-9]+",
                "exclude": {"names": ["PA13", "PA14"]},
            },
        )
    } == {"PB6", "PB7", "PA5", "PA6", "PA7"}
    assert {pin["name"] for pin in select_pins(pins, {"name_contains": "PA"})} == {
        "PA5",
        "PA6",
        "PA7",
        "PA13",
        "PA14",
    }


def test_reference_allocator_skips_existing_refs():
    allocator = ReferenceAllocator(["R1", "R2", "C1"])

    assert allocator.next("R") == "R3"
    assert allocator.next("C") == "C2"
    assert allocator.next("J") == "J1"


def test_pin_rules_connect_all_power_pins_without_manual_listing(tmp_path: Path):
    result = compile_design_intent(
        str(tmp_path),
        {
            "parts": [_base_parts()[0]],
            "pin_rules": [
                {"ref": "U1", "match": {"name_regex": "VDD|VDDA"}, "net": "+3V3"},
                {"ref": "U1", "match": {"name_regex": "VSS|VSSA"}, "net": "GND"},
            ],
        },
    )

    assert result["success"] is True
    assert result["expanded_spec"]["nets"]["+3V3"] == [["U1", "VDD"], ["U1", "VDDA"]]
    assert result["expanded_spec"]["nets"]["GND"] == [["U1", "VSS"], ["U1", "VSSA"]]
    assert Path(result["expanded_spec_path"]).exists()


def test_i2c_interface_expands_devices_interrupt_address_and_pullups(tmp_path: Path):
    result = compile_design_intent(
        str(tmp_path),
        {
            "parts": _base_parts()[:2],
            "interfaces": [
                {
                    "type": "i2c",
                    "name": "SENSOR_I2C",
                    "controller": {"ref": "U1", "scl": "PB6", "sda": "PB7"},
                    "devices": [
                        {
                            "ref": "U2",
                            "scl": "SCL",
                            "sda": "SDA",
                            "interrupts": {"INT": "IMU_INT"},
                            "address_pins": {"AD0": "GND"},
                        }
                    ],
                    "pullups": {"rail": "+3V3_SENSOR", "value": "4.7k"},
                }
            ],
        },
    )

    nets = result["expanded_spec"]["nets"]
    assert result["success"] is True
    assert ["U1", "PB6"] in nets["SENSOR_I2C_SCL"]
    assert ["U2", "SCL"] in nets["SENSOR_I2C_SCL"]
    assert ["U2", "INT"] in nets["IMU_INT"]
    assert ["U2", "AD0"] in nets["GND"]
    assert result["generated_refs"]["i2c_pullups"] == ["R1", "R2"]
    assert any(part["ref"] == "R1" and part["value"] == "4.7k" for part in result["expanded_spec"]["parts"])


def test_grouped_interfaces_inject_type_and_expand(tmp_path: Path):
    result = compile_design_intent(
        str(tmp_path),
        {
            "parts": _base_parts()[:2],
            "interfaces": {
                "i2c": [
                    {
                        "name": "SENSOR_I2C",
                        "controller": {"ref": "U1", "scl": "PB6", "sda": "PB7"},
                        "devices": [{"ref": "U2", "scl": "SCL", "sda": "SDA"}],
                    }
                ]
            },
        },
    )

    assert result["success"] is True
    assert ["U1", "PB6"] in result["expanded_spec"]["nets"]["SENSOR_I2C_SCL"]
    assert ["U2", "SDA"] in result["expanded_spec"]["nets"]["SENSOR_I2C_SDA"]


def test_spi_interface_expands_controller_and_chip_select(tmp_path: Path):
    result = compile_design_intent(
        str(tmp_path),
        {
            "parts": _base_parts(),
            "interfaces": [
                {
                    "type": "spi",
                    "name": "FLASH_SPI",
                    "controller": {"ref": "U1", "sck": "PA5", "miso": "PA6", "mosi": "PA7"},
                    "devices": [
                        {"ref": "U3", "sck": "SCK", "miso": "SO", "mosi": "SI", "cs": "FLASH_CS", "cs_pin": "~{CS}"}
                    ],
                }
            ],
        },
    )

    nets = result["expanded_spec"]["nets"]
    assert result["success"] is True
    assert ["U1", "PA5"] in nets["FLASH_SPI_SCK"]
    assert ["U3", "SCK"] in nets["FLASH_SPI_SCK"]
    assert nets["FLASH_CS"] == [["U3", "~{CS}"]]


def test_swd_interface_generates_header_connections(tmp_path: Path):
    result = compile_design_intent(
        str(tmp_path),
        {
            "parts": [_base_parts()[0]],
            "interfaces": [
                {
                    "type": "swd",
                    "target": "U1",
                    "swdio": "PA13",
                    "swclk": "PA14",
                    "reset": "NRST",
                    "rail": "+3V3",
                    "ground": "GND",
                }
            ],
        },
    )

    assert result["success"] is True
    assert result["generated_refs"]["swd_header"] == ["J1"]
    assert ["U1", "PA13"] in result["expanded_spec"]["nets"]["SWDIO"]
    assert ["J1", "2"] in result["expanded_spec"]["nets"]["SWDIO"]


def test_interface_endpoint_typo_fails_during_compile(tmp_path: Path):
    result = compile_design_intent(
        str(tmp_path),
        {
            "parts": _base_parts()[:2],
            "interfaces": [
                {
                    "type": "i2c",
                    "name": "SENSOR_I2C",
                    "controller": {"ref": "U1", "scl": "P86", "sda": "PB7"},
                    "devices": [{"ref": "U2", "scl": "SCL", "sda": "SDA"}],
                }
            ],
        },
    )

    assert result["success"] is False
    assert any(error["error"] == "unknown pin" and error["pin"] == "P86" for error in result["errors"])


def test_bulk_connection_pin_typo_fails_during_compile(tmp_path: Path):
    result = compile_design_intent(
        str(tmp_path),
        {
            "parts": [_base_parts()[0]],
            "bulk_connections": [{"net": "RESET_N", "pins": [["U1", "NRST_BAD"]]}],
        },
    )

    assert result["success"] is False
    assert result["errors"][0]["error"] == "unknown pin"


def test_no_connect_rules_support_match_exclude_and_top_level_except(tmp_path: Path):
    base = {"parts": [_base_parts()[0]]}

    match_exclude = compile_design_intent(
        str(tmp_path / "a"),
        {
            **base,
            "no_connect_rules": [
                {
                    "ref": "U1",
                    "match": {
                        "name_regex": "PA[0-9]+|PB[0-9]+",
                        "exclude": {"names": ["PA13", "PA14"]},
                    },
                }
            ],
        },
    )
    top_level_except = compile_design_intent(
        str(tmp_path / "b"),
        {
            **base,
            "no_connect_rules": [
                {
                    "ref": "U1",
                    "match": {"name_regex": "PA[0-9]+|PB[0-9]+"},
                    "except": ["PA13", "PA14"],
                }
            ],
        },
    )

    expected = [
        {"ref": "U1", "pin": "PB6"},
        {"ref": "U1", "pin": "PB7"},
        {"ref": "U1", "pin": "PA5"},
        {"ref": "U1", "pin": "PA6"},
        {"ref": "U1", "pin": "PA7"},
    ]
    assert match_exclude["success"] is True
    assert top_level_except["success"] is True
    assert match_exclude["expanded_spec"]["no_connects"] == expected
    assert top_level_except["expanded_spec"]["no_connects"] == expected


def test_decoupling_support_circuit_generates_capacitors_and_nets(tmp_path: Path):
    result = compile_design_intent(
        str(tmp_path),
        {
            "parts": [_base_parts()[0]],
            "support_circuits": [
                {
                    "type": "decoupling",
                    "target": "U1",
                    "rail": "+3V3",
                    "ground": "GND",
                    "capacitors": ["100n", "100n", "4.7u"],
                }
            ],
        },
    )

    assert result["success"] is True
    assert result["generated_refs"]["decoupling"] == ["C1", "C2", "C3"]
    assert ["C1", "1"] in result["expanded_spec"]["nets"]["+3V3"]
    assert ["C1", "2"] in result["expanded_spec"]["nets"]["GND"]


def test_grouped_support_circuits_inject_type_and_expand(tmp_path: Path):
    result = compile_design_intent(
        str(tmp_path),
        {
            "parts": [_base_parts()[0]],
            "support_circuits": {
                "decoupling": [
                    {
                        "target": "U1",
                        "rail": "+3V3",
                        "ground": "GND",
                        "capacitors": ["100n"],
                    }
                ]
            },
        },
    )

    assert result["success"] is True
    assert result["generated_refs"]["decoupling"] == ["C1"]
    assert ["C1", "1"] in result["expanded_spec"]["nets"]["+3V3"]


def test_grouped_design_intent_rejects_invalid_group_values(tmp_path: Path):
    result = compile_design_intent(
        str(tmp_path),
        {
            "parts": [_base_parts()[0]],
            "interfaces": {"i2c": "bad"},
            "support_circuits": {"decoupling": ["bad"]},
        },
    )

    assert result["success"] is False
    assert any(error["path"] == "interfaces.i2c" for error in result["errors"])
    assert any(error["path"] == "support_circuits.decoupling[0]" for error in result["errors"])


def test_crystal_preserves_requested_default_symbol(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        compiler,
        "_resolve_symbol_pins",
        lambda _lib_id: [{"number": "1", "name": "1"}, {"number": "2", "name": "2"}],
    )

    result = compile_design_intent(
        str(tmp_path),
        {"support_circuits": [{"type": "crystal", "pins": ["OSC_IN", "OSC_OUT"]}]},
    )

    assert result["success"] is True
    crystal = next(part for part in result["expanded_spec"]["parts"] if part["ref"] == "Y1")
    assert crystal["lib_id"] == "Device:Crystal"
    assert result["expanded_spec"]["nets"]["OSC_IN"] == [["Y1", "1"]]
    assert result["expanded_spec"]["nets"]["OSC_OUT"] == [["Y1", "2"]]


def test_crystal_gnd2_connects_ground_pins(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        compiler,
        "_resolve_symbol_pins",
        lambda _lib_id: [
            {"number": "1", "name": "1"},
            {"number": "2", "name": "2"},
            {"number": "3", "name": "3"},
            {"number": "4", "name": "4"},
        ],
    )

    result = compile_design_intent(
        str(tmp_path),
        {
            "support_circuits": {
                "crystal": [
                    {
                        "lib_id": "Device:Crystal_GND2",
                        "pins": ["OSC_IN", "OSC_OUT"],
                        "ground": "GNDA",
                    }
                ]
            }
        },
    )

    assert result["success"] is True
    crystal = next(part for part in result["expanded_spec"]["parts"] if part["ref"] == "Y1")
    assert crystal["lib_id"] == "Device:Crystal_GND2"
    assert ["Y1", "3"] in result["expanded_spec"]["nets"]["GNDA"]
    assert ["Y1", "4"] in result["expanded_spec"]["nets"]["GNDA"]


def test_grounded_crystal_errors_when_symbol_has_no_ground_pins(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        compiler,
        "_resolve_symbol_pins",
        lambda _lib_id: [{"number": "1", "name": "1"}, {"number": "2", "name": "2"}],
    )

    result = compile_design_intent(
        str(tmp_path),
        {
            "support_circuits": [
                {
                    "type": "crystal",
                    "lib_id": "Device:Crystal",
                    "pins": ["OSC_IN", "OSC_OUT"],
                    "ground": "GND",
                }
            ]
        },
    )

    assert result["success"] is False
    assert result["errors"][0]["error"] == "grounded crystal requested but symbol has no ground pins"


def test_led_indicator_uses_led_anode_toward_rail_and_cathode_to_ground(tmp_path: Path):
    result = compile_design_intent(
        str(tmp_path),
        {
            "support_circuits": [
                {
                    "type": "led_indicator",
                    "name": "POWER_LED",
                    "rail": "+3V3",
                    "ground": "GND",
                    "resistor": "1k",
                }
            ],
        },
    )

    assert result["success"] is True
    assert ["D1", "2"] in result["expanded_spec"]["nets"]["POWER_LED_K"]
    assert ["D1", "1"] in result["expanded_spec"]["nets"]["GND"]


def test_ferrite_filter_uses_kicad_ferrite_bead_symbol(tmp_path: Path):
    result = compile_design_intent(
        str(tmp_path),
        {
            "support_circuits": [
                {
                    "type": "ferrite_filter",
                    "in_net": "+3V3",
                    "out_net": "+3V3_FILTERED",
                    "footprint": "Inductor_SMD:L_0603_1608Metric",
                }
            ],
        },
    )

    assert result["success"] is True
    ferrite = next(part for part in result["expanded_spec"]["parts"] if part["ref"] == "FB1")
    assert ferrite["lib_id"] == "Device:FerriteBead"


def test_no_connect_rules_mark_unused_gpio_pins(tmp_path: Path):
    result = compile_design_intent(
        str(tmp_path),
        {
            "parts": [_base_parts()[0]],
            "interfaces": [
                {
                    "type": "i2c",
                    "name": "SENSOR_I2C",
                    "controller": {"ref": "U1", "scl": "PB6", "sda": "PB7"},
                    "devices": [],
                }
            ],
            "no_connect_rules": [
                {
                    "ref": "U1",
                    "match": {"name_regex": "PA[0-9]+|PB[0-9]+"},
                    "except": ["PA13", "PA14"],
                    "action": "mark_no_connect",
                }
            ],
        },
    )

    assert result["success"] is True
    assert {"ref": "U1", "pin": "PA5"} in result["expanded_spec"]["no_connects"]
    assert {"ref": "U1", "pin": "PB6"} not in result["expanded_spec"]["no_connects"]
    assert {"ref": "U1", "pin": "PA13"} not in result["expanded_spec"]["no_connects"]


def test_no_connect_rules_skip_hidden_nc_pins(tmp_path: Path):
    result = compile_design_intent(
        str(tmp_path),
        {
            "parts": [
                {
                    "ref": "U2",
                    "value": "SENSOR",
                    "footprint": "Package_LGA:LGA-6_2.0x2.5mm_P0.65mm",
                    "pins": [
                        {"number": "1", "name": "NC", "type": "no_connect", "hidden": True},
                        {"number": "2", "name": "NC", "type": "no_connect", "hidden": True},
                        {"number": "3", "name": "SCL", "type": "bidirectional"},
                        {"number": "4", "name": "SDA", "type": "bidirectional"},
                    ],
                }
            ],
            "no_connect_rules": [
                {"ref": "U2", "match": {"name_regex": "NC|SCL|SDA"}, "action": "mark_no_connect"}
            ],
        },
    )

    assert result["success"] is True
    assert result["summary"]["skipped_hidden_pin_count"] == 2
    assert {item["pin"] for item in result["skipped_hidden_pins"]} == {"1", "2"}
    assert result["expanded_spec"]["no_connects"] == [
        {"ref": "U2", "pin": "SCL"},
        {"ref": "U2", "pin": "SDA"},
    ]


def test_pin_rule_duplicate_vdd_expands_to_all_vdd_pins(tmp_path: Path):
    result = compile_design_intent(
        str(tmp_path),
        {
            "parts": [
                {
                    "ref": "U1",
                    "value": "DUAL_POWER",
                    "footprint": "Package_DIP:DIP-4_W7.62mm",
                    "pins": [
                        {"number": "1", "name": "VDD", "type": "power_in"},
                        {"number": "2", "name": "VDD", "type": "power_in"},
                        {"number": "3", "name": "GND", "type": "power_in"},
                    ],
                }
            ],
            "pin_rules": [{"ref": "U1", "match": {"pin": "VDD"}, "net": "+3V3"}],
        },
    )

    assert result["success"] is True
    assert result["expanded_spec"]["nets"]["+3V3"] == [["U1", "1"], ["U1", "2"]]


def test_explicit_duplicate_pin_name_returns_helpful_suggestion(tmp_path: Path):
    result = compile_design_intent(
        str(tmp_path),
        {
            "parts": [
                {
                    "ref": "U1",
                    "value": "DUAL_POWER",
                    "footprint": "Package_DIP:DIP-4_W7.62mm",
                    "pins": [
                        {"number": "1", "name": "VDD", "type": "power_in"},
                        {"number": "2", "name": "VDD", "type": "power_in"},
                    ],
                }
            ],
            "bulk_connections": [{"net": "+3V3", "pins": [["U1", "VDD"]]}],
        },
    )

    assert result["success"] is False
    error = result["errors"][0]
    assert error["error"] == "pin identifier is ambiguous"
    assert error["suggestion"] == "Use pin_rules to connect all matching pins, or use a pin number."
    assert error["example"]["pin_rules"][0]["match"] == {"pin": "VDD"}


def test_conflict_detection_catches_same_pin_assigned_to_two_nets(tmp_path: Path):
    result = compile_design_intent(
        str(tmp_path),
        {
            "parts": [_base_parts()[0]],
            "bulk_connections": [
                {"net": "RESET_N", "pins": [["U1", "NRST"]]},
                {"net": "OTHER_RESET", "pins": [["U1", "NRST"]]},
            ],
        },
    )

    assert result["success"] is False
    assert result["errors"][0]["error"] == "same ref/pin assigned to two different nets"
