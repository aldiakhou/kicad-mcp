"""Helpers for inline custom schematic symbols.

These are used when a design intent provides explicit pins instead of an
installed KiCad ``lib_id``. The generated symbol is embedded in the schematic
so KiCad CLI can export a real netlist for it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from kicad_mcp.utils.kicad_s_expr import SExprAtom, SExprList

CUSTOM_LIBRARY = "kicad_mcp"
CUSTOM_PINS_PROPERTY = "KICAD_MCP_CUSTOM_PINS"

_PIN_GRID_MM = 2.54
_PIN_LENGTH_MM = 2.54
_BODY_HALF_WIDTH_MM = 7.62


def is_custom_lib_id(lib_id: str) -> bool:
    """Return True when lib_id points to an MCP-generated inline symbol."""
    return str(lib_id).startswith(f"{CUSTOM_LIBRARY}:")


def custom_lib_id(ref: str, value: str) -> str:
    """Create a stable custom lib_id from a part value/reference."""
    base = str(value or ref or "custom_ic").strip()
    cleaned = re.sub(r"[^A-Za-z0-9_+.-]+", "_", base).strip("_")
    return f"{CUSTOM_LIBRARY}:{cleaned or ref or 'custom_ic'}"


def normalize_custom_pins(raw_pins: Any, *, ref: str) -> list[dict[str, str]]:
    """Normalize user-provided custom pins.

    Accepted forms:
    - [{"number": "1", "name": "VDD", "pintype": "power_in"}]
    - [["1", "VDD", "power_in"]]
    - ["VDD", "GND"] (numbers are assigned from list order)
    """
    if not isinstance(raw_pins, list) or not raw_pins:
        raise ValueError(f"Part '{ref}' has custom pins but 'pins' is not a non-empty list")

    pins: list[dict[str, str]] = []
    seen_numbers: set[str] = set()
    for index, item in enumerate(raw_pins, start=1):
        if isinstance(item, dict):
            number = str(item.get("number") or item.get("num") or index).strip()
            name = str(item.get("name") or item.get("pin") or number).strip()
            pintype = str(item.get("pintype") or item.get("type") or "bidirectional").strip()
        elif isinstance(item, list | tuple):
            if len(item) < 2:
                raise ValueError(f"Part '{ref}' custom pin #{index} must include number and name")
            number = str(item[0]).strip()
            name = str(item[1]).strip()
            pintype = str(item[2]).strip() if len(item) >= 3 else "bidirectional"
        else:
            number = str(index)
            name = str(item).strip()
            pintype = "bidirectional"

        if not number or not name:
            raise ValueError(f"Part '{ref}' custom pin #{index} has empty number or name")
        if number in seen_numbers:
            raise ValueError(f"Part '{ref}' custom pin number '{number}' is duplicated")
        seen_numbers.add(number)
        pins.append({"number": number, "name": name, "pintype": _normalize_pin_type(pintype)})
    return pins


def encode_custom_pins(pins: list[dict[str, str]]) -> str:
    """Encode custom pins for CircuitPart metadata."""
    return json.dumps(pins, separators=(",", ":"), sort_keys=True)


def decode_custom_pins(value: str | None) -> list[dict[str, str]]:
    """Decode custom pins from CircuitPart metadata."""
    if not value:
        return []
    try:
        raw = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    pins: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        number = str(item.get("number") or "").strip()
        name = str(item.get("name") or number).strip()
        if not number or not name:
            continue
        pins.append(
            {
                "number": number,
                "name": name,
                "pintype": _normalize_pin_type(str(item.get("pintype") or "bidirectional")),
            }
        )
    return pins


def custom_pin_positions(
    pins: list[dict[str, str]],
) -> dict[str, list[tuple[float, float, float]]]:
    """Return local custom pin positions keyed by number and name."""
    positions: dict[str, list[tuple[float, float, float]]] = {}
    for pin, x, y, angle in _pin_layout(pins):
        coord = (x, y, angle)
        positions.setdefault(pin["number"], []).append(coord)
        if pin["name"] != pin["number"]:
            positions.setdefault(pin["name"], []).append(coord)
    return positions


def build_custom_symbol_node(
    lib_id: str,
    value: str,
    pins: list[dict[str, str]],
    footprint: str | None = None,
) -> SExprList:
    """Build an embedded KiCad symbol definition for a custom part."""
    symbol_name = lib_id
    raw_name = lib_id.split(":", 1)[-1]
    body_half_height = max(_PIN_GRID_MM * 2.0, (_pins_per_side(len(pins)) + 1) * _PIN_GRID_MM / 2)

    return SExprList(
        [
            SExprAtom("symbol"),
            SExprAtom(symbol_name, quoted=True),
            SExprList([SExprAtom("exclude_from_sim"), SExprAtom("no")]),
            SExprList([SExprAtom("in_bom"), SExprAtom("yes")]),
            SExprList([SExprAtom("on_board"), SExprAtom("yes")]),
            _property("Reference", "U", -_BODY_HALF_WIDTH_MM, body_half_height + 3.81),
            _property("Value", value or raw_name, _BODY_HALF_WIDTH_MM, body_half_height + 3.81),
            _property("Footprint", footprint or "", 0, -body_half_height - 3.81, hidden=True),
            _property("Datasheet", "", 0, -body_half_height - 6.35, hidden=True),
            SExprList(
                [
                    SExprAtom("symbol"),
                    SExprAtom(f"{raw_name}_0_0", quoted=True),
                    _rectangle(-_BODY_HALF_WIDTH_MM, body_half_height, _BODY_HALF_WIDTH_MM, -body_half_height),
                ]
            ),
            SExprList(
                [
                    SExprAtom("symbol"),
                    SExprAtom(f"{raw_name}_1_1", quoted=True),
                    *[_pin_node(pin, x, y, angle) for pin, x, y, angle in _pin_layout(pins)],
                ]
            ),
        ]
    )


def _pin_layout(pins: list[dict[str, str]]) -> list[tuple[dict[str, str], float, float, float]]:
    pins_per_side = _pins_per_side(len(pins))
    result: list[tuple[dict[str, str], float, float, float]] = []
    for index, pin in enumerate(pins):
        is_right = index >= pins_per_side
        side_index = index - pins_per_side if is_right else index
        y = ((pins_per_side - 1) / 2.0 - side_index) * _PIN_GRID_MM
        x = _BODY_HALF_WIDTH_MM + _PIN_LENGTH_MM if is_right else -_BODY_HALF_WIDTH_MM - _PIN_LENGTH_MM
        angle = 180.0 if is_right else 0.0
        result.append((pin, x, y, angle))
    return result


def _pins_per_side(pin_count: int) -> int:
    return max(1, (pin_count + 1) // 2)


def _normalize_pin_type(value: str) -> str:
    normalized = str(value or "bidirectional").strip().lower().replace("-", "_")
    allowed = {
        "input",
        "output",
        "bidirectional",
        "tri_state",
        "passive",
        "free",
        "unspecified",
        "power_in",
        "power_out",
        "open_collector",
        "open_emitter",
        "no_connect",
    }
    return normalized if normalized in allowed else "bidirectional"


def _property(name: str, value: str, x: float, y: float, *, hidden: bool = False) -> SExprList:
    items = [
        SExprAtom("property"),
        SExprAtom(name, quoted=True),
        SExprAtom(value, quoted=True),
        _at(x, y, 0),
        SExprList([SExprAtom("show_name"), SExprAtom("no")]),
        SExprList([SExprAtom("do_not_autoplace"), SExprAtom("no")]),
    ]
    if hidden:
        items.append(SExprList([SExprAtom("hide"), SExprAtom("yes")]))
    items.append(_effects())
    return SExprList(items)


def _pin_node(pin: dict[str, str], x: float, y: float, angle: float) -> SExprList:
    return SExprList(
        [
            SExprAtom("pin"),
            SExprAtom(_normalize_pin_type(pin.get("pintype") or "bidirectional")),
            SExprAtom("line"),
            _at(x, y, angle),
            SExprList([SExprAtom("length"), SExprAtom(_fmt(_PIN_LENGTH_MM))]),
            SExprList([SExprAtom("name"), SExprAtom(pin["name"], quoted=True), _effects()]),
            SExprList([SExprAtom("number"), SExprAtom(pin["number"], quoted=True), _effects()]),
        ]
    )


def _rectangle(x1: float, y1: float, x2: float, y2: float) -> SExprList:
    return SExprList(
        [
            SExprAtom("rectangle"),
            SExprList([SExprAtom("start"), SExprAtom(_fmt(x1)), SExprAtom(_fmt(y1))]),
            SExprList([SExprAtom("end"), SExprAtom(_fmt(x2)), SExprAtom(_fmt(y2))]),
            SExprList(
                [
                    SExprAtom("stroke"),
                    SExprList([SExprAtom("width"), SExprAtom("0.254")]),
                    SExprList([SExprAtom("type"), SExprAtom("default")]),
                ]
            ),
            SExprList([SExprAtom("fill"), SExprList([SExprAtom("type"), SExprAtom("background")])]),
        ]
    )


def _effects() -> SExprList:
    return SExprList(
        [
            SExprAtom("effects"),
            SExprList(
                [
                    SExprAtom("font"),
                    SExprList([SExprAtom("size"), SExprAtom("1.27"), SExprAtom("1.27")]),
                ]
            ),
        ]
    )


def _at(x: float, y: float, angle: float) -> SExprList:
    return SExprList(
        [
            SExprAtom("at"),
            SExprAtom(_fmt(x)),
            SExprAtom(_fmt(y)),
            SExprAtom(_fmt(angle)),
        ]
    )


def _fmt(value: float) -> str:
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text or "0"
