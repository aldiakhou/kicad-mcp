"""Library mapping helpers for resolving symbol/footprint identifiers.

Maps between normalized names and KiCad library identifiers.
"""

from __future__ import annotations

import re
from typing import Any

# Common library ID aliases for quick lookup
_COMMON_SYMBOLS: dict[str, str] = {
    "R": "Device:R",
    "C": "Device:C",
    "L": "Device:L",
    "D": "Device:D",
    "LED": "Device:LED",
    "Crystal": "Device:Crystal",
    "Crystal_GND2": "Device:Crystal_GND24",
    "Crystal_GND24": "Device:Crystal_GND24",
    "FerriteBead": "Device:FerriteBead",
    "SW_Push": "Switch:SW_Push",
    "USB_C_Receptacle": "Connector:USB_C_Receptacle_USB2.0",
}

# Pattern for extracting library and symbol name from a lib_id
_LIB_ID_PATTERN = re.compile(r"^([^:]+):(.+)$")


def resolve_lib_id(lib_id: str) -> tuple[str, str]:
    """Split a library ID into (library, symbol_name).

    Args:
        lib_id: Either "Library:Symbol" or a shorthand like "R".

    Returns:
        Tuple of (library_name, symbol_name).
    """
    # Check aliases first
    if lib_id in _COMMON_SYMBOLS:
        lib_id = _COMMON_SYMBOLS[lib_id]

    match = _LIB_ID_PATTERN.match(lib_id)
    if match:
        return match.group(1), match.group(2)

    # If no colon, assume Device library for common passives
    if lib_id in ("R", "C", "L", "D", "LED"):
        return "Device", lib_id

    return "", lib_id


def footprint_for_passive(ref_prefix: str, value: str = "") -> str | None:
    """Suggest a default footprint for a passive component.

    Args:
        ref_prefix: Reference designator prefix (e.g., "R", "C").
        value: Component value (may influence package size).

    Returns:
        Footprint string or None if not determinable.
    """
    defaults: dict[str, str] = {
        "R": "Resistor_SMD:R_0402_1005Metric",
        "C": "Capacitor_SMD:C_0402_1005Metric",
        "L": "Inductor_SMD:L_0402_1005Metric",
        "D": "Diode_SMD:D_SOD-123",
        "FB": "Inductor_SMD:L_0402_1005Metric",
    }
    return defaults.get(ref_prefix)


def normalize_pin_name(pin: str) -> str:
    """Normalize a pin name for comparison.

    Strips whitespace and normalizes case for comparison.
    """
    return pin.strip()


def is_power_symbol(lib_id: str) -> bool:
    """Check if a library ID is a power symbol."""
    lib, name = resolve_lib_id(lib_id)
    return lib.lower() in ("power", "") and name.startswith((
        "+", "GND", "VCC", "VDD", "VSS", "VBUS",
    ))


def extract_intent_metadata(properties: dict[str, Any]) -> dict[str, str]:
    """Extract KICAD_MCP metadata from symbol properties."""
    return {
        k: str(v) for k, v in properties.items()
        if k.startswith("KICAD_MCP_")
    }
