"""
Structured KiCad PCB S-expression editing utilities.
"""

from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
from typing import Any
import uuid

from kicad_mcp.utils.kicad_s_expr import (
    SExprAtom,
    SExprList,
    parse_s_expression,
    serialize_s_expression,
)


class KiCadPcbError(ValueError):
    """Raised when KiCad PCB parsing or editing fails."""


class KiCadPcb:
    """Structured read/write access to KiCad PCB S-expressions."""

    def __init__(self, root: SExprList):
        if root.head() != "kicad_pcb":
            raise KiCadPcbError("Root S-expression must be a kicad_pcb list")
        self.root = root

    @classmethod
    def empty(cls, board_width_mm: float = 100.0, board_height_mm: float = 80.0) -> KiCadPcb:
        """Create an empty KiCad PCB model."""
        pcb = cls(
            SExprList(
                [
                    SExprAtom("kicad_pcb"),
                    SExprList([SExprAtom("version"), SExprAtom("20240108")]),
                    SExprList([SExprAtom("generator"), SExprAtom("kicad_mcp", quoted=True)]),
                    SExprList(
                        [
                            SExprAtom("general"),
                            SExprList([SExprAtom("thickness"), SExprAtom("1.6")]),
                        ]
                    ),
                    SExprList([SExprAtom("paper"), SExprAtom("A4", quoted=True)]),
                    _layers_node(),
                    SExprList([SExprAtom("setup")]),
                    SExprList([SExprAtom("net"), SExprAtom("0"), SExprAtom("", quoted=True)]),
                ]
            )
        )
        pcb.create_board_outline(board_width_mm, board_height_mm)
        return pcb

    @classmethod
    def from_text(cls, content: str) -> KiCadPcb:
        """Create a PCB model from text."""
        return cls(parse_s_expression(content))

    @classmethod
    def from_file(cls, pcb_path: str) -> KiCadPcb:
        """Load a PCB model from disk."""
        return cls.from_text(Path(pcb_path).read_text(encoding="utf-8"))

    def to_text(self) -> str:
        """Serialize the PCB back to KiCad S-expression text."""
        return f"{serialize_s_expression(self.root)}\n"

    def add_footprint(
        self,
        footprint_id: str,
        footprint_node: SExprList,
        reference: str,
        value: str,
        x: float,
        y: float,
        angle: float = 0.0,
        net_assignments: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Add a footprint instance to the board."""
        if self.find_footprint(reference) is not None:
            raise ValueError(f"Footprint reference already exists: {reference}")
        footprint = deepcopy(footprint_node)
        footprint.items.append(SExprList([SExprAtom("uuid"), SExprAtom(str(uuid.uuid4()))]))
        self._replace_or_append_child(footprint, "at", _at_node(x, y, angle))
        self._replace_or_append_child(
            footprint, "layer", SExprList([SExprAtom("layer"), SExprAtom("F.Cu", quoted=True)])
        )
        self._set_footprint_property(footprint, "Reference", reference)
        self._set_footprint_property(footprint, "Value", value)
        self._assign_pad_nets(footprint, net_assignments or {})
        self.root.items.append(footprint)
        return {
            "reference": reference,
            "value": value,
            "footprint_id": footprint_id,
            "position": {"x": x, "y": y, "angle": angle},
            "net_assignments": net_assignments or {},
        }

    def move_footprint(
        self, reference: str, x: float, y: float, angle: float | None = None
    ) -> dict[str, Any]:
        """Move a footprint by reference."""
        footprint = self.find_footprint(reference)
        if footprint is None:
            raise KeyError(f"Footprint not found: {reference}")
        current = self._parse_at(footprint)
        new_angle = current["angle"] if angle is None else angle
        self._replace_or_append_child(footprint, "at", _at_node(x, y, new_angle))
        return {"reference": reference, "position": {"x": x, "y": y, "angle": new_angle}}

    def create_board_outline(
        self,
        width_mm: float,
        height_mm: float,
        origin_x: float = 0.0,
        origin_y: float = 0.0,
    ) -> dict[str, Any]:
        """Replace the rectangular board outline."""
        self.root.items = [
            item
            for item in self.root.items
            if not (
                isinstance(item, SExprList)
                and item.head() == "gr_rect"
                and _child_text(item, "layer") == "Edge.Cuts"
            )
        ]
        rect = SExprList(
            [
                SExprAtom("gr_rect"),
                SExprList(
                    [SExprAtom("start"), SExprAtom(_num(origin_x)), SExprAtom(_num(origin_y))]
                ),
                SExprList(
                    [
                        SExprAtom("end"),
                        SExprAtom(_num(origin_x + width_mm)),
                        SExprAtom(_num(origin_y + height_mm)),
                    ]
                ),
                SExprList(
                    [
                        SExprAtom("stroke"),
                        SExprList([SExprAtom("width"), SExprAtom("0.1")]),
                        SExprList([SExprAtom("type"), SExprAtom("solid")]),
                    ]
                ),
                SExprList([SExprAtom("fill"), SExprAtom("none")]),
                SExprList([SExprAtom("layer"), SExprAtom("Edge.Cuts", quoted=True)]),
                SExprList([SExprAtom("uuid"), SExprAtom(str(uuid.uuid4()))]),
            ]
        )
        self.root.items.append(rect)
        return {
            "width_mm": width_mm,
            "height_mm": height_mm,
            "origin": {"x": origin_x, "y": origin_y},
        }

    def add_track(
        self,
        net_name: str,
        points: list[dict[str, float]],
        layer: str = "F.Cu",
        width_mm: float = 0.25,
    ) -> dict[str, Any]:
        """Add track segments between consecutive points."""
        normalized = [_coerce_point(point) for point in points]
        if len(normalized) < 2:
            raise ValueError("A track requires at least two points")
        net_id = self.ensure_net(net_name)
        segments = []
        for start, end in zip(normalized, normalized[1:]):
            segment_uuid = str(uuid.uuid4())
            segment = SExprList(
                [
                    SExprAtom("segment"),
                    SExprList(
                        [
                            SExprAtom("start"),
                            SExprAtom(_num(start["x"])),
                            SExprAtom(_num(start["y"])),
                        ]
                    ),
                    SExprList(
                        [SExprAtom("end"), SExprAtom(_num(end["x"])), SExprAtom(_num(end["y"]))]
                    ),
                    SExprList([SExprAtom("width"), SExprAtom(_num(width_mm))]),
                    SExprList([SExprAtom("layer"), SExprAtom(layer, quoted=True)]),
                    SExprList([SExprAtom("net"), SExprAtom(str(net_id))]),
                    SExprList([SExprAtom("uuid"), SExprAtom(segment_uuid)]),
                ]
            )
            self.root.items.append(segment)
            segments.append({"uuid": segment_uuid, "start": start, "end": end})
        return {
            "net_name": net_name,
            "net_id": net_id,
            "layer": layer,
            "width_mm": width_mm,
            "segments": segments,
        }

    def add_via(
        self,
        net_name: str,
        x: float,
        y: float,
        drill_mm: float = 0.3,
        diameter_mm: float = 0.6,
    ) -> dict[str, Any]:
        """Add a through via."""
        net_id = self.ensure_net(net_name)
        via_uuid = str(uuid.uuid4())
        via = SExprList(
            [
                SExprAtom("via"),
                _at_node(x, y, None),
                SExprList([SExprAtom("size"), SExprAtom(_num(diameter_mm))]),
                SExprList([SExprAtom("drill"), SExprAtom(_num(drill_mm))]),
                SExprList(
                    [
                        SExprAtom("layers"),
                        SExprAtom("F.Cu", quoted=True),
                        SExprAtom("B.Cu", quoted=True),
                    ]
                ),
                SExprList([SExprAtom("net"), SExprAtom(str(net_id))]),
                SExprList([SExprAtom("uuid"), SExprAtom(via_uuid)]),
            ]
        )
        self.root.items.append(via)
        return {
            "uuid": via_uuid,
            "net_name": net_name,
            "net_id": net_id,
            "position": {"x": x, "y": y},
        }

    def ensure_net(self, net_name: str) -> int:
        """Ensure a board net exists and return its numeric id."""
        if not net_name:
            raise ValueError("net_name must be non-empty")
        existing = self._net_id(net_name)
        if existing is not None:
            return existing
        net_id = max(self._all_net_ids(), default=0) + 1
        self.root.items.append(
            SExprList([SExprAtom("net"), SExprAtom(str(net_id)), SExprAtom(net_name, quoted=True)])
        )
        return net_id

    def find_footprint(self, reference: str) -> SExprList | None:
        """Find a footprint by its Reference property."""
        for footprint in self._top_level("footprint"):
            if self._footprint_property_text(footprint, "Reference") == reference:
                return footprint
        return None

    def list_footprints(self) -> list[dict[str, Any]]:
        """List footprint references and positions."""
        footprints = []
        for footprint in self._top_level("footprint"):
            footprints.append(
                {
                    "reference": self._footprint_property_text(footprint, "Reference"),
                    "value": self._footprint_property_text(footprint, "Value"),
                    "footprint_name": _atom_text(footprint.items[1] if len(footprint.items) > 1 else None),
                    "position": self._parse_at(footprint),
                    "bounds": self.footprint_bounds(footprint),
                }
            )
        return footprints

    def assign_footprint_pad_nets(
        self, reference: str, net_assignments: dict[str, str], clear_stale: bool = True
    ) -> dict[str, Any]:
        """Assign net names to pads on an existing footprint."""
        footprint = self.find_footprint(reference)
        if footprint is None:
            raise KeyError(f"Footprint not found: {reference}")
        pads = footprint.child_lists("pad")
        available = {
            _atom_text(pad.items[1] if len(pad.items) > 1 else None) or ""
            for pad in pads
        }
        missing = sorted(pad for pad in net_assignments if pad not in available)
        if clear_stale:
            for pad in pads:
                pad.items = [
                    item
                    for item in pad.items
                    if not (isinstance(item, SExprList) and item.head() == "net")
                ]
        self._assign_pad_nets(footprint, net_assignments)
        return {
            "reference": reference,
            "assigned_pads": sorted(pad for pad in net_assignments if pad in available),
            "missing_pads": missing,
        }

    def list_nets(self) -> list[dict[str, Any]]:
        """List board nets."""
        nets = []
        for net in self._top_level("net"):
            if len(net.items) >= 3:
                nets.append(
                    {
                        "id": int(_atom_text(net.items[1]) or "0"),
                        "name": _atom_text(net.items[2]) or "",
                    }
                )
        return sorted(nets, key=lambda item: item["id"])

    def list_track_segments(self) -> list[dict[str, Any]]:
        """List routed track segments with resolved net names."""
        net_names = {net["id"]: net["name"] for net in self.list_nets()}
        segments = []
        for segment in self._top_level("segment"):
            start = _xy_child(segment, "start")
            end = _xy_child(segment, "end")
            if start is None or end is None:
                continue
            net_id = _child_int(segment, "net", 0)
            segments.append(
                {
                    "start": start,
                    "end": end,
                    "width_mm": _child_float(segment, "width", 0.0),
                    "layer": _child_text(segment, "layer") or "",
                    "net_id": net_id,
                    "net_name": net_names.get(net_id, ""),
                }
            )
        return segments

    def list_vias(self) -> list[dict[str, Any]]:
        """List through vias with resolved net names."""
        net_names = {net["id"]: net["name"] for net in self.list_nets()}
        vias = []
        for via in self._top_level("via"):
            position = self._parse_at(via)
            net_id = _child_int(via, "net", 0)
            vias.append(
                {
                    "position": {"x": position["x"], "y": position["y"]},
                    "diameter_mm": _child_float(via, "size", 0.0),
                    "drill_mm": _child_float(via, "drill", 0.0),
                    "net_id": net_id,
                    "net_name": net_names.get(net_id, ""),
                }
            )
        return vias

    def footprint_pad_positions(self) -> list[dict[str, Any]]:
        """Return transformed pad positions with assigned net names."""
        pads = []
        for footprint in self._top_level("footprint"):
            reference = self._footprint_property_text(footprint, "Reference") or ""
            footprint_at = self._parse_at(footprint)
            footprint_name = _atom_text(footprint.items[1] if len(footprint.items) > 1 else None) or ""
            for pad in footprint.child_lists("pad"):
                pad_number = _atom_text(pad.items[1] if len(pad.items) > 1 else None) or ""
                local = self._parse_at(pad)
                position = _transform_point(
                    local["x"],
                    local["y"],
                    footprint_at["x"],
                    footprint_at["y"],
                    footprint_at["angle"],
                )
                net_expr = pad.first_child("net")
                net_name = ""
                net_id = 0
                if net_expr is not None and len(net_expr.items) >= 3:
                    net_id = int(_atom_text(net_expr.items[1]) or "0")
                    net_name = _atom_text(net_expr.items[2]) or ""
                pads.append(
                    {
                        "reference": reference,
                        "footprint_name": footprint_name,
                        "pad": pad_number,
                        "position": position,
                        "net_id": net_id,
                        "net_name": net_name,
                    }
                )
        return pads

    def footprint_bounds(self, footprint: SExprList) -> dict[str, float]:
        """Return a coarse transformed footprint bounding box."""
        footprint_at = self._parse_at(footprint)
        points = []
        for pad in footprint.child_lists("pad"):
            local = self._parse_at(pad)
            size = pad.first_child("size")
            half_w = 1.0
            half_h = 1.0
            if size is not None and len(size.items) >= 3:
                try:
                    half_w = max(0.5, float(_atom_text(size.items[1]) or "1") / 2.0)
                    half_h = max(0.5, float(_atom_text(size.items[2]) or "1") / 2.0)
                except ValueError:
                    pass
            for dx, dy in ((-half_w, -half_h), (half_w, -half_h), (half_w, half_h), (-half_w, half_h)):
                points.append(
                    _transform_point(
                        local["x"] + dx,
                        local["y"] + dy,
                        footprint_at["x"],
                        footprint_at["y"],
                        footprint_at["angle"],
                    )
                )
        if not points:
            points = [{"x": footprint_at["x"] - 2.5, "y": footprint_at["y"] - 2.5}, {"x": footprint_at["x"] + 2.5, "y": footprint_at["y"] + 2.5}]
        return {
            "left": min(point["x"] for point in points),
            "top": min(point["y"] for point in points),
            "right": max(point["x"] for point in points),
            "bottom": max(point["y"] for point in points),
        }

    def _top_level(self, head: str) -> list[SExprList]:
        return [
            item
            for item in self.root.items[1:]
            if isinstance(item, SExprList) and item.head() == head
        ]

    def _net_id(self, net_name: str) -> int | None:
        for net in self._top_level("net"):
            if len(net.items) >= 3 and _atom_text(net.items[2]) == net_name:
                return int(_atom_text(net.items[1]) or "0")
        return None

    def _all_net_ids(self) -> list[int]:
        ids = []
        for net in self._top_level("net"):
            if len(net.items) >= 2:
                try:
                    ids.append(int(_atom_text(net.items[1]) or "0"))
                except ValueError:
                    continue
        return ids

    def _set_footprint_property(self, footprint: SExprList, property_name: str, value: str) -> None:
        for property_expr in footprint.child_lists("property"):
            if (
                len(property_expr.items) >= 3
                and _atom_text(property_expr.items[1]) == property_name
            ):
                property_expr.items[2] = SExprAtom(value, quoted=True)
                return
        footprint.items.append(
            SExprList(
                [
                    SExprAtom("property"),
                    SExprAtom(property_name, quoted=True),
                    SExprAtom(value, quoted=True),
                    _at_node(0, 0, 0),
                    SExprList([SExprAtom("layer"), SExprAtom("F.Fab", quoted=True)]),
                ]
            )
        )

    def _footprint_property_text(self, footprint: SExprList, property_name: str) -> str | None:
        for property_expr in footprint.child_lists("property"):
            if (
                len(property_expr.items) >= 3
                and _atom_text(property_expr.items[1]) == property_name
            ):
                return _atom_text(property_expr.items[2])
        return None

    def _assign_pad_nets(self, footprint: SExprList, net_assignments: dict[str, str]) -> None:
        for pad in footprint.child_lists("pad"):
            if len(pad.items) < 2:
                continue
            pad_number = _atom_text(pad.items[1])
            if pad_number not in net_assignments:
                continue
            net_name = net_assignments[pad_number]
            net_id = self.ensure_net(net_name)
            pad.items = [
                item
                for item in pad.items
                if not (isinstance(item, SExprList) and item.head() == "net")
            ]
            pad.items.append(
                SExprList(
                    [SExprAtom("net"), SExprAtom(str(net_id)), SExprAtom(net_name, quoted=True)]
                )
            )

    def _parse_at(self, expr: SExprList) -> dict[str, float]:
        at_expr = expr.first_child("at")
        if at_expr is None:
            return {"x": 0.0, "y": 0.0, "angle": 0.0}
        values = []
        for item in at_expr.items[1:4]:
            try:
                values.append(float(_atom_text(item) or "0"))
            except ValueError:
                values.append(0.0)
        while len(values) < 3:
            values.append(0.0)
        return {"x": values[0], "y": values[1], "angle": values[2]}

    def _replace_or_append_child(self, expr: SExprList, head: str, replacement: SExprList) -> None:
        for index, item in enumerate(expr.items):
            if isinstance(item, SExprList) and item.head() == head:
                expr.items[index] = replacement
                return
        expr.items.append(replacement)


def validate_pcb_text(content: str) -> dict[str, Any]:
    """Validate PCB S-expression structure from raw text."""
    pcb = KiCadPcb.from_text(content)
    return {
        "valid": True,
        "root": pcb.root.head(),
        "footprint_count": len(pcb.list_footprints()),
        "track_count": len(pcb._top_level("segment")),
        "via_count": len(pcb._top_level("via")),
    }


def _layers_node() -> SExprList:
    return SExprList(
        [
            SExprAtom("layers"),
            SExprList(
                [SExprAtom("0"), SExprAtom("F.Cu", quoted=True), SExprAtom("signal", quoted=True)]
            ),
            SExprList(
                [SExprAtom("31"), SExprAtom("B.Cu", quoted=True), SExprAtom("signal", quoted=True)]
            ),
            SExprList(
                [SExprAtom("32"), SExprAtom("B.Adhes", quoted=True), SExprAtom("user", quoted=True)]
            ),
            SExprList(
                [SExprAtom("33"), SExprAtom("F.Adhes", quoted=True), SExprAtom("user", quoted=True)]
            ),
            SExprList(
                [SExprAtom("34"), SExprAtom("B.Paste", quoted=True), SExprAtom("user", quoted=True)]
            ),
            SExprList(
                [SExprAtom("35"), SExprAtom("F.Paste", quoted=True), SExprAtom("user", quoted=True)]
            ),
            SExprList(
                [SExprAtom("36"), SExprAtom("B.SilkS", quoted=True), SExprAtom("user", quoted=True)]
            ),
            SExprList(
                [SExprAtom("37"), SExprAtom("F.SilkS", quoted=True), SExprAtom("user", quoted=True)]
            ),
            SExprList(
                [SExprAtom("38"), SExprAtom("B.Mask", quoted=True), SExprAtom("user", quoted=True)]
            ),
            SExprList(
                [SExprAtom("39"), SExprAtom("F.Mask", quoted=True), SExprAtom("user", quoted=True)]
            ),
            SExprList(
                [
                    SExprAtom("44"),
                    SExprAtom("Edge.Cuts", quoted=True),
                    SExprAtom("user", quoted=True),
                ]
            ),
        ]
    )


def _at_node(x: float, y: float, angle: float | None) -> SExprList:
    items: list[SExprAtom | SExprList] = [SExprAtom("at"), SExprAtom(_num(x)), SExprAtom(_num(y))]
    if angle is not None:
        items.append(SExprAtom(_num(angle)))
    return SExprList(items)


def _child_text(expr: SExprList, head: str) -> str | None:
    child = expr.first_child(head)
    if child is None or len(child.items) < 2:
        return None
    return _atom_text(child.items[1])


def _child_float(expr: SExprList, head: str, default: float) -> float:
    text = _child_text(expr, head)
    if text is None:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _child_int(expr: SExprList, head: str, default: int) -> int:
    text = _child_text(expr, head)
    if text is None:
        return default
    try:
        return int(text)
    except ValueError:
        return default


def _xy_child(expr: SExprList, head: str) -> dict[str, float] | None:
    child = expr.first_child(head)
    if child is None or len(child.items) < 3:
        return None
    try:
        return {
            "x": float(_atom_text(child.items[1]) or "0"),
            "y": float(_atom_text(child.items[2]) or "0"),
        }
    except ValueError:
        return None


def _atom_text(node: object | None) -> str | None:
    return node.value if isinstance(node, SExprAtom) else None


def _coerce_point(point: dict[str, float]) -> dict[str, float]:
    try:
        return {"x": float(point["x"]), "y": float(point["y"])}
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Point must contain numeric x and y values: {point}") from exc


def _transform_point(
    local_x: float, local_y: float, origin_x: float, origin_y: float, angle: float
) -> dict[str, float]:
    radians = math.radians(angle)
    return {
        "x": round(local_x * math.cos(radians) - local_y * math.sin(radians) + origin_x, 6),
        "y": round(local_x * math.sin(radians) + local_y * math.cos(radians) + origin_y, 6),
    }


def _num(value: float) -> str:
    numeric = float(value)
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:.6f}".rstrip("0").rstrip(".")
