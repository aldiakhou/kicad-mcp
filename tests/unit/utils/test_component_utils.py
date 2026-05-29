from kicad_mcp.utils.component_utils import (
    extract_resistance_value,
    extract_voltage_from_regulator,
    normalize_component_value,
)


def test_negative_79xx_regulators_parse_as_negative_voltage():
    assert extract_voltage_from_regulator("LM7905") == "-5V"
    assert extract_voltage_from_regulator("MC7912") == "-12V"


def test_resistor_normalization_uses_canonical_kilo_ohm_suffix():
    assert extract_resistance_value("10k") == (10.0, "k")
    assert normalize_component_value("10k", "R") == "10kΩ"
    assert normalize_component_value("4K7", "R") == "4.7kΩ"
