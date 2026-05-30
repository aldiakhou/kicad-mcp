"""Normalize design intent into canonical circuit representation.

Converts the high-level design intent format (parts, rails, interfaces,
support_circuits, etc.) into a CanonicalCircuit with explicit parts and
pin-to-net endpoints.
"""

from __future__ import annotations

import re
from typing import Any

from kicad_mcp.schematic_engine.models import (
    CanonicalCircuit,
    CircuitEndpoint,
    CircuitPart,
)

# Default footprints for common passives when not specified
_PASSIVE_FOOTPRINT_DEFAULTS: dict[str, str] = {
    "R": "Resistor_SMD:R_0402_1005Metric",
    "C": "Capacitor_SMD:C_0402_1005Metric",
    "L": "Inductor_SMD:L_0402_1005Metric",
    "D": "Diode_SMD:D_SOD-123",
    "FB": "Inductor_SMD:L_0402_1005Metric",
}

# Nets that are considered power/ground rails
_POWER_NET_PATTERNS = re.compile(
    r"^(\+[\dV.]+|VCC|VDD|VBUS|AVCC|AVDD|V_\w+|GND|AGND|DGND|VSS|GNDA|GNDD|CHASSIS)$",
    re.IGNORECASE,
)


class GeneratedRefAllocator:
    """Allocate unique generated reference designators."""

    def __init__(self, existing_refs: set[str]):
        self.used = set(existing_refs)

    def claim(self, ref: str) -> str:
        if ref in self.used:
            raise ValueError(f"Duplicate reference designator '{ref}'")
        self.used.add(ref)
        return ref

    def next(self, prefix: str) -> str:
        for index in range(1, 10000):
            ref = f"{prefix}{index}"
            if ref not in self.used:
                self.used.add(ref)
                return ref
        raise RuntimeError(f"No free reference for {prefix}")


def normalize_design_intent(
    project_path: str,
    intent: dict[str, Any],
) -> CanonicalCircuit:
    """Convert a design intent dict into a CanonicalCircuit.

    Args:
        project_path: Path to the KiCad project.
        intent: Design intent dictionary with parts, rails, interfaces, etc.

    Returns:
        CanonicalCircuit with all parts and endpoints resolved.

    Raises:
        ValueError: If the intent is malformed or has unresolvable references.
    """
    parts: list[CircuitPart] = []
    endpoints: list[CircuitEndpoint] = []
    no_connects: list[tuple[str, str]] = []
    blocks: dict[str, list[str]] = {}
    rails: set[str] = set()

    # --- Extract parts ---
    for part_spec in intent.get("parts", []):
        part = _normalize_part(part_spec)
        parts.append(part)
        block_name = part.block
        blocks.setdefault(block_name, []).append(part.ref)

    ref_allocator = GeneratedRefAllocator({part.ref for part in parts})

    # --- Extract rails ---
    for rail_spec in intent.get("rails", []):
        rail_name = rail_spec.get("net") or rail_spec.get("name", "")
        if not rail_name:
            continue
        rails.add(rail_name)
        for connection in rail_spec.get("connections", []):
            ref = connection.get("ref", "")
            pins = connection.get("pins", [])
            if isinstance(pins, str):
                pins = [pins]
            for pin in pins:
                endpoints.append(CircuitEndpoint(
                    ref=ref,
                    pin=str(pin),
                    net=rail_name,
                    required=True,
                    allow_hidden=connection.get("allow_hidden", False),
                    source="rails",
                ))

    # --- Extract interfaces ---
    interfaces_raw = intent.get("interfaces", [])
    if isinstance(interfaces_raw, dict):
        # Dict format: {"i2c": [...connections...], "spi": [...]}
        for iface_name, iface_connections in interfaces_raw.items():
            if isinstance(iface_connections, list):
                iface_spec = {"type": iface_name, "connections": iface_connections}
                iface_endpoints = _normalize_interface(iface_spec)
                endpoints.extend(iface_endpoints)
            elif isinstance(iface_connections, dict):
                # Single interface dict with type implicit from key
                iface_connections["type"] = iface_connections.get("type", iface_name)
                iface_endpoints = _normalize_interface(iface_connections)
                endpoints.extend(iface_endpoints)
    elif isinstance(interfaces_raw, list):
        for iface_spec in interfaces_raw:
            iface_endpoints = _normalize_interface(iface_spec)
            endpoints.extend(iface_endpoints)

    # --- Extract bulk_connections ---
    for bulk in intent.get("bulk_connections", []):
        net_name = bulk.get("net", "")
        if not net_name:
            continue
        for conn in bulk.get("endpoints", []):
            ref = conn.get("ref", "")
            pin = conn.get("pin", "")
            if ref and pin:
                endpoints.append(CircuitEndpoint(
                    ref=ref,
                    pin=str(pin),
                    net=net_name,
                    required=conn.get("required", True),
                    allow_hidden=conn.get("allow_hidden", False),
                    source="bulk_connections",
                ))

    # --- Extract support_circuits ---
    for sc in intent.get("support_circuits", []):
        sc_parts, sc_endpoints = _normalize_support_circuit(sc, ref_allocator)
        parts.extend(sc_parts)
        endpoints.extend(sc_endpoints)
        for p in sc_parts:
            blocks.setdefault(p.block, []).append(p.ref)

    # --- Extract pin_rules ---
    for rule in intent.get("pin_rules", []):
        rule_endpoints = _normalize_pin_rule(rule)
        endpoints.extend(rule_endpoints)

    # --- Extract no_connect_rules ---
    for nc_rule in intent.get("no_connect_rules", []):
        ref = nc_rule.get("ref", "")
        pins = nc_rule.get("pins", [])
        if isinstance(pins, str):
            pins = [pins]
        for pin in pins:
            no_connects.append((ref, str(pin)))

    # Detect power nets from endpoint net names
    for ep in endpoints:
        if _POWER_NET_PATTERNS.match(ep.net):
            rails.add(ep.net)

    # Validate: no duplicate refs
    _validate_no_duplicate_refs(parts)

    return CanonicalCircuit(
        project_path=project_path,
        parts=parts,
        endpoints=endpoints,
        no_connects=no_connects,
        blocks=blocks,
        rails=rails,
    )


def _normalize_part(part_spec: dict[str, Any]) -> CircuitPart:
    """Convert a part spec dict to a CircuitPart."""
    ref = part_spec.get("ref", "")
    if not ref:
        raise ValueError(f"Part missing 'ref': {part_spec}")
    lib_id = part_spec.get("lib_id", "")
    if not lib_id:
        raise ValueError(f"Part '{ref}' missing 'lib_id'")

    value = part_spec.get("value", "")
    footprint = part_spec.get("footprint")
    block = part_spec.get("block", "default")
    role = part_spec.get("role")
    properties = {}

    # Apply default footprint for passives
    if not footprint:
        prefix = re.match(r"^[A-Z]+", ref)
        if prefix and prefix.group() in _PASSIVE_FOOTPRINT_DEFAULTS:
            footprint = _PASSIVE_FOOTPRINT_DEFAULTS[prefix.group()]

    # Carry over extra properties
    for key in ("position", "angle", "mirror"):
        if key in part_spec:
            properties[key] = str(part_spec[key])

    return CircuitPart(
        ref=ref,
        lib_id=lib_id,
        value=value,
        footprint=footprint,
        block=block,
        role=role,
        properties=properties,
    )


def _normalize_interface(iface_spec: dict[str, Any]) -> list[CircuitEndpoint]:
    """Convert an interface spec to endpoints."""
    endpoints: list[CircuitEndpoint] = []
    iface_type = iface_spec.get("type", "")
    connections = iface_spec.get("connections", [])

    for conn in connections:
        net_name = conn.get("net", "")
        if not net_name:
            continue
        for ep in conn.get("endpoints", []):
            ref = ep.get("ref", "")
            pin = ep.get("pin", "")
            if ref and pin:
                endpoints.append(CircuitEndpoint(
                    ref=ref,
                    pin=str(pin),
                    net=net_name,
                    required=ep.get("required", True),
                    allow_hidden=ep.get("allow_hidden", False),
                    source=f"interface:{iface_type}",
                ))

    # Handle shorthand interface definitions
    signals = iface_spec.get("signals", {})
    for signal_name, signal_def in signals.items():
        net_name = signal_def.get("net", signal_name)
        for ep in signal_def.get("endpoints", []):
            ref = ep.get("ref", "")
            pin = ep.get("pin", "")
            if ref and pin:
                endpoints.append(CircuitEndpoint(
                    ref=ref,
                    pin=str(pin),
                    net=net_name,
                    required=ep.get("required", True),
                    allow_hidden=ep.get("allow_hidden", False),
                    source=f"interface:{iface_type}:{signal_name}",
                ))

    return endpoints


def _normalize_support_circuit(
    sc_spec: dict[str, Any],
    ref_allocator: GeneratedRefAllocator,
) -> tuple[list[CircuitPart], list[CircuitEndpoint]]:
    """Convert a support circuit spec to parts and endpoints."""
    parts: list[CircuitPart] = []
    endpoints: list[CircuitEndpoint] = []
    sc_type = sc_spec.get("type", "")
    target = sc_spec.get("target", "")

    if sc_type == "decoupling":
        parts, endpoints = _normalize_decoupling(sc_spec, target, ref_allocator)
    elif sc_type == "crystal":
        parts, endpoints = _normalize_crystal(sc_spec, target, ref_allocator)
    elif sc_type == "usb_c_power_input":
        parts, endpoints = _normalize_usb_c_power(sc_spec, ref_allocator)
    elif sc_type in ("pullup", "pulldown"):
        parts, endpoints = _normalize_pull_resistors(sc_spec, target, sc_type, ref_allocator)
    elif sc_type == "reset_button":
        parts, endpoints = _normalize_reset_button(sc_spec, target, ref_allocator)
    elif sc_type == "led":
        parts, endpoints = _normalize_led(sc_spec, target, ref_allocator)
    elif sc_type == "ferrite":
        parts, endpoints = _normalize_ferrite(sc_spec, target, ref_allocator)

    return parts, endpoints


def _normalize_decoupling(
    spec: dict[str, Any],
    target: str,
    ref_allocator: GeneratedRefAllocator,
) -> tuple[list[CircuitPart], list[CircuitEndpoint]]:
    """Generate decoupling capacitor parts and endpoints."""
    parts: list[CircuitPart] = []
    endpoints: list[CircuitEndpoint] = []
    rail = spec.get("rail", "+3V3")
    ground = spec.get("ground", "GND")
    capacitors = spec.get("capacitors", ["100n"])
    if isinstance(capacitors, str):
        capacitors = [capacitors]

    explicit_ref = spec.get("ref")
    for cap_value in capacitors:
        if explicit_ref and len(capacitors) == 1:
            ref = ref_allocator.claim(str(explicit_ref))
        else:
            ref = ref_allocator.next("C")

        part = CircuitPart(
            ref=ref,
            lib_id="Device:C",
            value=cap_value,
            footprint="Capacitor_SMD:C_0402_1005Metric",
            block=target if target else "power",
            role="decoupling",
            properties={
                "KICAD_MCP_ROLE": "decoupling",
                "KICAD_MCP_TARGET": target,
                "KICAD_MCP_NETS": f"{rail},{ground}",
            },
        )
        parts.append(part)

        endpoints.append(CircuitEndpoint(
            ref=ref, pin="1", net=rail, required=True,
            source=f"support_circuit:decoupling:{target}",
        ))
        endpoints.append(CircuitEndpoint(
            ref=ref, pin="2", net=ground, required=True,
            source=f"support_circuit:decoupling:{target}",
        ))

    return parts, endpoints


def _normalize_crystal(
    spec: dict[str, Any],
    target: str,
    ref_allocator: GeneratedRefAllocator,
) -> tuple[list[CircuitPart], list[CircuitEndpoint]]:
    """Generate crystal and load capacitor parts."""
    parts: list[CircuitPart] = []
    endpoints: list[CircuitEndpoint] = []

    explicit_ref = spec.get("ref")
    crystal_ref = ref_allocator.claim(str(explicit_ref)) if explicit_ref else ref_allocator.next("Y")
    crystal_value = spec.get("value", "8MHz")
    pins = spec.get("pins", ["PF0", "PF1"])
    ground = spec.get("ground", "GND")

    # Try Device:Crystal_GND24 first, fall back to Device:Crystal
    crystal_lib_id = spec.get("lib_id", "Device:Crystal_GND24")
    use_grounded = "GND" in crystal_lib_id

    part = CircuitPart(
        ref=crystal_ref,
        lib_id=crystal_lib_id,
        value=crystal_value,
        footprint=spec.get("footprint"),
        block=target if target else "mcu",
        role="crystal",
        properties={
            "KICAD_MCP_ROLE": "crystal",
            "KICAD_MCP_TARGET": target,
        },
    )
    parts.append(part)

    # Connect crystal pins to target
    if len(pins) >= 2:
        # Crystal In pin
        endpoints.append(CircuitEndpoint(
            ref=crystal_ref, pin="1", net=f"XTAL_{target}_IN",
            required=True, source="support_circuit:crystal",
        ))
        endpoints.append(CircuitEndpoint(
            ref=target, pin=str(pins[0]), net=f"XTAL_{target}_IN",
            required=True, source="support_circuit:crystal",
        ))
        # Crystal Out pin
        endpoints.append(CircuitEndpoint(
            ref=crystal_ref, pin="2", net=f"XTAL_{target}_OUT",
            required=True, source="support_circuit:crystal",
        ))
        endpoints.append(CircuitEndpoint(
            ref=target, pin=str(pins[1]), net=f"XTAL_{target}_OUT",
            required=True, source="support_circuit:crystal",
        ))

    # Ground pins for grounded crystal
    if use_grounded:
        endpoints.append(CircuitEndpoint(
            ref=crystal_ref, pin="3", net=ground,
            required=True, source="support_circuit:crystal",
        ))
        endpoints.append(CircuitEndpoint(
            ref=crystal_ref, pin="4", net=ground,
            required=True, source="support_circuit:crystal",
        ))

    # Load capacitors
    load_caps = spec.get("load_capacitors")
    if load_caps:
        cap_value = load_caps if isinstance(load_caps, str) else "18pF"
        for net_name in [f"XTAL_{target}_IN", f"XTAL_{target}_OUT"]:
            cap_ref = ref_allocator.next("C")
            cap_part = CircuitPart(
                ref=cap_ref,
                lib_id="Device:C",
                value=cap_value,
                footprint="Capacitor_SMD:C_0402_1005Metric",
                block=target if target else "mcu",
                role="load_capacitor",
                properties={
                    "KICAD_MCP_ROLE": "load_capacitor",
                    "KICAD_MCP_TARGET": crystal_ref,
                },
            )
            parts.append(cap_part)
            endpoints.append(CircuitEndpoint(
                ref=cap_ref, pin="1", net=net_name,
                required=True, source="support_circuit:crystal:load_cap",
            ))
            endpoints.append(CircuitEndpoint(
                ref=cap_ref, pin="2", net=ground,
                required=True, source="support_circuit:crystal:load_cap",
            ))

    return parts, endpoints


def _normalize_usb_c_power(
    spec: dict[str, Any],
    ref_allocator: GeneratedRefAllocator,
) -> tuple[list[CircuitPart], list[CircuitEndpoint]]:
    """Generate USB-C power input circuit."""
    parts: list[CircuitPart] = []
    endpoints: list[CircuitEndpoint] = []

    explicit_ref = spec.get("ref")
    connector_ref = ref_allocator.claim(str(explicit_ref)) if explicit_ref else ref_allocator.next("J")
    vbus_net = spec.get("vbus_net", "+5V")
    ground = spec.get("ground", "GND")
    cc_resistor = spec.get("cc_resistor", "5.1k")
    shield = spec.get("shield", "CHASSIS")

    # USB-C connector
    connector = CircuitPart(
        ref=connector_ref,
        lib_id=spec.get("lib_id", "Connector:USB_C_Receptacle_USB2.0"),
        value="USB-C",
        footprint=spec.get("footprint"),
        block="power",
        role="usb_c_power",
        properties={
            "KICAD_MCP_ROLE": "usb_c_power",
            "KICAD_MCP_NETS": f"{vbus_net},{ground}",
        },
    )
    parts.append(connector)

    # VBUS pins (A4, B9 for USB 2.0 receptacle, or A9/B4 depending on variant)
    vbus_pins = spec.get("vbus_pins", ["A4", "B9", "A9", "B4"])
    for pin in vbus_pins:
        endpoints.append(CircuitEndpoint(
            ref=connector_ref, pin=str(pin), net=vbus_net,
            required=True, allow_hidden=True,
            source="support_circuit:usb_c_power:vbus",
        ))

    # GND pins
    gnd_pins = spec.get("gnd_pins", ["A1", "A12", "B1", "B12"])
    for pin in gnd_pins:
        endpoints.append(CircuitEndpoint(
            ref=connector_ref, pin=str(pin), net=ground,
            required=True, allow_hidden=True,
            source="support_circuit:usb_c_power:gnd",
        ))

    # Shield
    shield_pins = spec.get("shield_pins", ["S1"])
    for pin in shield_pins:
        endpoints.append(CircuitEndpoint(
            ref=connector_ref, pin=str(pin), net=shield,
            required=False, allow_hidden=True,
            source="support_circuit:usb_c_power:shield",
        ))

    # CC pulldown resistors
    for i, cc_pin in enumerate(spec.get("cc_pins", ["A5", "B5"])):
        r_ref = ref_allocator.next("R")
        r_part = CircuitPart(
            ref=r_ref,
            lib_id="Device:R",
            value=cc_resistor,
            footprint="Resistor_SMD:R_0402_1005Metric",
            block="power",
            role="cc_pulldown",
            properties={
                "KICAD_MCP_ROLE": "cc_pulldown",
                "KICAD_MCP_TARGET": connector_ref,
            },
        )
        parts.append(r_part)

        cc_net = f"CC{i + 1}_{connector_ref}"
        endpoints.append(CircuitEndpoint(
            ref=connector_ref, pin=str(cc_pin), net=cc_net,
            required=True, source="support_circuit:usb_c_power:cc",
        ))
        endpoints.append(CircuitEndpoint(
            ref=r_ref, pin="1", net=cc_net,
            required=True, source="support_circuit:usb_c_power:cc",
        ))
        endpoints.append(CircuitEndpoint(
            ref=r_ref, pin="2", net=ground,
            required=True, source="support_circuit:usb_c_power:cc",
        ))

    return parts, endpoints


def _normalize_pull_resistors(
    spec: dict[str, Any],
    target: str,
    pull_type: str,
    ref_allocator: GeneratedRefAllocator,
) -> tuple[list[CircuitPart], list[CircuitEndpoint]]:
    """Generate pull-up or pull-down resistors."""
    parts: list[CircuitPart] = []
    endpoints: list[CircuitEndpoint] = []

    rail = spec.get("rail", "+3V3" if pull_type == "pullup" else "GND")
    value = spec.get("value", "4.7k")
    signals = spec.get("signals", [])
    if isinstance(signals, str):
        signals = [signals]

    explicit_ref = spec.get("ref")
    for signal in signals:
        net_name = signal if isinstance(signal, str) else signal.get("net", "")
        if explicit_ref and len(signals) == 1:
            ref = ref_allocator.claim(str(explicit_ref))
        else:
            ref = ref_allocator.next("R")

        part = CircuitPart(
            ref=ref,
            lib_id="Device:R",
            value=value,
            footprint="Resistor_SMD:R_0402_1005Metric",
            block=target if target else "default",
            role=pull_type,
            properties={
                "KICAD_MCP_ROLE": pull_type,
                "KICAD_MCP_TARGET": target,
            },
        )
        parts.append(part)

        # Connect to rail
        rail_pin = "1" if pull_type == "pullup" else "2"
        signal_pin = "2" if pull_type == "pullup" else "1"

        endpoints.append(CircuitEndpoint(
            ref=ref, pin=rail_pin, net=rail,
            required=True, source=f"support_circuit:{pull_type}",
        ))
        endpoints.append(CircuitEndpoint(
            ref=ref, pin=signal_pin, net=net_name,
            required=True, source=f"support_circuit:{pull_type}",
        ))

    return parts, endpoints


def _normalize_reset_button(
    spec: dict[str, Any],
    target: str,
    ref_allocator: GeneratedRefAllocator,
) -> tuple[list[CircuitPart], list[CircuitEndpoint]]:
    """Generate reset button circuit."""
    parts: list[CircuitPart] = []
    endpoints: list[CircuitEndpoint] = []

    explicit_ref = spec.get("ref")
    ref = ref_allocator.claim(str(explicit_ref)) if explicit_ref else ref_allocator.next("SW")
    net = spec.get("net", f"NRST_{target}")
    ground = spec.get("ground", "GND")

    part = CircuitPart(
        ref=ref,
        lib_id=spec.get("lib_id", "Switch:SW_Push"),
        value="Reset",
        footprint=spec.get("footprint"),
        block=target if target else "mcu",
        role="reset_button",
        properties={"KICAD_MCP_ROLE": "reset_button", "KICAD_MCP_TARGET": target},
    )
    parts.append(part)

    endpoints.append(CircuitEndpoint(
        ref=ref, pin="1", net=net,
        required=True, source="support_circuit:reset_button",
    ))
    endpoints.append(CircuitEndpoint(
        ref=ref, pin="2", net=ground,
        required=True, source="support_circuit:reset_button",
    ))

    # Connect to target reset pin if specified
    target_pin = spec.get("target_pin", "NRST")
    if target and target_pin:
        endpoints.append(CircuitEndpoint(
            ref=target, pin=target_pin, net=net,
            required=True, source="support_circuit:reset_button",
        ))

    return parts, endpoints


def _normalize_led(
    spec: dict[str, Any],
    target: str,
    ref_allocator: GeneratedRefAllocator,
) -> tuple[list[CircuitPart], list[CircuitEndpoint]]:
    """Generate LED with current-limiting resistor."""
    parts: list[CircuitPart] = []
    endpoints: list[CircuitEndpoint] = []

    explicit_led_ref = spec.get("ref")
    led_ref = ref_allocator.claim(str(explicit_led_ref)) if explicit_led_ref else ref_allocator.next("D")
    explicit_resistor_ref = spec.get("resistor_ref")
    resistor_ref = (
        ref_allocator.claim(str(explicit_resistor_ref))
        if explicit_resistor_ref
        else ref_allocator.next("R")
    )
    net = spec.get("net", f"LED_{target}")
    rail = spec.get("rail", "+3V3")
    ground = spec.get("ground", "GND")
    resistor_value = spec.get("resistor", "1k")

    led = CircuitPart(
        ref=led_ref,
        lib_id=spec.get("lib_id", "Device:LED"),
        value=spec.get("color", "Green"),
        footprint=spec.get("footprint"),
        block=target if target else "default",
        role="led",
        properties={"KICAD_MCP_ROLE": "led", "KICAD_MCP_TARGET": target},
    )
    parts.append(led)

    resistor = CircuitPart(
        ref=resistor_ref,
        lib_id="Device:R",
        value=resistor_value,
        footprint="Resistor_SMD:R_0402_1005Metric",
        block=target if target else "default",
        role="current_limit",
        properties={"KICAD_MCP_ROLE": "current_limit", "KICAD_MCP_TARGET": led_ref},
    )
    parts.append(resistor)

    # Rail -> Resistor -> LED -> GND
    endpoints.append(CircuitEndpoint(
        ref=resistor_ref, pin="1", net=rail,
        required=True, source="support_circuit:led",
    ))
    endpoints.append(CircuitEndpoint(
        ref=resistor_ref, pin="2", net=net,
        required=True, source="support_circuit:led",
    ))
    endpoints.append(CircuitEndpoint(
        ref=led_ref, pin="A", net=net,
        required=True, source="support_circuit:led",
    ))
    endpoints.append(CircuitEndpoint(
        ref=led_ref, pin="K", net=ground,
        required=True, source="support_circuit:led",
    ))

    return parts, endpoints


def _normalize_ferrite(
    spec: dict[str, Any],
    target: str,
    ref_allocator: GeneratedRefAllocator,
) -> tuple[list[CircuitPart], list[CircuitEndpoint]]:
    """Generate ferrite bead."""
    parts: list[CircuitPart] = []
    endpoints: list[CircuitEndpoint] = []

    explicit_ref = spec.get("ref")
    ref = ref_allocator.claim(str(explicit_ref)) if explicit_ref else ref_allocator.next("FB")
    input_net = spec.get("input_net", "+5V")
    output_net = spec.get("output_net", "+5V_F")
    value = spec.get("value", "600R@100MHz")

    part = CircuitPart(
        ref=ref,
        lib_id=spec.get("lib_id", "Device:FerriteBead"),
        value=value,
        footprint=spec.get("footprint", "Inductor_SMD:L_0402_1005Metric"),
        block=target if target else "power",
        role="ferrite",
        properties={"KICAD_MCP_ROLE": "ferrite", "KICAD_MCP_TARGET": target},
    )
    parts.append(part)

    endpoints.append(CircuitEndpoint(
        ref=ref, pin="1", net=input_net,
        required=True, source="support_circuit:ferrite",
    ))
    endpoints.append(CircuitEndpoint(
        ref=ref, pin="2", net=output_net,
        required=True, source="support_circuit:ferrite",
    ))

    return parts, endpoints


def _normalize_pin_rule(rule: dict[str, Any]) -> list[CircuitEndpoint]:
    """Convert a pin rule into explicit endpoints.

    Supports two formats:
    1. Direct: {"ref": "U1", "pins": ["PA0", "PA1"], "net": "+3V3"}
    2. Match: {"ref": "U1", "match": {"name_regex": "^(VDD|VDDA)$"}, "net": "+3V3"}

    For match format, the regex pattern is stored and will be resolved
    against actual symbol pins at compilation time.
    """
    endpoints: list[CircuitEndpoint] = []
    ref = rule.get("ref", "")
    net = rule.get("net", "")

    # Direct pin list format
    pins = rule.get("pins", [])
    if isinstance(pins, str):
        pins = [pins]

    for pin in pins:
        if ref and pin and net:
            endpoints.append(CircuitEndpoint(
                ref=ref,
                pin=str(pin),
                net=net,
                required=rule.get("required", True),
                allow_hidden=rule.get("allow_hidden", False),
                source="pin_rules",
            ))

    # Match format: resolve regex patterns to pin names
    match_spec = rule.get("match")
    if match_spec and isinstance(match_spec, dict) and ref and net:
        name_regex = match_spec.get("name_regex", "")
        if name_regex:
            # Store as a special endpoint with the regex pattern
            # The pin field contains the regex for later resolution
            endpoints.append(CircuitEndpoint(
                ref=ref,
                pin=f"__regex__:{name_regex}",
                net=net,
                required=rule.get("required", True),
                allow_hidden=rule.get("allow_hidden", True),
                source="pin_rules:match",
            ))

    return endpoints


def _validate_no_duplicate_refs(parts: list[CircuitPart]) -> None:
    """Validate there are no duplicate reference designators."""
    seen: dict[str, int] = {}
    for part in parts:
        if part.ref in seen:
            raise ValueError(
                f"Duplicate reference designator '{part.ref}' at index {seen[part.ref]} "
                f"and later. Each part must have a unique ref."
            )
        seen[part.ref] = len(seen)
