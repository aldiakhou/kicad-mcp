"""Normalize design intent into canonical circuit representation.

Converts the high-level design intent format (parts, rails, interfaces,
support_circuits, etc.) into a CanonicalCircuit with explicit parts and
pin-to-net endpoints.
"""

from __future__ import annotations

import re
from typing import Any

from kicad_mcp.schematic_engine.custom_symbols import (
    CUSTOM_PINS_PROPERTY,
    custom_lib_id,
    decode_custom_pins,
    encode_custom_pins,
    is_custom_lib_id,
    normalize_custom_pins,
)
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
    no_connect_summary: dict[str, Any] = {
        "requested_count": 0,
        "matched_count": 0,
        "emitted_count": 0,
        "skipped_connected_count": 0,
        "skipped_hidden_count": 0,
        "matched_zero_pins_count": 0,
        "matched_zero_pins": [],
        "unmatched_rule_count": 0,
        "rules": [],
        "warnings": [],
    }

    # --- Extract parts ---
    for part_spec in intent.get("parts", []):
        part = _normalize_part(part_spec)
        parts.append(part)
        block_name = part.block
        blocks.setdefault(block_name, []).append(part.ref)

    ref_allocator = GeneratedRefAllocator({part.ref for part in parts})

    # --- Extract rails ---
    for rail_spec in _normalize_rail_specs(intent.get("rails", [])):
        rail_name = rail_spec.get("net") or rail_spec.get("name", "")
        if not rail_name:
            continue
        rails.add(rail_name)
        for connection in _rail_connections(rail_spec):
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
    for bulk in _normalize_object_list(intent.get("bulk_connections", []), "bulk_connections"):
        net_name = bulk.get("net", "")
        if not net_name and bulk.get("type") == "pin_to_net":
            net_name = bulk.get("net_name", "")
        if not net_name:
            continue
        for conn in _bulk_connections(bulk):
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
    for sc in _normalize_grouped_entries(
        intent.get("support_circuits", []),
        "support_circuits",
        type_key="type",
    ):
        sc_parts, sc_endpoints = _normalize_support_circuit(sc, ref_allocator)
        parts.extend(sc_parts)
        endpoints.extend(sc_endpoints)
        for p in sc_parts:
            blocks.setdefault(p.block, []).append(p.ref)

    parts_by_ref = {part.ref: part for part in parts}

    # --- Extract pin_rules ---
    for rule in _normalize_object_list(intent.get("pin_rules", []), "pin_rules"):
        rule_endpoints = _normalize_pin_rule(rule, parts_by_ref)
        endpoints.extend(rule_endpoints)

    _validate_endpoint_conflicts(parts_by_ref, endpoints)
    connected_keys = {
        _resolved_endpoint_key(parts_by_ref, endpoint.ref, endpoint.pin)
        for endpoint in endpoints
    }

    # --- Extract no_connect_rules ---
    for index, nc_rule in enumerate(
        _normalize_object_list(intent.get("no_connect_rules", []), "no_connect_rules")
    ):
        markers, rule_summary = _normalize_no_connect_rule(
            nc_rule,
            parts_by_ref,
            connected_keys,
            f"no_connect_rules[{index}]",
        )
        no_connects.extend(markers)
        _merge_no_connect_summary(no_connect_summary, rule_summary)

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
        no_connect_summary=no_connect_summary,
    )


def _normalize_part(part_spec: dict[str, Any]) -> CircuitPart:
    """Convert a part spec dict to a CircuitPart."""
    if not isinstance(part_spec, dict):
        raise ValueError(f"Part entry must be an object: {part_spec!r}")
    ref = part_spec.get("ref", "")
    if not ref:
        raise ValueError(f"Part missing 'ref': {part_spec}")
    lib_id = part_spec.get("lib_id", "")
    value = part_spec.get("value", "")
    properties = {}
    if not lib_id:
        if "pins" not in part_spec:
            raise ValueError(f"Part '{ref}' missing 'lib_id' or custom 'pins'")
        custom_pins = normalize_custom_pins(part_spec.get("pins"), ref=ref)
        lib_id = custom_lib_id(ref, value or ref)
        properties[CUSTOM_PINS_PROPERTY] = encode_custom_pins(custom_pins)
        properties["KICAD_MCP_CUSTOM_SYMBOL"] = "true"

    footprint = part_spec.get("footprint")
    block = part_spec.get("block", "default")
    role = part_spec.get("role")

    # Apply default footprint for passives
    if not footprint:
        prefix = re.match(r"^[A-Z]+", ref)
        if prefix and prefix.group() in _PASSIVE_FOOTPRINT_DEFAULTS:
            footprint = _PASSIVE_FOOTPRINT_DEFAULTS[prefix.group()]

    # Carry over extra properties
    for key in ("position", "angle", "mirror"):
        if key in part_spec:
            properties[key] = str(part_spec[key])
    for key, value_item in (part_spec.get("properties") or {}).items():
        properties[str(key)] = str(value_item)

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
    if not isinstance(iface_spec, dict):
        raise ValueError(f"Interface entry must be an object: {iface_spec!r}")
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

    if str(iface_type).lower() == "i2c":
        endpoints.extend(_normalize_i2c_interface(iface_spec))

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


def _normalize_i2c_interface(iface_spec: dict[str, Any]) -> list[CircuitEndpoint]:
    """Normalize the public schema's controller/devices I2C shorthand."""
    endpoints: list[CircuitEndpoint] = []
    controller = iface_spec.get("controller")
    devices = iface_spec.get("devices", [])
    if not isinstance(controller, dict) or not isinstance(devices, list):
        return endpoints

    iface_name = str(iface_spec.get("name") or "I2C").upper()
    scl_net = str(iface_spec.get("scl_net") or f"{iface_name}_SCL")
    sda_net = str(iface_spec.get("sda_net") or f"{iface_name}_SDA")
    controller_ref = str(controller.get("ref") or "")
    if controller_ref:
        if controller.get("scl"):
            endpoints.append(CircuitEndpoint(
                ref=controller_ref,
                pin=str(controller["scl"]),
                net=scl_net,
                required=True,
                source=f"interface:{iface_spec.get('type', 'i2c')}:controller",
            ))
        if controller.get("sda"):
            endpoints.append(CircuitEndpoint(
                ref=controller_ref,
                pin=str(controller["sda"]),
                net=sda_net,
                required=True,
                source=f"interface:{iface_spec.get('type', 'i2c')}:controller",
            ))

    for device in devices:
        if not isinstance(device, dict):
            continue
        ref = str(device.get("ref") or "")
        if not ref:
            continue
        if device.get("scl"):
            endpoints.append(CircuitEndpoint(
                ref=ref,
                pin=str(device["scl"]),
                net=scl_net,
                required=True,
                source=f"interface:{iface_spec.get('type', 'i2c')}:device",
            ))
        if device.get("sda"):
            endpoints.append(CircuitEndpoint(
                ref=ref,
                pin=str(device["sda"]),
                net=sda_net,
                required=True,
                source=f"interface:{iface_spec.get('type', 'i2c')}:device",
            ))
    return endpoints


def _normalize_support_circuit(
    sc_spec: dict[str, Any],
    ref_allocator: GeneratedRefAllocator,
) -> tuple[list[CircuitPart], list[CircuitEndpoint]]:
    """Convert a support circuit spec to parts and endpoints."""
    if not isinstance(sc_spec, dict):
        raise ValueError(f"Support circuit entry must be an object: {sc_spec!r}")
    parts: list[CircuitPart] = []
    endpoints: list[CircuitEndpoint] = []
    sc_type = _canonical_support_type(str(sc_spec.get("type", "")))
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
    elif sc_type == "power_flag":
        parts, endpoints = _normalize_power_flag(sc_spec, ref_allocator)
    elif sc_type == "connector_header":
        parts, endpoints = _normalize_connector_header(sc_spec, ref_allocator)
    elif sc_type:
        raise ValueError(f"Unsupported support circuit type '{sc_spec.get('type')}'")

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
    if pull_type == "pulldown":
        rail = spec.get("ground", rail)
    value = spec.get("value", "4.7k")
    signals = spec.get("signals", [])
    if not signals:
        signal = spec.get("net")
        if signal:
            signals = [signal]
    target_pin = spec.get("pin") or spec.get("target_pin")
    target_ref = spec.get("ref") or target
    target_net = spec.get("net")
    if target_pin and target_ref and target_net and target_net not in signals:
        signals = [*signals, target_net]
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

        if target_pin and target_ref and net_name == target_net:
            endpoints.append(CircuitEndpoint(
                ref=str(target_ref), pin=str(target_pin), net=net_name,
                required=True, source=f"support_circuit:{pull_type}:target",
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


def _normalize_power_flag(
    spec: dict[str, Any],
    ref_allocator: GeneratedRefAllocator,
) -> tuple[list[CircuitPart], list[CircuitEndpoint]]:
    explicit_ref = spec.get("ref")
    ref = ref_allocator.claim(str(explicit_ref)) if explicit_ref else ref_allocator.next("#FLG")
    net = spec.get("net") or spec.get("rail") or "+3V3"
    part = CircuitPart(
        ref=ref,
        lib_id="power:PWR_FLAG",
        value="PWR_FLAG",
        footprint=None,
        block="power",
        role="power_flag",
        properties={"KICAD_MCP_ROLE": "power_flag"},
    )
    return [part], [
        CircuitEndpoint(
            ref=ref,
            pin="1",
            net=str(net),
            required=True,
            allow_hidden=True,
            source="support_circuit:power_flag",
        )
    ]


def _normalize_connector_header(
    spec: dict[str, Any],
    ref_allocator: GeneratedRefAllocator,
) -> tuple[list[CircuitPart], list[CircuitEndpoint]]:
    explicit_ref = spec.get("ref")
    ref = ref_allocator.claim(str(explicit_ref)) if explicit_ref else ref_allocator.next("J")
    nets = spec.get("pins") or spec.get("nets") or []
    if isinstance(nets, str):
        nets = [nets]
    pin_count = int(spec.get("pin_count") or len(nets) or 1)
    part = CircuitPart(
        ref=ref,
        lib_id=spec.get("lib_id", f"Connector_Generic:Conn_01x{pin_count:02d}"),
        value=spec.get("value") or spec.get("name") or f"Conn_01x{pin_count:02d}",
        footprint=spec.get(
            "footprint",
            f"Connector_PinHeader_2.54mm:PinHeader_1x{pin_count:02d}_P2.54mm_Vertical",
        ),
        block=spec.get("target") or "interfaces",
        role="connector_header",
        properties={"KICAD_MCP_ROLE": "connector_header"},
    )
    endpoints = [
        CircuitEndpoint(
            ref=ref,
            pin=str(index),
            net=str(net),
            required=True,
            source="support_circuit:connector_header",
        )
        for index, net in enumerate(nets, start=1)
        if str(net)
    ]
    return [part], endpoints


def _normalize_pin_rule(
    rule: dict[str, Any],
    parts_by_ref: dict[str, CircuitPart] | None = None,
) -> list[CircuitEndpoint]:
    """Convert a pin rule into explicit endpoints.

    Supports two formats:
    1. Direct: {"ref": "U1", "pins": ["PA0", "PA1"], "net": "+3V3"}
    2. Match: {"ref": "U1", "match": {"name_regex": "^(VDD|VDDA)$"}, "net": "+3V3"}

    Match format is resolved during normalization so the writer, expected
    netlist, no-connect handling, and pre-commit checks all use the same pins.
    """
    endpoints: list[CircuitEndpoint] = []
    if not isinstance(rule, dict):
        raise ValueError(f"Pin rule entry must be an object: {rule!r}")
    ref = rule.get("ref", "")
    net = rule.get("net", "")

    # Direct pin list format
    pins = rule.get("pins", [])
    pin = rule.get("pin") or rule.get("number") or rule.get("name")
    if pin:
        pins = [pin, *pins] if isinstance(pins, list) else [pin, pins]
    names = rule.get("names", [])
    numbers = rule.get("numbers", [])
    for extra in (names, numbers):
        if isinstance(extra, str):
            pins = [*pins, extra] if isinstance(pins, list) else [pins, extra]
        elif isinstance(extra, list):
            pins = [*pins, *extra] if isinstance(pins, list) else [pins, *extra]
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

    # Match format: resolve selectors to concrete symbol pins.
    match_spec = rule.get("match")
    if not match_spec and not pins:
        match_spec = {
            key: rule.get(key)
            for key in (
                "pin",
                "pins",
                "name",
                "number",
                "names",
                "numbers",
                "name_regex",
                "number_regex",
                "pin_type",
                "name_contains",
            )
            if rule.get(key)
        }
    if match_spec and isinstance(match_spec, dict) and ref and net:
        if parts_by_ref is None:
            raise ValueError("pin_rules match selectors require part metadata")
        part = parts_by_ref.get(str(ref))
        if part is None:
            raise ValueError(f"pin_rules ref not found: {ref}")
        pins_matched = _select_part_pins(part, match_spec)
        if not pins_matched:
            raise ValueError(
                f"pin_rules selector matched zero pins for {ref}: {match_spec}"
            )
        for pin_info in pins_matched:
            endpoints.append(CircuitEndpoint(
                ref=ref,
                pin=_pin_identifier(part, pin_info),
                net=net,
                required=rule.get("required", True),
                allow_hidden=rule.get("allow_hidden", True),
                source="pin_rules:match",
            ))

    return endpoints


def _normalize_no_connect_rule(
    rule: dict[str, Any],
    parts_by_ref: dict[str, CircuitPart],
    connected_keys: set[tuple[str, str]],
    path: str,
) -> tuple[list[tuple[str, str]], dict[str, Any]]:
    if not isinstance(rule, dict):
        raise ValueError(f"{path} must be an object")
    if rule.get("action", "mark_no_connect") != "mark_no_connect":
        raise ValueError(f"{path} action must be 'mark_no_connect'")

    ref = str(rule.get("ref") or "")
    markers: list[tuple[str, str]] = []
    summary: dict[str, Any] = {
        "path": path,
        "ref": ref,
        "selector": rule.get("match"),
        "requested_count": 0,
        "matched_count": 0,
        "emitted_count": 0,
        "skipped_connected": [],
        "skipped_hidden": [],
        "matched_zero_pins": [],
        "unmatched": False,
        "warnings": [],
    }
    if not ref:
        raise ValueError(f"{path} missing ref")

    candidate_pins: list[tuple[str, dict[str, Any] | None]] = []
    direct_pins = _rule_pin_list(rule)
    for pin in direct_pins:
        candidate_pins.append((pin, None))

    match_spec = rule.get("match")
    if match_spec:
        part = parts_by_ref.get(ref)
        if part is None:
            raise ValueError(f"{path} ref not found: {ref}")
        matched = _select_part_pins(part, match_spec)
        if not matched:
            summary["unmatched"] = True
            summary["matched_zero_pins"].append(
                {"path": path, "ref": ref, "selector": match_spec}
            )
            summary["warnings"].append("selector matched zero pins")
        for pin_info in matched:
            candidate_pins.append((_pin_identifier(part, pin_info), pin_info))

    except_pins = {str(item) for item in rule.get("except", [])}
    include_hidden = bool(rule.get("include_hidden", False))
    summary["requested_count"] = len(candidate_pins)
    summary["matched_count"] = len(candidate_pins)

    seen: set[tuple[str, str]] = set()
    for pin, pin_info in candidate_pins:
        if pin in except_pins:
            continue
        if pin_info is not None and (
            str(pin_info.get("name") or "") in except_pins
            or str(pin_info.get("number") or "") in except_pins
        ):
            continue
        if pin_info is not None and _pin_is_hidden(pin_info) and not include_hidden:
            summary["skipped_hidden"].append(
                {
                    "pin": pin,
                    "name": str(pin_info.get("name") or ""),
                    "number": str(pin_info.get("number") or ""),
                }
            )
            continue
        marker_key = _resolved_endpoint_key(parts_by_ref, ref, pin)
        if marker_key in connected_keys:
            summary["skipped_connected"].append({"ref": ref, "pin": pin})
            continue
        marker = (ref, pin)
        if marker in seen:
            continue
        seen.add(marker)
        markers.append(marker)

    summary["emitted_count"] = len(markers)
    return markers, summary


def _rule_pin_list(rule: dict[str, Any]) -> list[str]:
    pins = rule.get("pins", [])
    pin = rule.get("pin") or rule.get("number") or rule.get("name")
    if pin:
        pins = [pin, *pins] if isinstance(pins, list) else [pin, pins]
    for key in ("names", "numbers"):
        extra = rule.get(key, [])
        if isinstance(extra, str):
            pins = [*pins, extra] if isinstance(pins, list) else [pins, extra]
        elif isinstance(extra, list):
            pins = [*pins, *extra] if isinstance(pins, list) else [pins, *extra]
    if isinstance(pins, str):
        pins = [pins]
    return [str(item) for item in pins if str(item)]


def _merge_no_connect_summary(target: dict[str, Any], rule_summary: dict[str, Any]) -> None:
    target["rules"].append(rule_summary)
    target["requested_count"] += int(rule_summary.get("requested_count", 0))
    target["matched_count"] += int(rule_summary.get("matched_count", 0))
    target["emitted_count"] += int(rule_summary.get("emitted_count", 0))
    target["skipped_connected_count"] += len(rule_summary.get("skipped_connected", []))
    target["skipped_hidden_count"] += len(rule_summary.get("skipped_hidden", []))
    target["matched_zero_pins_count"] += len(rule_summary.get("matched_zero_pins", []))
    target["matched_zero_pins"].extend(rule_summary.get("matched_zero_pins", []))
    if rule_summary.get("unmatched"):
        target["unmatched_rule_count"] += 1
    for warning in rule_summary.get("warnings", []):
        target["warnings"].append(
            {
                "path": rule_summary.get("path"),
                "ref": rule_summary.get("ref"),
                "warning": warning,
                "selector": rule_summary.get("selector"),
            }
        )


def _validate_endpoint_conflicts(
    parts_by_ref: dict[str, CircuitPart],
    endpoints: list[CircuitEndpoint],
) -> None:
    assignments: dict[tuple[str, str], CircuitEndpoint] = {}
    for endpoint in endpoints:
        key = _resolved_endpoint_key(parts_by_ref, endpoint.ref, endpoint.pin)
        existing = assignments.get(key)
        if existing and existing.net != endpoint.net:
            raise ValueError(
                "same ref/pin assigned to multiple nets: "
                f"{endpoint.ref}.{endpoint.pin} is on both "
                f"{existing.net} and {endpoint.net}"
            )
        assignments[key] = endpoint


def _resolved_endpoint_key(
    parts_by_ref: dict[str, CircuitPart],
    ref: str,
    pin: str,
) -> tuple[str, str]:
    part = parts_by_ref.get(str(ref))
    if part is None:
        return str(ref), _pin_lookup_key(pin)
    matches = _exact_pin_matches(part, str(pin))
    if len(matches) == 1:
        number = str(matches[0].get("number") or "")
        name = str(matches[0].get("name") or "")
        return str(ref), _pin_lookup_key(number or name or pin)
    return str(ref), _pin_lookup_key(pin)


def _exact_pin_matches(part: CircuitPart, requested_pin: str) -> list[dict[str, Any]]:
    requested = str(requested_pin)
    try:
        pins = _part_pins(part)
    except ValueError:
        return []
    return [
        pin
        for pin in pins
        if requested in _pin_values(pin)
    ]


def _select_part_pins(part: CircuitPart, selector: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(selector, dict):
        return []
    exclude = selector.get("exclude")
    positive = {key: value for key, value in selector.items() if key != "exclude"}
    try:
        matches = [pin for pin in _part_pins(part) if _pin_matches(pin, positive)]
        if isinstance(exclude, dict):
            matches = [pin for pin in matches if not _pin_matches(pin, exclude)]
    except re.error as exc:
        raise ValueError(f"invalid pin selector regex for {part.ref}: {exc}") from exc
    return matches


def _part_pins(part: CircuitPart) -> list[dict[str, Any]]:
    if is_custom_lib_id(part.lib_id):
        return [
            {
                "number": pin["number"],
                "name": pin["name"],
                "pinfunction": f"{pin['name']}_{pin['number']}",
                "pintype": pin.get("pintype", "bidirectional"),
                "hidden": False,
            }
            for pin in decode_custom_pins(part.properties.get(CUSTOM_PINS_PROPERTY))
        ]
    try:
        from kicad_mcp.utils.schematic_pins import _resolve_symbol_pins

        return _resolve_symbol_pins(part.lib_id)
    except Exception as exc:
        raise ValueError(f"Symbol {part.lib_id} for {part.ref} could not be resolved") from exc


def _pin_matches(pin: dict[str, Any], selector: dict[str, Any]) -> bool:
    if not selector:
        return True
    for key, expected in selector.items():
        if key == "pin":
            if str(expected) not in _pin_values(pin):
                return False
        elif key == "pins":
            expected_values = (
                {str(item) for item in expected}
                if isinstance(expected, list)
                else {str(expected)}
            )
            if not expected_values.intersection(_pin_values(pin)):
                return False
        elif key == "name":
            if str(pin.get("name") or "") != str(expected):
                return False
        elif key == "number":
            if str(pin.get("number") or "") != str(expected):
                return False
        elif key == "names":
            expected_values = (
                {str(item) for item in expected}
                if isinstance(expected, list)
                else {str(expected)}
            )
            if str(pin.get("name") or "") not in expected_values:
                return False
        elif key == "numbers":
            expected_values = (
                {str(item) for item in expected}
                if isinstance(expected, list)
                else {str(expected)}
            )
            if str(pin.get("number") or "") not in expected_values:
                return False
        elif key == "name_regex":
            if not re.search(str(expected), str(pin.get("name") or "")):
                return False
        elif key == "number_regex":
            if not re.search(str(expected), str(pin.get("number") or "")):
                return False
        elif key == "pin_type":
            pin_type = str(pin.get("pintype") or pin.get("type") or "").lower()
            if pin_type != str(expected).lower():
                return False
        elif key == "name_contains":
            if str(expected).lower() not in str(pin.get("name") or "").lower():
                return False
        else:
            return False
    return True


def _pin_values(pin: dict[str, Any]) -> set[str]:
    return {
        str(value)
        for value in (pin.get("name"), pin.get("number"), pin.get("pinfunction"))
        if value is not None and str(value)
    }


def _pin_identifier(part: CircuitPart, pin: dict[str, Any]) -> str:
    name = str(pin.get("name") or "")
    number = str(pin.get("number") or "")
    if name and _pin_name_counts(part).get(name, 0) == 1:
        return name
    return number or name


def _pin_name_counts(part: CircuitPart) -> dict[str, int]:
    counts: dict[str, int] = {}
    for pin in _part_pins(part):
        name = str(pin.get("name") or "")
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def _pin_is_hidden(pin: dict[str, Any]) -> bool:
    return bool(pin.get("hidden"))


def _pin_lookup_key(value: str) -> str:
    cleaned = (
        str(value or "")
        .replace("~{", "")
        .replace("}", "")
        .replace("{", "")
        .replace("~", "")
    )
    return "".join(cleaned.lower().split())


def _normalize_object_list(raw: Any, path: str) -> list[dict[str, Any]]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{path} must be a list")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"{path}[{index}] must be an object")
        result.append(dict(item))
    return result


def _normalize_grouped_entries(raw: Any, path: str, *, type_key: str) -> list[dict[str, Any]]:
    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        entries = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ValueError(f"{path}[{index}] must be an object")
            entries.append(dict(item))
        return entries
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be a list or grouped object")

    entries: list[dict[str, Any]] = []
    for group, group_value in raw.items():
        if group_value in (None, ""):
            continue
        items = group_value if isinstance(group_value, list) else [group_value]
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"{path}.{group}[{index}] must be an object")
            normalized = dict(item)
            normalized.setdefault(type_key, group)
            entries.append(normalized)
    return entries


def _normalize_rail_specs(raw: Any) -> list[dict[str, Any]]:
    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        return _normalize_object_list(raw, "rails")
    if not isinstance(raw, dict):
        raise ValueError("rails must be a list or object")
    result: list[dict[str, Any]] = []
    for rail_name, spec in raw.items():
        if isinstance(spec, dict):
            normalized = dict(spec)
        elif isinstance(spec, list):
            normalized = {"pins": spec}
        else:
            raise ValueError(f"rails.{rail_name} must be an object or pins list")
        normalized.setdefault("name", str(rail_name))
        result.append(normalized)
    return result


def _rail_connections(rail_spec: dict[str, Any]) -> list[dict[str, Any]]:
    connections = rail_spec.get("connections", [])
    if connections:
        if not isinstance(connections, list):
            raise ValueError("rail connections must be a list")
        return [dict(item) for item in connections if isinstance(item, dict)]

    pins = rail_spec.get("pins", [])
    if isinstance(pins, str):
        pins = [pins]
    result: list[dict[str, Any]] = []
    for item in pins:
        if isinstance(item, dict):
            result.append(dict(item))
        elif isinstance(item, list | tuple) and len(item) >= 2:
            result.append({"ref": item[0], "pins": [item[1]]})
        elif isinstance(item, str) and ":" in item:
            ref, pin = item.split(":", 1)
            result.append({"ref": ref, "pins": [pin]})
        else:
            raise ValueError(f"Invalid rail pin entry: {item!r}")
    return result


def _bulk_connections(bulk: dict[str, Any]) -> list[dict[str, Any]]:
    if bulk.get("ref") and bulk.get("pin"):
        return [{"ref": bulk["ref"], "pin": bulk["pin"]}]

    endpoints = bulk.get("endpoints", [])
    if endpoints:
        if not isinstance(endpoints, list):
            raise ValueError("bulk connection endpoints must be a list")
        return [dict(item) for item in endpoints if isinstance(item, dict)]

    pins = bulk.get("pins", [])
    if isinstance(pins, str):
        pins = [pins]
    result: list[dict[str, Any]] = []
    for item in pins:
        if isinstance(item, dict):
            result.append(dict(item))
        elif isinstance(item, list | tuple) and len(item) >= 2:
            result.append({"ref": item[0], "pin": item[1]})
        elif isinstance(item, str) and ":" in item:
            ref, pin = item.split(":", 1)
            result.append({"ref": ref, "pin": pin})
        else:
            raise ValueError(f"Invalid bulk connection pin entry: {item!r}")
    return result


def _canonical_support_type(raw_type: str) -> str:
    aliases = {
        "led_indicator": "led",
        "ferrite_filter": "ferrite",
    }
    return aliases.get(raw_type, raw_type)


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
