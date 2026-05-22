"""
Pin-map helpers for KiCad schematic symbols.
"""

from __future__ import annotations

from functools import lru_cache
import math
from typing import Any

from kicad_mcp.utils.kicad_s_expr import KiCadSchematic, SExprAtom, SExprList
from kicad_mcp.utils.library_resolver import KiCadLibraryError, resolve_symbol
from kicad_mcp.utils.native_netlist import export_native_netlist

SCHEMATIC_GRID_MM = 1.27


def get_symbol_pin_map(schematic_path: str, reference: str) -> dict[str, Any]:
    """Return transformed pin positions for a placed schematic symbol."""
    schematic = KiCadSchematic.from_file(schematic_path)
    return get_symbol_pin_map_from_schematic(schematic, schematic_path, reference)


def get_symbol_pin_map_from_schematic(
    schematic: KiCadSchematic, schematic_path: str, reference: str
) -> dict[str, Any]:
    """Return transformed pin positions for a symbol in an already-loaded schematic."""
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
        pins = _resolve_symbol_pins(lib_id)
    except KiCadLibraryError as exc:
        return {
            "success": False,
            "schematic_path": schematic_path,
            "reference": reference,
            "lib_id": lib_id,
            "error": str(exc),
        }
    position = symbol["position"]
    transformed = [
        _transform_pin(pin, position["x"], position["y"], position.get("angle", 0.0))
        for pin in pins
    ]
    connection_groups = _connection_groups(transformed)
    return {
        "success": True,
        "schematic_path": schematic_path,
        "reference": reference,
        "lib_id": lib_id,
        "position": position,
        "pin_count": len(transformed),
        "deduplicated_pin_count": len(connection_groups),
        "pins": transformed,
        "connection_groups": connection_groups,
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
    pin_map = get_symbol_pin_map_from_schematic(schematic, schematic_path, reference)
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
    start = selected["connection_point"]
    end = {
        "x": _snap(start["x"] + math.cos(math.radians(angle)) * stub_length_mm),
        "y": _snap(start["y"] + math.sin(math.radians(angle)) * stub_length_mm),
    }
    # Pin-anchored labels are the most reliable KiCad-native way to bind a
    # labeled net to a symbol pin. Generated wire stubs are intentionally not
    # added here because they can merge nearby power nets when many hidden or
    # overlapping pins share one connection point.
    wire = None
    label = schematic.add_label(net_name, start["x"], start["y"], label_type, angle)
    return {
        "reference": reference,
        "pin": selected,
        "net_name": net_name,
        "wire": wire,
        "label": label,
        "stub_endpoint": end,
    }


def add_no_connect_to_pin(
    schematic: KiCadSchematic,
    schematic_path: str,
    reference: str,
    pin: str,
    allow_hidden_power: bool = False,
) -> dict[str, Any]:
    """Add a no-connect marker to an actual symbol pin coordinate."""
    pin_map = get_symbol_pin_map_from_schematic(schematic, schematic_path, reference)
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
        raise ValueError(f"Pin {reference}.{selected['number']} is hidden")
    marker = schematic.add_no_connect(
        selected["connection_point"]["x"], selected["connection_point"]["y"]
    )
    return {"reference": reference, "pin": selected, "no_connect": marker}


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


@lru_cache(maxsize=256)
def _resolve_symbol_pins_cached(lib_id: str) -> tuple[tuple[tuple[str, Any], ...], ...]:
    resolved = resolve_symbol(lib_id)
    pins = _extract_library_pins(resolved["node"])
    if pins:
        return _freeze_pins(pins)
    parent = _child_text(resolved["node"], "extends")
    seen = {lib_id}
    while parent:
        parent_lib_id = f"{resolved['library']}:{parent}"
        if parent_lib_id in seen:
            break
        seen.add(parent_lib_id)
        parent_resolved = resolve_symbol(parent_lib_id)
        pins = _extract_library_pins(parent_resolved["node"])
        if pins:
            return _freeze_pins(pins)
        parent = _child_text(parent_resolved["node"], "extends")
    return ()


def _resolve_symbol_pins(lib_id: str) -> list[dict[str, Any]]:
    pins = []
    for items in _resolve_symbol_pins_cached(lib_id):
        pin = dict(items)
        pin["local_position"] = dict(pin["local_position"])
        pins.append(pin)
    return pins


def _freeze_pins(pins: list[dict[str, Any]]) -> tuple[tuple[tuple[str, Any], ...], ...]:
    frozen = []
    for pin in pins:
        flat = dict(pin)
        flat["local_position"] = tuple(sorted(pin["local_position"].items()))
        frozen.append(tuple(sorted(flat.items())))
    return tuple(frozen)


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
    # KiCad symbol-library coordinates use positive Y upward, while sheet
    # coordinates use positive Y downward. Convert into sheet space before
    # applying the placed-symbol rotation.
    x = local["x"] * math.cos(radians) - local["y"] * math.sin(radians) + origin_x
    y = -local["x"] * math.sin(radians) - local["y"] * math.cos(radians) + origin_y
    transformed = dict(pin)
    connection_point = {"x": _snap(x), "y": _snap(y)}
    stub_angle = (local.get("angle", 0.0) - angle) % 360
    recommended_label_position = {
        "x": _snap(connection_point["x"] + math.cos(math.radians(stub_angle)) * 5.08),
        "y": _snap(connection_point["y"] + math.sin(math.radians(stub_angle)) * 5.08),
        "angle": stub_angle,
    }
    transformed["position"] = {
        "x": connection_point["x"],
        "y": connection_point["y"],
        "angle": stub_angle,
    }
    transformed["connection_point"] = connection_point
    transformed["stub_direction"] = {
        "angle": stub_angle,
        "dx": round(math.cos(math.radians(stub_angle)), 6),
        "dy": round(math.sin(math.radians(stub_angle)), 6),
    }
    transformed["recommended_label_position"] = recommended_label_position
    transformed["unit"] = 1
    transformed["variant"] = "default"
    return transformed


def _connection_groups(pins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[float, float, float], dict[str, Any]] = {}
    for pin in pins:
        position = pin["position"]
        key = (position["x"], position["y"], position["angle"])
        group = groups.setdefault(
            key,
            {
                "connection_point": pin["connection_point"],
                "angle": position["angle"],
                "pins": [],
            },
        )
        group["pins"].append(
            {
                "number": pin["number"],
                "name": pin["name"],
                "pinfunction": pin["pinfunction"],
                "hidden": pin["hidden"],
            }
        )
    return list(groups.values())


def _snap(value: float, grid: float = SCHEMATIC_GRID_MM) -> float:
    return round(round(value / grid) * grid, 6)


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
