"""
Pin-map helpers for KiCad schematic symbols.
"""

from __future__ import annotations

import math
from typing import Any

from kicad_mcp.utils.kicad_s_expr import KiCadSchematic, SExprAtom, SExprList
from kicad_mcp.utils.library_resolver import KiCadLibraryError, resolve_symbol
from kicad_mcp.utils.native_netlist import export_native_netlist


def get_symbol_pin_map(schematic_path: str, reference: str) -> dict[str, Any]:
    """Return transformed pin positions for a placed schematic symbol."""
    schematic = KiCadSchematic.from_file(schematic_path)
    symbol = _symbol_by_reference(schematic, reference)
    if symbol is None:
        return {
            "success": False,
            "schematic_path": schematic_path,
            "reference": reference,
            "error": f"Symbol not found: {reference}",
        }
    lib_id = symbol.get("lib_id")
    if not lib_id:
        return {
            "success": False,
            "schematic_path": schematic_path,
            "reference": reference,
            "error": f"Symbol {reference} has no lib_id",
        }
    try:
        resolved = resolve_symbol(lib_id)
    except KiCadLibraryError as exc:
        return {
            "success": False,
            "schematic_path": schematic_path,
            "reference": reference,
            "lib_id": lib_id,
            "error": str(exc),
        }
    pins = _extract_library_pins(resolved["node"])
    position = symbol["position"]
    transformed = [
        _transform_pin(pin, position["x"], position["y"], position.get("angle", 0.0))
        for pin in pins
    ]
    return {
        "success": True,
        "schematic_path": schematic_path,
        "reference": reference,
        "lib_id": lib_id,
        "position": position,
        "pin_count": len(transformed),
        "pins": transformed,
    }


def attach_net_to_pin(
    schematic: KiCadSchematic,
    schematic_path: str,
    reference: str,
    pin: str,
    net_name: str,
    label_type: str = "global",
    stub_length_mm: float = 5.08,
    allow_hidden_power: bool = False,
) -> dict[str, Any]:
    """Attach a short wire and label to a symbol pin in a loaded schematic."""
    pin_map = get_symbol_pin_map(schematic_path, reference)
    if not pin_map.get("success"):
        raise ValueError(str(pin_map.get("error", "Unable to resolve pin map")))
    matches = [
        item
        for item in pin_map["pins"]
        if item["number"] == pin or item["name"] == pin or item["pinfunction"] == pin
    ]
    if not matches:
        raise ValueError(f"Pin not found on {reference}: {pin}")
    if len(matches) > 1:
        raise ValueError(f"Pin selector is ambiguous on {reference}: {pin}")
    selected = matches[0]
    if selected.get("hidden") and not allow_hidden_power:
        raise ValueError(
            f"Pin {reference}.{selected['number']} is hidden; pass allow_hidden_power=True "
            "only when this hidden attachment is intentional."
        )
    angle = selected["position"].get("angle", 0.0)
    start = {"x": selected["position"]["x"], "y": selected["position"]["y"]}
    end = {
        "x": start["x"] + math.cos(math.radians(angle)) * stub_length_mm,
        "y": start["y"] + math.sin(math.radians(angle)) * stub_length_mm,
    }
    wire = schematic.add_wire([start, end])
    label = schematic.add_label(net_name, end["x"], end["y"], label_type, angle)
    return {
        "reference": reference,
        "pin": selected,
        "net_name": net_name,
        "wire": wire,
        "label": label,
    }


def verify_native_net_membership(
    schematic_path: str, reference: str, pin: str, net_name: str
) -> dict[str, Any]:
    """Verify a ref/pin membership using KiCad's native netlist export."""
    native = export_native_netlist(schematic_path)
    if not native.get("success"):
        return {
            "success": False,
            "reason": native.get("error", "Native netlist export failed"),
            "native_netlist": native,
        }
    net = native.get("nets", {}).get(net_name)
    if not net:
        return {"success": False, "reason": f"Net not found: {net_name}", "native_netlist": native}
    for node in net.get("nodes", []):
        pinfunction = node.get("pinfunction", "")
        if node.get("ref") == reference and (
            node.get("pin") == pin or pinfunction == pin or pinfunction.startswith(f"{pin}_")
        ):
            return {"success": True, "reason": "native netlist membership verified"}
    return {
        "success": False,
        "reason": f"{reference}.{pin} was not found on net {net_name}",
        "native_netlist": native,
    }


def _symbol_by_reference(schematic: KiCadSchematic, reference: str) -> dict[str, Any] | None:
    for symbol in schematic.list_symbols():
        if symbol.get("reference") == reference:
            return symbol
    return None


def _extract_library_pins(node: SExprList) -> list[dict[str, Any]]:
    pins: list[dict[str, Any]] = []
    _collect_pins(node, pins)
    return pins


def _collect_pins(node: SExprList, pins: list[dict[str, Any]]) -> None:
    if node.head() == "pin":
        pins.append(_pin_to_dict(node))
    for child in node.child_lists():
        _collect_pins(child, pins)


def _pin_to_dict(pin: SExprList) -> dict[str, Any]:
    at = _parse_at(pin)
    name = _child_text(pin, "name") or ""
    number = _child_text(pin, "number") or ""
    pintype = _atom_text(pin.items[1] if len(pin.items) > 1 else None) or ""
    shape = _atom_text(pin.items[2] if len(pin.items) > 2 else None) or ""
    hidden = any(
        (isinstance(item, SExprAtom) and item.value == "hide")
        or (isinstance(item, SExprList) and item.head() == "hide")
        for item in pin.items
    )
    return {
        "number": number,
        "name": name,
        "pinfunction": f"{name}_{number}" if name and number else name or number,
        "pintype": pintype,
        "shape": shape,
        "hidden": hidden,
        "local_position": at,
    }


def _transform_pin(pin: dict[str, Any], origin_x: float, origin_y: float, angle: float) -> dict[str, Any]:
    local = pin["local_position"]
    radians = math.radians(angle)
    x = local["x"] * math.cos(radians) - local["y"] * math.sin(radians) + origin_x
    y = local["x"] * math.sin(radians) + local["y"] * math.cos(radians) + origin_y
    transformed = dict(pin)
    transformed["position"] = {
        "x": round(x, 6),
        "y": round(y, 6),
        "angle": (local.get("angle", 0.0) + angle) % 360,
    }
    return transformed


def _parse_at(expr: SExprList) -> dict[str, float]:
    at = expr.first_child("at")
    if at is None:
        return {"x": 0.0, "y": 0.0, "angle": 0.0}
    values = []
    for item in at.items[1:4]:
        try:
            values.append(float(_atom_text(item) or "0"))
        except ValueError:
            values.append(0.0)
    while len(values) < 3:
        values.append(0.0)
    return {"x": values[0], "y": values[1], "angle": values[2]}


def _child_text(expr: SExprList, head: str) -> str | None:
    child = expr.first_child(head)
    if child is None or len(child.items) < 2:
        return None
    return _atom_text(child.items[1])


def _atom_text(node: object | None) -> str | None:
    return node.value if isinstance(node, SExprAtom) else None
