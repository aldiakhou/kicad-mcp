"""
Pin-map helpers for KiCad schematic symbols.
"""

from __future__ import annotations

from functools import lru_cache
import math
from typing import Any

from kicad_mcp.utils.kicad_s_expr import KiCadSchematic, SExprAtom, SExprList
from kicad_mcp.utils.library_resolver import KiCadLibraryError, resolve_symbol
from kicad_mcp.utils.native_netlist import export_native_netlist, native_node_matches_endpoint

SCHEMATIC_GRID_MM = 1.27


class PinVisibility:
    VISIBLE = "visible"
    HIDDEN_POWER = "hidden_power"
    HIDDEN_NO_CONNECT = "hidden_no_connect"
    HIDDEN_OTHER = "hidden_other"


def classify_pin(pin: dict[str, Any]) -> str:
    """Classify visible and hidden symbol pins for safe intent handling."""
    if not pin.get("hidden"):
        return PinVisibility.VISIBLE

    name = str(pin.get("name") or "").upper()
    pin_type = str(pin.get("pintype") or pin.get("type") or "").lower()

    if name in {"NC", "DNC", "RES", "RESERVED", "N.C."} or "NC" in name:
        return PinVisibility.HIDDEN_NO_CONNECT
    if pin_type == "power_in" or name in {"VDD", "VCC", "VSS", "GND", "VBAT"}:
        return PinVisibility.HIDDEN_POWER
    return PinVisibility.HIDDEN_OTHER


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
        pins = _resolve_embedded_symbol_pins(schematic, lib_id)
        if not pins:
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
    *,
    label_placement: str = "pin_anchor",
    label_clearance_mm: float = 5.08,
    label_side: str = "auto",
    connection_style: str = "label",
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
    visibility = classify_pin(selected)
    if visibility != PinVisibility.VISIBLE and (
        visibility != PinVisibility.HIDDEN_POWER or not allow_hidden_power
    ):
        raise ValueError(
            f"Pin {reference}.{selected['number']} is hidden ({visibility}); pass "
            "allow_hidden_power=True only for intentional hidden power-pin attachments."
        )
    angle = selected["position"].get("angle", 0.0)
    start = selected["connection_point"]
    if connection_style == "auto" and _is_power_net(net_name):
        power = _attach_power_symbol_to_pin(schematic, net_name, start, angle)
        if power is not None:
            return {
                "reference": reference,
                "pin": selected,
                "net_name": net_name,
                "wire": None,
                "label": None,
                "power_symbol": power,
                "stub_endpoint": start,
            }
    end = {
        "x": _snap(start["x"] + math.cos(math.radians(angle)) * max(stub_length_mm, label_clearance_mm)),
        "y": _snap(start["y"] + math.sin(math.radians(angle)) * max(stub_length_mm, label_clearance_mm)),
    }
    if label_placement == "external_stubs":
        end = _place_external_label_endpoint(schematic, start, angle, net_name, label_side, label_clearance_mm)
        stub_points = _external_stub_points(start, end, angle, label_side, label_clearance_mm)
        wire = _add_stub_wires(schematic, stub_points)
        label = schematic.add_label(
            net_name,
            end["x"],
            end["y"],
            "local" if label_type == "global" else label_type,
            _readable_label_angle(angle),
        )
    else:
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


def _is_power_net(net_name: str) -> bool:
    normalized = net_name.upper()
    return normalized in {"GND", "AGND", "DGND", "+3V3", "+3.3V", "+5V", "VBUS", "VCC", "VDD"}


def _power_symbol_lib_id(net_name: str) -> str | None:
    aliases = {
        "GND": "power:GND",
        "AGND": "power:GNDA",
        "DGND": "power:GNDD",
        "+3V3": "power:+3V3",
        "+3.3V": "power:+3V3",
        "+5V": "power:+5V",
        "VBUS": "power:VBUS",
        "VCC": "power:VCC",
        "VDD": "power:VDD",
    }
    return aliases.get(net_name.upper())


def _attach_power_symbol_to_pin(
    schematic: KiCadSchematic,
    net_name: str,
    point: dict[str, float],
    pin_angle: float,
) -> dict[str, Any] | None:
    lib_id = _power_symbol_lib_id(net_name)
    if lib_id is None:
        return None
    try:
        resolved = resolve_symbol(lib_id)
    except Exception:
        return None
    ref = _next_power_reference(schematic)
    # Place the power symbol origin at the pin point. KiCad power-symbol pins are
    # defined at the symbol origin, so coincident placement gives a direct net tie.
    return schematic.add_symbol(
        lib_id,
        ref,
        net_name,
        point["x"],
        point["y"],
        _power_symbol_angle(pin_angle, net_name),
        None,
        None,
        resolved["node"],
    )


def _next_power_reference(schematic: KiCadSchematic) -> str:
    used = {symbol["reference"] for symbol in schematic.list_symbols()}
    for index in range(1, 10000):
        ref = f"#PWR{index:03d}"
        if ref not in used:
            return ref
    raise ValueError("Unable to allocate power symbol reference")


def _power_symbol_angle(pin_angle: float, net_name: str) -> float:
    if net_name.upper() in {"GND", "AGND", "DGND"}:
        return 0.0
    if int(pin_angle) % 360 == 90:
        return 180.0
    if int(pin_angle) % 360 == 270:
        return 0.0
    return 0.0


def _place_external_label_endpoint(
    schematic: KiCadSchematic,
    start: dict[str, float],
    angle: float,
    text: str,
    label_side: str,
    clearance_mm: float,
) -> dict[str, float]:
    escape_direction = _pin_escape_direction(angle, label_side)
    label_direction = _label_direction_from_angle(angle, label_side)
    length = max(clearance_mm, 7.62)
    normalized = int(round(angle / 90.0) * 90) % 360
    if normalized in {90, 270} and label_side not in {"top", "bottom"}:
        base = {
            "x": _snap(start["x"] + escape_direction["dx"] * length + label_direction["dx"] * length),
            "y": _snap(start["y"] + escape_direction["dy"] * length + label_direction["dy"] * length),
        }
    else:
        base = {
            "x": _snap(start["x"] + label_direction["dx"] * length),
            "y": _snap(start["y"] + label_direction["dy"] * length),
        }
    occupied = _label_placement_obstacles(schematic)
    label_angle = _readable_label_angle(angle)
    page_rect = _schematic_page_rect(schematic)
    perpendicular = {"dx": -label_direction["dy"], "dy": label_direction["dx"]}
    for push in range(0, 8):
        pushed_base = {
            "x": _snap(base["x"] + label_direction["dx"] * push * SCHEMATIC_GRID_MM * 2.0),
            "y": _snap(base["y"] + label_direction["dy"] * push * SCHEMATIC_GRID_MM * 2.0),
        }
        for step in range(0, 18):
            offset = step * SCHEMATIC_GRID_MM * 2.0
            signs = [1.0] if step == 0 else [1.0, -1.0]
            for sign in signs:
                candidate = {
                    "x": _snap(pushed_base["x"] + perpendicular["dx"] * offset * sign),
                    "y": _snap(pushed_base["y"] + perpendicular["dy"] * offset * sign),
                }
                candidate_rect = _text_rect(candidate, text, label_angle)
                if not _rect_inside(candidate_rect, page_rect):
                    continue
                if not any(_rects_intersect(candidate_rect, rect, padding=0.5) for rect in occupied):
                    return candidate
    for step in range(0, 18):
        offset = step * SCHEMATIC_GRID_MM * 2.0
        signs = [1.0] if step == 0 else [1.0, -1.0]
        for sign in signs:
            candidate = {
                "x": _snap(base["x"] + perpendicular["dx"] * offset * sign),
                "y": _snap(base["y"] + perpendicular["dy"] * offset * sign),
            }
            candidate_rect = _text_rect(candidate, text, label_angle)
            if _rect_inside(candidate_rect, page_rect):
                return candidate
    return base


def _external_stub_points(
    start: dict[str, float],
    end: dict[str, float],
    angle: float,
    label_side: str,
    clearance_mm: float,
) -> list[dict[str, float]]:
    escape_direction = _pin_escape_direction(angle, label_side)
    length = max(clearance_mm, 7.62)
    knee = {
        "x": _snap(start["x"] + escape_direction["dx"] * length),
        "y": _snap(start["y"] + escape_direction["dy"] * length),
    }
    if knee == end:
        return [start, end]
    return [start, knee, end]


def _add_stub_wires(schematic: KiCadSchematic, points: list[dict[str, float]]) -> Any:
    wires = []
    for start, end in zip(points, points[1:]):
        if start == end:
            continue
        wires.append(schematic.add_wire([start, end]))
    if len(wires) == 1:
        return wires[0]
    return {"segments": wires, "points": points}


def _direction_from_angle(angle: float, label_side: str) -> dict[str, float]:
    return _pin_escape_direction(angle, label_side)


def _pin_escape_direction(angle: float, label_side: str) -> dict[str, float]:
    if label_side == "right":
        return {"dx": 1.0, "dy": 0.0}
    if label_side == "left":
        return {"dx": -1.0, "dy": 0.0}
    if label_side == "top":
        return {"dx": 0.0, "dy": -1.0}
    if label_side == "bottom":
        return {"dx": 0.0, "dy": 1.0}
    normalized = int(round(angle / 90.0) * 90) % 360
    if normalized == 180:
        return {"dx": -1.0, "dy": 0.0}
    if normalized == 90:
        return {"dx": 0.0, "dy": 1.0}
    if normalized == 270:
        return {"dx": 0.0, "dy": -1.0}
    return {"dx": 1.0, "dy": 0.0}


def _label_direction_from_angle(angle: float, label_side: str) -> dict[str, float]:
    if label_side == "left":
        return {"dx": -1.0, "dy": 0.0}
    if label_side == "right":
        return {"dx": 1.0, "dy": 0.0}
    normalized = int(round(angle / 90.0) * 90) % 360
    if normalized == 180:
        return {"dx": -1.0, "dy": 0.0}
    return {"dx": 1.0, "dy": 0.0}


def _readable_label_angle(angle: float) -> float:
    return 0.0


def _label_placement_obstacles(schematic: KiCadSchematic) -> list[tuple[float, float, float, float]]:
    obstacles = [_label_rect(label) for label in schematic.list_labels()]
    for symbol in schematic.list_symbols():
        bounds = symbol.get("bounds", {})
        if not bounds:
            continue
        obstacles.append(
            (
                float(bounds["left"]) - 1.27,
                float(bounds["top"]) - 1.27,
                float(bounds["right"]) + 1.27,
                float(bounds["bottom"]) + 1.27,
            )
        )
    return obstacles


def _schematic_page_rect(schematic: KiCadSchematic) -> tuple[float, float, float, float]:
    bounds = schematic.get_sheet_bounds()
    margin = 2.54
    return (
        margin,
        margin,
        float(bounds["width"]) - margin,
        float(bounds["height"]) - margin,
    )


def _rect_inside(
    inner: tuple[float, float, float, float],
    outer: tuple[float, float, float, float],
) -> bool:
    return (
        inner[0] >= outer[0]
        and inner[1] >= outer[1]
        and inner[2] <= outer[2]
        and inner[3] <= outer[3]
    )


def _label_rect(label: dict[str, Any]) -> tuple[float, float, float, float]:
    position = label.get("position", {})
    return _text_rect(
        {
            "x": float(position.get("x", 0.0)),
            "y": float(position.get("y", 0.0)),
        },
        str(label.get("text") or ""),
        float(position.get("angle", 0.0)),
    )


def _text_rect(
    position: dict[str, float], text: str, angle: float
) -> tuple[float, float, float, float]:
    x = float(position.get("x", 0.0))
    y = float(position.get("y", 0.0))
    width = max(3.0, len(text) * 0.9)
    height = 2.0
    normalized = float(angle) % 360
    if normalized in {90.0, 270.0}:
        return (x - height / 2.0, y, x + height / 2.0, y + width)
    if normalized == 180.0:
        return (x - width, y - height / 2.0, x, y + height / 2.0)
    return (x, y - height / 2.0, x + width, y + height / 2.0)


def _rects_intersect(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    *,
    padding: float = 0.0,
) -> bool:
    return not (
        first[2] + padding <= second[0]
        or first[0] - padding >= second[2]
        or first[3] + padding <= second[1]
        or first[1] - padding >= second[3]
    )


def add_no_connect_to_pin(
    schematic: KiCadSchematic,
    schematic_path: str,
    reference: str,
    pin: str,
    allow_hidden_power: bool = False,
    *,
    allow_hidden_no_connect: bool = False,
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
    visibility = classify_pin(selected)
    if visibility == PinVisibility.HIDDEN_NO_CONNECT:
        return {
            "reference": reference,
            "pin": selected,
            "no_connect": None,
            "skipped": True,
            "reason": "hidden NC pin does not require a no-connect marker",
        }
    if visibility != PinVisibility.VISIBLE and not allow_hidden_no_connect:
        raise ValueError(
            f"Pin {reference}.{selected['number']} is hidden; pass "
            "allow_hidden_no_connect=True only when this hidden no-connect is intentional."
        )
    marker = schematic.add_no_connect(
        selected["connection_point"]["x"], selected["connection_point"]["y"]
    )
    return {"reference": reference, "pin": selected, "no_connect": marker}


def remove_no_connect_at_pin(
    schematic: KiCadSchematic,
    schematic_path: str,
    reference: str,
    pin: str,
) -> dict[str, Any]:
    """Remove no-connect markers located at a resolved symbol pin coordinate."""
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
    point = selected["connection_point"]
    removed = []
    for marker in list(schematic.list_no_connects()):
        marker_position = marker.get("position", {})
        if marker_position.get("x") == point["x"] and marker_position.get("y") == point["y"]:
            marker_uuid = marker.get("uuid")
            if marker_uuid is None:
                continue
            removed.append(schematic.delete_item("no_connect", marker_uuid))
    return {
        "reference": reference,
        "pin": selected,
        "removed_count": len(removed),
        "removed": removed,
    }


def remove_pin_attached_net_artifacts(
    schematic: KiCadSchematic,
    schematic_path: str,
    reference: str,
    pin: str,
    *,
    keep_net: str | None = None,
) -> dict[str, Any]:
    """Remove MCP-style labels/stubs/no-connects attached to a resolved pin."""
    selected = _resolve_single_pin(schematic, schematic_path, reference, pin)
    point = selected["connection_point"]
    removed_labels = []
    removed_wires = []
    removed_no_connects = remove_no_connect_at_pin(schematic, schematic_path, reference, pin)

    for label in list(_labels_at_point_or_stub_end(schematic, point)):
        if keep_net is not None and label.get("text") == keep_net:
            continue
        label_uuid = label.get("uuid")
        if label_uuid is None:
            continue
        removed_labels.append(
            {
                "text": label.get("text"),
                "removed": schematic.delete_item("label", label_uuid),
            }
        )

    for wire in list(_pin_stub_wires(schematic, point)):
        wire_uuid = wire.get("uuid")
        if wire_uuid is None:
            continue
        try:
            removed_wires.append(schematic.delete_item("wire", wire_uuid))
        except KeyError:
            continue

    old_nets = sorted(
        {
            str(item.get("text"))
            for item in removed_labels
            if item.get("text") and item.get("text") != keep_net
        }
    )
    return {
        "reference": reference,
        "pin": selected,
        "removed_no_connects": removed_no_connects,
        "removed_labels": removed_labels,
        "removed_wires": removed_wires,
        "old_nets": old_nets,
        "removed_count": removed_no_connects.get("removed_count", 0)
        + len(removed_labels)
        + len(removed_wires),
    }


def pin_attached_nets(
    schematic: KiCadSchematic,
    schematic_path: str,
    reference: str,
    pin: str,
) -> dict[str, Any]:
    """Return MCP-style net labels attached directly or by a short stub to a pin."""
    selected = _resolve_single_pin(schematic, schematic_path, reference, pin)
    labels = list(_labels_at_point_or_stub_end(schematic, selected["connection_point"]))
    return {
        "reference": reference,
        "pin": selected,
        "nets": sorted({str(label.get("text")) for label in labels if label.get("text")}),
        "labels": labels,
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
    resolved_pin = _resolved_pin_from_file(schematic_path, reference, pin)
    for node in net.get("nodes", []):
        if native_node_matches_endpoint(node, reference, pin, resolved_pin):
            return {"success": True, "reason": "native netlist membership verified"}
    return {
        "success": False,
        "reason": f"{reference}.{pin} was not found on net {net_name}",
        "native_netlist": native,
    }


def _resolve_single_pin(
    schematic: KiCadSchematic,
    schematic_path: str,
    reference: str,
    pin: str,
) -> dict[str, Any]:
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
    return matches[0]


def _resolved_pin_from_file(
    schematic_path: str, reference: str, pin: str
) -> dict[str, Any] | None:
    try:
        schematic = KiCadSchematic.from_file(schematic_path)
        return _resolve_single_pin(schematic, schematic_path, reference, pin)
    except Exception:
        return None


def _labels_at_point_or_stub_end(
    schematic: KiCadSchematic, point: dict[str, float]
) -> list[dict[str, Any]]:
    labels = []
    for label in schematic.list_labels():
        position = label.get("position", {})
        if _same_point(position, point):
            labels.append(label)
    for wire in _pin_stub_wires(schematic, point):
        for endpoint in wire.get("points", []):
            if _same_point(endpoint, point):
                continue
            for label in schematic.list_labels():
                if _same_point(label.get("position", {}), endpoint) and label not in labels:
                    labels.append(label)
    return labels


def _pin_stub_wires(schematic: KiCadSchematic, point: dict[str, float]) -> list[dict[str, Any]]:
    stubs = []
    for wire in schematic.list_wires():
        points = wire.get("points", [])
        if len(points) < 2:
            continue
        if any(_same_point(wire_point, point) for wire_point in points):
            stubs.append(wire)
    return stubs


def _same_point(left: dict[str, Any], right: dict[str, Any]) -> bool:
    try:
        return (
            abs(float(left.get("x")) - float(right.get("x"))) <= 1e-6
            and abs(float(left.get("y")) - float(right.get("y"))) <= 1e-6
        )
    except (TypeError, ValueError):
        return False


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


def _resolve_embedded_symbol_pins(schematic: KiCadSchematic, lib_id: str) -> list[dict[str, Any]]:
    lib_symbols = schematic.root.first_child("lib_symbols")
    if lib_symbols is None:
        return []
    for symbol in lib_symbols.child_lists("symbol"):
        if len(symbol.items) > 1 and _atom_text(symbol.items[1]) == lib_id:
            return _extract_library_pins(symbol)
    return []


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
