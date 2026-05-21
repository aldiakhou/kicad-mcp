"""
Structured KiCad schematic S-expression parsing and editing utilities.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any, TypeAlias, cast
import uuid


class SExpressionError(ValueError):
    """Raised when KiCad S-expression parsing fails."""


@dataclass
class SExprAtom:
    """Single S-expression atom."""

    value: str
    quoted: bool = False

    def to_source(self) -> str:
        """Render this atom back to source."""
        if self.quoted or _needs_quotes(self.value):
            escaped = self.value.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        return self.value


@dataclass
class SExprList:
    """S-expression list node."""

    items: list[SExprNode]

    def head(self) -> str | None:
        """Return the list head when present."""
        if self.items and isinstance(self.items[0], SExprAtom):
            return self.items[0].value
        return None

    def child_lists(self, head: str | None = None) -> list[SExprList]:
        """Return child list nodes, optionally filtered by head."""
        children = [item for item in self.items if isinstance(item, SExprList)]
        if head is None:
            return children
        return [child for child in children if child.head() == head]

    def first_child(self, head: str) -> SExprList | None:
        """Return the first child list with the given head."""
        for child in self.child_lists(head):
            return child
        return None


SExprNode: TypeAlias = SExprAtom | SExprList
LABEL_OVERLAP_RESOLUTION_OFFSETS_MM = (3.0, 6.0, 9.0, 12.0, 15.0)
S_EXPRESSION_SPECIAL_CHARS = '()"'
FLOAT_COMPARISON_TOLERANCE = 1e-9
CONNECTIVITY_TOLERANCE_MM = 0.25
SYMBOL_CONNECTION_SEARCH_PADDING_MM = 2.5
# Approximate average character width heuristic for rough text bounding boxes in overlap detection.
# This is intentionally coarse and assumes typical KiCad default text sizing, not exact font metrics.
TEXT_CHAR_WIDTH_MM = 0.9
DEFAULT_SYMBOL_HALF_WIDTH_MM = 5.0
DEFAULT_SYMBOL_HALF_HEIGHT_MM = 4.0
# Treat symbol properties within slightly more than one coarse symbol width as visually attached.
BLOCK_PROPERTY_ATTACHMENT_PADDING_MM = 12.0
# Coarse half-size for including KiCad junction markers in block bounds.
JUNCTION_MARKER_HALF_SIZE_MM = 0.6
BLOCK_CONFIDENCE_HIGH_THRESHOLD = 3
BLOCK_CONFIDENCE_MEDIUM_THRESHOLD = 1
SUPPORTED_CLEANUP_LAYOUT_STYLES = {"left_to_right"}
BLOCK_LAYOUT_PRIORITY_USB = 0
BLOCK_LAYOUT_PRIORITY_POWER = 1
BLOCK_LAYOUT_PRIORITY_MCU = 2
BLOCK_LAYOUT_PRIORITY_NFC = 3
BLOCK_LAYOUT_PRIORITY_DISPLAY = 4
BLOCK_LAYOUT_PRIORITY_HEADERS = 5
BLOCK_LAYOUT_PRIORITY_OTHER = 6


@dataclass(frozen=True)
class BoundingBox:
    """Simple bounding box for overlap detection."""

    left: float
    top: float
    right: float
    bottom: float

    def intersects(self, other: BoundingBox, padding: float = 0.0) -> bool:
        """Check whether two boxes intersect."""
        return not (
            self.right + padding < other.left
            or self.left - padding > other.right
            or self.bottom + padding < other.top
            or self.top - padding > other.bottom
        )


class KiCadSchematic:
    """Structured read/write access to KiCad schematic S-expressions."""

    PAPER_SIZES_MM: dict[str, tuple[float, float]] = {
        "A5": (210.0, 148.0),
        "A4": (297.0, 210.0),
        "A3": (420.0, 297.0),
        "A2": (594.0, 420.0),
        "A1": (841.0, 594.0),
        "A0": (1189.0, 841.0),
        "USLetter": (279.4, 215.9),
        "USLegal": (355.6, 215.9),
    }

    def __init__(self, root: SExprList):
        if root.head() != "kicad_sch":
            raise SExpressionError("Root S-expression must be a kicad_sch list")
        self.root = root

    @classmethod
    def from_text(cls, content: str) -> KiCadSchematic:
        """Create a schematic model from text."""
        return cls(parse_s_expression(content))

    @classmethod
    def from_file(cls, schematic_path: str) -> KiCadSchematic:
        """Load a schematic model from disk."""
        return cls.from_text(Path(schematic_path).read_text(encoding="utf-8"))

    @classmethod
    def empty(cls, paper: str = "A4") -> KiCadSchematic:
        """Create an empty KiCad schematic model."""
        return cls(
            SExprList(
                [
                    SExprAtom("kicad_sch"),
                    SExprList([SExprAtom("version"), SExprAtom("20230121")]),
                    SExprList([SExprAtom("generator"), SExprAtom("kicad_mcp", quoted=True)]),
                    SExprList([SExprAtom("uuid"), SExprAtom(str(uuid.uuid4()))]),
                    SExprList([SExprAtom("paper"), SExprAtom(paper, quoted=True)]),
                    SExprList([SExprAtom("lib_symbols")]),
                ]
            )
        )

    def to_text(self) -> str:
        """Serialize the schematic back to KiCad S-expression text."""
        return f"{serialize_s_expression(self.root)}\n"

    def embed_lib_symbol(self, lib_id: str, symbol_node: SExprList) -> dict[str, Any]:
        """Embed a library symbol definition if it is not already present."""
        lib_symbols = self._ensure_lib_symbols()
        for existing in lib_symbols.child_lists("symbol"):
            if len(existing.items) > 1 and self._atom_text(existing.items[1], default="") == lib_id:
                return {"lib_id": lib_id, "embedded": False}
        lib_symbols.items.append(symbol_node)
        return {"lib_id": lib_id, "embedded": True}

    def add_symbol(
        self,
        lib_id: str,
        reference: str,
        value: str,
        x: float,
        y: float,
        angle: float = 0.0,
        footprint: str | None = None,
        properties: dict[str, str] | None = None,
        lib_symbol: SExprList | None = None,
    ) -> dict[str, Any]:
        """Add a schematic symbol instance."""
        if self._find_symbol_node(reference) is not None:
            raise ValueError(f"Symbol reference already exists: {reference}")
        embedded = None
        if lib_symbol is not None:
            embedded = self.embed_lib_symbol(lib_id, lib_symbol)
        symbol_uuid = str(uuid.uuid4())
        symbol = SExprList(
            [
                SExprAtom("symbol"),
                SExprList([SExprAtom("lib_id"), SExprAtom(lib_id, quoted=True)]),
                self._build_at(x, y, angle),
                SExprList([SExprAtom("unit"), SExprAtom("1")]),
                SExprList([SExprAtom("exclude_from_sim"), SExprAtom("no")]),
                SExprList([SExprAtom("in_bom"), SExprAtom("yes")]),
                SExprList([SExprAtom("on_board"), SExprAtom("yes")]),
                SExprList([SExprAtom("uuid"), SExprAtom(symbol_uuid)]),
                self._build_property("Reference", reference, x, y - 4.0),
                self._build_property("Value", value, x, y + 4.0),
                self._build_property("Footprint", footprint or "", x, y + 8.0, hidden=True),
                self._build_property("Datasheet", "", x, y + 12.0, hidden=True),
            ]
        )
        for name, property_value in (properties or {}).items():
            if name not in {"Reference", "Value", "Footprint", "Datasheet"}:
                symbol.items.append(
                    self._build_property(name, property_value, x, y + 16.0, hidden=True)
                )
        self.root.items.append(symbol)
        return {
            "reference": reference,
            "value": value,
            "lib_id": lib_id,
            "uuid": symbol_uuid,
            "position": {"x": x, "y": y, "angle": angle},
            "footprint": footprint,
            "embedded_symbol": embedded,
        }

    def add_label(
        self,
        text: str,
        x: float,
        y: float,
        label_type: str = "local",
        angle: float = 0.0,
    ) -> dict[str, Any]:
        """Add a local, global, or hierarchical schematic label."""
        head = {
            "local": "label",
            "global": "global_label",
            "hierarchical": "hierarchical_label",
        }.get(label_type)
        if head is None:
            raise ValueError("label_type must be one of: local, global, hierarchical")
        label_uuid = str(uuid.uuid4())
        label = SExprList(
            [
                SExprAtom(head),
                SExprAtom(text, quoted=True),
                self._build_at(x, y, angle),
                SExprList([SExprAtom("uuid"), SExprAtom(label_uuid)]),
            ]
        )
        if head != "label":
            label.items.insert(2, SExprList([SExprAtom("shape"), SExprAtom("input")]))
        self.root.items.append(label)
        return {
            "uuid": label_uuid,
            "type": label_type,
            "text": text,
            "position": {"x": x, "y": y, "angle": angle},
        }

    def add_wire(
        self, points: list[dict[str, float]], net_name: str | None = None
    ) -> dict[str, Any]:
        """Add a schematic wire and optionally a local label at its first point."""
        normalized_points = [_coerce_point(point) for point in points]
        if len(normalized_points) < 2:
            raise ValueError("A wire requires at least two points")
        wire_uuid = str(uuid.uuid4())
        wire = SExprList(
            [
                SExprAtom("wire"),
                SExprList(
                    [
                        SExprAtom("pts"),
                        *[self._build_xy(point["x"], point["y"]) for point in normalized_points],
                    ]
                ),
                SExprList([SExprAtom("uuid"), SExprAtom(wire_uuid)]),
            ]
        )
        self.root.items.append(wire)
        label = None
        if net_name:
            first = normalized_points[0]
            label = self.add_label(net_name, first["x"], first["y"], "local", 0.0)
        return {"uuid": wire_uuid, "points": normalized_points, "net_label": label}

    def connect_points(
        self,
        start: dict[str, float],
        end: dict[str, float],
        style: str = "orthogonal",
        net_name: str | None = None,
    ) -> dict[str, Any]:
        """Connect two schematic points with a direct or orthogonal wire."""
        start_point = _coerce_point(start)
        end_point = _coerce_point(end)
        if style == "direct":
            points = [start_point, end_point]
        elif style == "orthogonal":
            corner = {"x": end_point["x"], "y": start_point["y"]}
            points = [start_point, corner, end_point]
        else:
            raise ValueError("style must be one of: orthogonal, direct")
        return self.add_wire(points, net_name)

    def delete_item(self, item_type: str, item_id: str) -> dict[str, Any]:
        """Delete a supported top-level schematic item."""
        if item_type == "symbol":

            def predicate(item: SExprNode) -> bool:
                return (
                    isinstance(item, SExprList)
                    and item.head() == "symbol"
                    and self._symbol_reference(item) == item_id
                )
        elif item_type == "wire":

            def predicate(item: SExprNode) -> bool:
                return (
                    isinstance(item, SExprList)
                    and item.head() == "wire"
                    and self._get_uuid(item) == item_id
                )
        elif item_type == "label":

            def predicate(item: SExprNode) -> bool:
                return (
                    isinstance(item, SExprList)
                    and item.head() in {"label", "global_label", "hierarchical_label"}
                    and self._get_uuid(item) == item_id
                )
        else:
            raise ValueError("item_type must be one of: symbol, wire, label")

        for index, item in enumerate(self.root.items):
            if predicate(item):
                removed = self.root.items.pop(index)
                return {"item_type": item_type, "item_id": item_id, "removed_head": removed.head()}
        raise KeyError(f"{item_type} not found: {item_id}")

    def list_symbols(self) -> list[dict[str, Any]]:
        """Return all top-level schematic symbols."""
        return [self._symbol_to_dict(symbol) for symbol in self._top_level("symbol")]

    def list_labels(self) -> list[dict[str, Any]]:
        """Return all top-level labels."""
        labels: list[dict[str, Any]] = []
        for head in ("label", "global_label", "hierarchical_label"):
            labels.extend(self._label_to_dict(label) for label in self._top_level(head))
        return labels

    def list_wires(self) -> list[dict[str, Any]]:
        """Return all top-level wires."""
        wires: list[dict[str, Any]] = []
        for wire in self._top_level("wire"):
            points = []
            pts_expr = wire.first_child("pts")
            if pts_expr is not None:
                for point in pts_expr.child_lists("xy"):
                    coords = self._atoms_to_floats(point.items[1:3])
                    if len(coords) == 2:
                        points.append({"x": coords[0], "y": coords[1]})
            wires.append(
                {
                    "uuid": self._get_uuid(wire),
                    "points": points,
                }
            )
        return wires

    def list_junctions(self) -> list[dict[str, Any]]:
        """Return all top-level junction markers."""
        junctions: list[dict[str, Any]] = []
        for junction in self._top_level("junction"):
            xy_expr = junction.first_child("xy")
            if xy_expr is None:
                continue
            position = self._parse_xy(xy_expr)
            junctions.append({"position": position, "uuid": self._get_uuid(junction)})
        return junctions

    def get_symbol_connection_points(self, reference: str) -> list[dict[str, Any]]:
        """Return wire endpoints that appear attached to a symbol."""
        if self.get_symbol(reference) is None:
            raise KeyError(f"Symbol not found: {reference}")
        connection_points: list[dict[str, Any]] = []
        seen: set[tuple[str | None, int]] = set()
        for wire in self.find_wires_intersecting_symbol(reference):
            for endpoint in wire["endpoints"]:
                if not endpoint["inside_symbol"]:
                    continue
                key = (wire.get("uuid"), endpoint["endpoint_index"])
                if key in seen:
                    continue
                seen.add(key)
                connection_points.append(
                    {
                        "wire_uuid": wire.get("uuid"),
                        "endpoint_index": endpoint["endpoint_index"],
                        "point": endpoint["point"],
                    }
                )
        return connection_points

    def get_label_connection_point(self, label_uuid: str) -> dict[str, float]:
        """Return the anchor point of a label."""
        label = self._find_label_node(label_uuid)
        if label is None:
            raise KeyError(f"Label not found: {label_uuid}")
        position = self._parse_at(label)
        return {"x": position["x"], "y": position["y"]}

    def find_wires_touching_point(
        self, x: float, y: float, tolerance: float = CONNECTIVITY_TOLERANCE_MM
    ) -> list[dict[str, Any]]:
        """Return wires touching a point, classifying endpoint versus mid-segment contact."""
        point = (x, y)
        touching_wires: list[dict[str, Any]] = []
        for wire in self.list_wires():
            segments = _wire_segments(wire)
            touching_segments = [
                index
                for index, segment in enumerate(segments)
                if _point_on_segment(point, segment["start"], segment["end"], tolerance=tolerance)
            ]
            if not touching_segments:
                continue
            touching_endpoints = [
                index
                for index, wire_point in enumerate(wire.get("points", []))
                if math.dist(point, (wire_point["x"], wire_point["y"])) <= tolerance
            ]
            touching_wires.append(
                {
                    "uuid": wire.get("uuid"),
                    "point_count": len(wire.get("points", [])),
                    "points": [dict(point_data) for point_data in wire.get("points", [])],
                    "touching_segments": touching_segments,
                    "touching_endpoints": touching_endpoints,
                    "touch_type": "endpoint" if touching_endpoints else "segment",
                }
            )
        return touching_wires

    def find_junctions_touching_point(
        self, x: float, y: float, tolerance: float = CONNECTIVITY_TOLERANCE_MM
    ) -> list[dict[str, Any]]:
        """Return junction markers touching a point."""
        point = (x, y)
        touching_junctions: list[dict[str, Any]] = []
        for junction in self.list_junctions():
            position = junction["position"]
            if math.dist(point, (position["x"], position["y"])) <= tolerance:
                touching_junctions.append(junction)
        return touching_junctions

    def find_wires_intersecting_symbol(self, reference: str) -> list[dict[str, Any]]:
        """Return wires that intersect a symbol's coarse connection search region."""
        symbol = self.get_symbol(reference)
        if symbol is None:
            raise KeyError(f"Symbol not found: {reference}")
        symbol_box = _expand_bbox(
            self._symbol_bbox(symbol),
            SYMBOL_CONNECTION_SEARCH_PADDING_MM,
        )

        intersections: list[dict[str, Any]] = []
        for wire in self.list_wires():
            segments = _wire_segments(wire)
            intersecting_segments = [
                index
                for index, segment in enumerate(segments)
                if _segment_intersects_bbox(segment["start"], segment["end"], symbol_box)
            ]
            if not intersecting_segments:
                continue
            endpoints = []
            for endpoint_index, point in enumerate(wire.get("points", [])):
                endpoints.append(
                    {
                        "endpoint_index": endpoint_index,
                        "point": {"x": point["x"], "y": point["y"]},
                        "inside_symbol": _point_in_bbox((point["x"], point["y"]), symbol_box),
                    }
                )
            intersections.append(
                {
                    "uuid": wire.get("uuid"),
                    "point_count": len(wire.get("points", [])),
                    "points": [dict(point_data) for point_data in wire.get("points", [])],
                    "intersecting_segments": intersecting_segments,
                    "endpoints": endpoints,
                }
            )
        return intersections

    def move_wire_endpoint(
        self,
        wire_uuid: str,
        old_x: float,
        old_y: float,
        new_x: float,
        new_y: float,
    ) -> dict[str, Any]:
        """Move a single endpoint on a straight 2-point wire."""
        wire = self._find_wire_node(wire_uuid)
        if wire is None:
            raise KeyError(f"Wire not found: {wire_uuid}")

        point_nodes = self._wire_point_nodes(wire)
        if len(point_nodes) != 2:
            raise ValueError(
                f"Wire {wire_uuid} must be a straight 2-point wire for endpoint editing."
            )

        matched_index: int | None = None
        for index, point_node in enumerate(point_nodes):
            point = self._parse_xy(point_node)
            if math.dist((old_x, old_y), (point["x"], point["y"])) <= CONNECTIVITY_TOLERANCE_MM:
                matched_index = index
                self._set_xy(point_node, new_x, new_y)
                break
        if matched_index is None:
            raise ValueError(
                f"Wire {wire_uuid} has no endpoint at ({_format_number(old_x)}, {_format_number(old_y)})"
            )

        updated_wire = self._wire_to_dict(wire)
        return {
            "uuid": wire_uuid,
            "endpoint_index": matched_index,
            "old_point": {"x": old_x, "y": old_y},
            "new_point": {"x": new_x, "y": new_y},
            "points": updated_wire["points"],
        }

    def translate_wire(self, wire_uuid: str, dx: float, dy: float) -> dict[str, Any]:
        """Translate every point in a wire."""
        wire = self._find_wire_node(wire_uuid)
        if wire is None:
            raise KeyError(f"Wire not found: {wire_uuid}")
        for point_node in self._wire_point_nodes(wire):
            point = self._parse_xy(point_node)
            self._set_xy(point_node, point["x"] + dx, point["y"] + dy)
        return self._wire_to_dict(wire)

    def move_junction(
        self, old_x: float, old_y: float, new_x: float, new_y: float
    ) -> dict[str, Any]:
        """Move a junction marker identified by its current position."""
        junction = self._find_junction_node(old_x, old_y)
        if junction is None:
            raise KeyError(
                f"Junction not found at ({_format_number(old_x)}, {_format_number(old_y)})"
            )
        xy_expr = junction.first_child("xy")
        if xy_expr is None:
            raise SExpressionError("Junction is malformed: missing xy node")
        self._set_xy(xy_expr, new_x, new_y)
        return {
            "old_point": {"x": old_x, "y": old_y},
            "new_point": {"x": new_x, "y": new_y},
        }

    def get_symbol(self, reference: str) -> dict[str, Any] | None:
        """Return a single symbol by reference."""
        symbol = self._find_symbol_node(reference)
        if symbol is None:
            return None
        return self._symbol_to_dict(symbol)

    def get_sheet_bounds(self) -> dict[str, Any]:
        """Return the sheet bounds inferred from the paper definition."""
        paper_expr = self.root.first_child("paper")
        if paper_expr is None or len(paper_expr.items) < 2:
            width, height = self.PAPER_SIZES_MM["A4"]
            return {
                "paper": "A4",
                "width": width,
                "height": height,
                "origin": {"x": 0.0, "y": 0.0},
            }

        paper_name = self._atom_text(paper_expr.items[1], default="A4") or "A4"
        if paper_name == "User" and len(paper_expr.items) >= 4:
            dims = self._atoms_to_floats(paper_expr.items[2:4])
            if len(dims) == 2:
                return {
                    "paper": paper_name,
                    "width": dims[0],
                    "height": dims[1],
                    "origin": {"x": 0.0, "y": 0.0},
                }

        width, height = self.PAPER_SIZES_MM.get(paper_name, self.PAPER_SIZES_MM["A4"])
        return {
            "paper": paper_name,
            "width": width,
            "height": height,
            "origin": {"x": 0.0, "y": 0.0},
        }

    def find_overlaps(self) -> list[dict[str, Any]]:
        """Return obvious label/property overlap issues."""
        overlaps: list[dict[str, Any]] = []
        labels = self.list_labels()
        symbols = self.list_symbols()

        label_boxes = {
            label["uuid"]: self._label_bbox(label)
            for label in labels
            if label.get("uuid") is not None
        }
        property_entries: list[dict[str, Any]] = []
        symbol_boxes: dict[str, BoundingBox] = {}

        for symbol in symbols:
            reference = symbol["reference"]
            symbol_boxes[reference] = self._symbol_bbox(symbol)
            for property_name, property_data in symbol.get("properties", {}).items():
                property_entries.append(
                    {
                        "reference": reference,
                        "property_name": property_name,
                        "bbox": self._property_bbox(property_name, property_data),
                        "property": property_data,
                    }
                )

        label_values = [label for label in labels if label.get("uuid") in label_boxes]
        for index, left_label in enumerate(label_values):
            left_box = label_boxes[left_label["uuid"]]
            for right_label in label_values[index + 1 :]:
                right_box = label_boxes[right_label["uuid"]]
                if left_box.intersects(right_box, padding=0.25):
                    overlaps.append(
                        {
                            "type": "label-vs-label",
                            "objects": [left_label["uuid"], right_label["uuid"]],
                            "details": {
                                "left": left_label["text"],
                                "right": right_label["text"],
                            },
                        }
                    )

        for label in label_values:
            label_box = label_boxes[label["uuid"]]
            for symbol in symbols:
                symbol_box = symbol_boxes[symbol["reference"]]
                if label_box.intersects(symbol_box, padding=0.5):
                    overlaps.append(
                        {
                            "type": "label-vs-symbol",
                            "objects": [label["uuid"], symbol["reference"]],
                            "details": {
                                "label": label["text"],
                                "reference": symbol["reference"],
                            },
                        }
                    )
                if label_box.intersects(_expand_bbox(symbol_box, 2.5), padding=0.0):
                    overlaps.append(
                        {
                            "type": "label-vs-pin",
                            "objects": [label["uuid"], symbol["reference"]],
                            "details": {
                                "label": label["text"],
                                "reference": symbol["reference"],
                                "approximate": True,
                            },
                        }
                    )

            for property_entry in property_entries:
                if label_box.intersects(property_entry["bbox"], padding=0.25):
                    overlaps.append(
                        {
                            "type": "label-vs-property",
                            "objects": [label["uuid"], property_entry["reference"]],
                            "details": {
                                "label": label["text"],
                                "property_name": property_entry["property_name"],
                            },
                        }
                    )

        for index, left_property in enumerate(property_entries):
            for right_property in property_entries[index + 1 :]:
                if left_property["bbox"].intersects(right_property["bbox"], padding=0.25):
                    overlaps.append(
                        {
                            "type": "property-vs-property",
                            "objects": [
                                f"{left_property['reference']}:{left_property['property_name']}",
                                f"{right_property['reference']}:{right_property['property_name']}",
                            ],
                            "details": {
                                "left_reference": left_property["reference"],
                                "right_reference": right_property["reference"],
                            },
                        }
                    )

        for property_entry in property_entries:
            symbol_box = symbol_boxes[property_entry["reference"]]
            if property_entry["bbox"].intersects(symbol_box, padding=0.25):
                overlaps.append(
                    {
                        "type": "property-vs-symbol",
                        "objects": [
                            f"{property_entry['reference']}:{property_entry['property_name']}",
                            property_entry["reference"],
                        ],
                        "details": {
                            "reference": property_entry["reference"],
                            "property_name": property_entry["property_name"],
                        },
                    }
                )

        return overlaps

    def move_symbol(
        self, reference: str, x: float, y: float, angle: float | None = None
    ) -> dict[str, Any]:
        """Move a symbol instance."""
        symbol = self._find_symbol_node(reference)
        if symbol is None:
            raise KeyError(f"Symbol not found: {reference}")
        self._set_at(symbol, x, y, angle)
        return self._symbol_to_dict(symbol)

    def symbol_connectivity_risk(self, reference: str) -> dict[str, Any]:
        """Return whether moving a symbol may affect connectivity."""
        symbol = self.get_symbol(reference)
        if symbol is None:
            raise KeyError(f"Symbol not found: {reference}")

        symbol_box = _expand_bbox(self._symbol_bbox(symbol), 2.5)
        attachments: list[dict[str, Any]] = []

        for wire in self.list_wires():
            for segment in _wire_segments(wire):
                if _segment_intersects_bbox(segment["start"], segment["end"], symbol_box):
                    attachments.append(
                        {
                            "type": "wire",
                            "uuid": wire.get("uuid"),
                            "segment": segment,
                        }
                    )

        for label in self.list_labels():
            if label.get("uuid") == symbol.get("uuid"):
                continue
            label_position = label["position"]
            if _point_in_bbox((label_position["x"], label_position["y"]), symbol_box):
                attachments.append(
                    {
                        "type": "label",
                        "uuid": label.get("uuid"),
                        "label_type": label.get("type"),
                        "text": label.get("text"),
                    }
                )

        return {
            "attached": bool(attachments),
            "reference": reference,
            "attachments": attachments,
        }

    def connectivity_snapshot(self) -> dict[str, Any]:
        """Return a coarse geometry-based connectivity snapshot for symbols and labels."""
        return {
            "symbols": {
                symbol["reference"]: self.symbol_connectivity_snapshot(symbol["reference"])
                for symbol in self.list_symbols()
            },
            "labels": {
                cast(str, label["uuid"]): self.label_connectivity_snapshot(cast(str, label["uuid"]))
                for label in self.list_labels()
                if label.get("uuid") is not None
            },
        }

    def target_connectivity_snapshot(self, target_type: str, target_id: str) -> dict[str, Any]:
        """Return a geometry-based connectivity snapshot for one target."""
        if target_type == "symbol":
            return self.symbol_connectivity_snapshot(target_id)
        if target_type == "label":
            return self.label_connectivity_snapshot(target_id)
        raise ValueError(f"Unsupported connectivity snapshot target type: {target_type}")

    def symbol_connectivity_snapshot(self, reference: str) -> dict[str, Any]:
        """Return a geometry-based snapshot of wires and labels attached to a symbol."""
        symbol = self.get_symbol(reference)
        if symbol is None:
            raise KeyError(f"Symbol not found: {reference}")

        nearby_wires: set[str] = set()
        point_groups: dict[tuple[float, float], dict[str, Any]] = {}
        for wire in self.find_wires_intersecting_symbol(reference):
            wire_uuid = wire.get("uuid")
            if wire_uuid is not None:
                nearby_wires.add(wire_uuid)
            for endpoint in wire["endpoints"]:
                if not endpoint["inside_symbol"]:
                    continue
                point_key = _point_key(endpoint["point"]["x"], endpoint["point"]["y"])
                group = point_groups.setdefault(
                    point_key,
                    {
                        "x": endpoint["point"]["x"],
                        "y": endpoint["point"]["y"],
                        "wires": [],
                        "labels": [],
                    },
                )
                if wire_uuid is not None and wire_uuid not in group["wires"]:
                    group["wires"].append(wire_uuid)

        labels_by_id = {
            cast(str, label["uuid"]): label
            for label in self.list_labels()
            if label.get("uuid") is not None
        }
        nearby_labels: list[dict[str, Any]] = []
        nearby_label_ids: set[str] = set()
        for group in point_groups.values():
            for label in labels_by_id.values():
                label_position = label["position"]
                if (
                    math.dist(
                        (group["x"], group["y"]),
                        (label_position["x"], label_position["y"]),
                    )
                    > CONNECTIVITY_TOLERANCE_MM
                ):
                    continue
                label_entry = {
                    "uuid": cast(str, label["uuid"]),
                    "text": label["text"],
                }
                if label_entry["uuid"] not in nearby_label_ids:
                    nearby_label_ids.add(label_entry["uuid"])
                    nearby_labels.append(label_entry)
                if all(existing["uuid"] != label_entry["uuid"] for existing in group["labels"]):
                    group["labels"].append(label_entry)

        connection_points = sorted(
            (
                {
                    "x": group["x"],
                    "y": group["y"],
                    "wires": sorted(group["wires"]),
                    "labels": sorted(group["labels"], key=lambda entry: entry["uuid"]),
                }
                for group in point_groups.values()
            ),
            key=lambda entry: (entry["x"], entry["y"]),
        )

        return {
            "reference": reference,
            "position": symbol["position"],
            "nearby_wires": sorted(nearby_wires),
            "nearby_labels": sorted(nearby_labels, key=lambda entry: entry["uuid"]),
            "connection_points": connection_points,
        }

    def label_connectivity_snapshot(self, label_uuid: str) -> dict[str, Any]:
        """Return a geometry-based snapshot of wires touching a label."""
        label = self._find_label_node(label_uuid)
        if label is None:
            raise KeyError(f"Label not found: {label_uuid}")

        label_data = self._label_to_dict(label)
        position = label_data["position"]
        touching_wires = self.find_wires_touching_point(position["x"], position["y"])
        return {
            "uuid": label_uuid,
            "text": label_data["text"],
            "position": position,
            "touching_wires": sorted(
                {wire["uuid"] for wire in touching_wires if wire.get("uuid") is not None}
            ),
            "wire_contacts": sorted(
                [
                    {
                        "uuid": wire["uuid"],
                        "touch_type": wire["touch_type"],
                        "touching_endpoints": sorted(wire["touching_endpoints"]),
                    }
                    for wire in touching_wires
                    if wire.get("uuid") is not None
                ],
                key=lambda entry: cast(str, entry["uuid"]),
            ),
        }

    def move_symbol_with_connections(
        self,
        reference: str,
        x: float,
        y: float,
        angle: float | None = None,
    ) -> dict[str, Any]:
        """Move a symbol and any clearly attached wire endpoints."""
        plan = self._plan_symbol_move_with_connections(reference, x, y, angle)
        symbol = self.move_symbol(reference, x, y, angle)
        moved_wire_endpoints = [
            self.move_wire_endpoint(
                cast(str, endpoint["wire_uuid"]),
                endpoint["old_point"]["x"],
                endpoint["old_point"]["y"],
                endpoint["new_point"]["x"],
                endpoint["new_point"]["y"],
            )
            for endpoint in plan["moved_wire_endpoints"]
        ]
        moved_labels = [
            self.move_label(
                label["label_uuid"],
                label["new_position"]["x"],
                label["new_position"]["y"],
                label["new_position"]["angle"],
            )
            for label in plan["moved_labels"]
        ]
        return {
            "symbol": symbol,
            "moved_wire_endpoints": moved_wire_endpoints,
            "moved_labels": moved_labels,
        }

    def move_label(
        self, label_uuid: str, x: float, y: float, angle: float | None = None
    ) -> dict[str, Any]:
        """Move a label instance."""
        label = self._find_label_node(label_uuid)
        if label is None:
            raise KeyError(f"Label not found: {label_uuid}")
        self._set_at(label, x, y, angle)
        return self._label_to_dict(label)

    def label_connectivity_risk(self, label_uuid: str) -> dict[str, Any]:
        """Return whether moving a label may affect connectivity."""
        label = self._find_label_node(label_uuid)
        if label is None:
            raise KeyError(f"Label not found: {label_uuid}")

        label_data = self._label_to_dict(label)
        position = label_data["position"]
        point = (position["x"], position["y"])
        attachments: list[dict[str, Any]] = []

        for wire in self.list_wires():
            for segment in _wire_segments(wire):
                if _point_on_segment(point, segment["start"], segment["end"], tolerance=0.25):
                    attachments.append(
                        {
                            "type": "wire",
                            "uuid": wire.get("uuid"),
                            "segment": segment,
                        }
                    )

        for symbol in self.list_symbols():
            symbol_box = _expand_bbox(self._symbol_bbox(symbol), 2.5)
            if _point_in_bbox(point, symbol_box):
                attachments.append(
                    {
                        "type": "symbol",
                        "reference": symbol["reference"],
                        "uuid": symbol.get("uuid"),
                    }
                )

        return {
            "attached": bool(attachments),
            "label_uuid": label_uuid,
            "label_type": label_data.get("type"),
            "text": label_data.get("text"),
            "attachments": attachments,
        }

    def move_label_with_wire(
        self,
        label_uuid: str,
        x: float,
        y: float,
        angle: float | None = None,
    ) -> dict[str, Any]:
        """Move a label together with any clearly attached wire endpoints."""
        plan = self._plan_label_move_with_wire(label_uuid, x, y, angle)
        label = self.move_label(label_uuid, x, y, angle)
        moved_wire_endpoints = [
            self.move_wire_endpoint(
                cast(str, endpoint["wire_uuid"]),
                endpoint["old_point"]["x"],
                endpoint["old_point"]["y"],
                endpoint["new_point"]["x"],
                endpoint["new_point"]["y"],
            )
            for endpoint in plan["moved_wire_endpoints"]
        ]
        return {
            "label": label,
            "moved_wire_endpoints": moved_wire_endpoints,
        }

    def preview_connectivity_move(
        self,
        target_type: str,
        target_id: str,
        x: float,
        y: float,
        angle: float | None = None,
    ) -> dict[str, Any]:
        """Return the planned changes for a connectivity-preserving move without mutating."""
        if target_type == "symbol":
            changed_objects = self._plan_symbol_move_with_connections(target_id, x, y, angle)
        elif target_type == "label":
            changed_objects = self._plan_label_move_with_wire(target_id, x, y, angle)
        else:
            raise ValueError(f"Unsupported preview target type: {target_type}")
        return {
            "target_type": target_type,
            "target_id": target_id,
            "changed_objects": changed_objects,
        }

    def find_functional_blocks(self) -> list[dict[str, Any]]:
        """Return conservative functional block candidates."""
        return [self._public_block(block) for block in self._discover_functional_blocks()]

    def get_functional_block(self, block_id: str) -> dict[str, Any]:
        """Return one functional block candidate by block id."""
        return self._public_block(self._require_functional_block(block_id))

    def block_connectivity_snapshot(
        self,
        block_id: str | None = None,
        *,
        symbol_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return a coarse snapshot for one functional block."""
        block = (
            self._require_functional_block(block_id)
            if block_id is not None
            else self._require_functional_block_by_symbols(symbol_refs)
        )
        return self._block_connectivity_snapshot_from_block(block)

    def preview_block_move(
        self,
        block_id: str,
        dx: float,
        dy: float,
        preserve_connectivity: bool = True,
    ) -> dict[str, Any]:
        """Preview a translation-only functional block move without mutating."""
        if not preserve_connectivity:
            return {
                "success": False,
                "block_id": block_id,
                "dx": dx,
                "dy": dy,
                "planned_changes": {
                    "refusals": ["Only connectivity-preserving block moves are supported."]
                },
            }
        block = self._require_functional_block(block_id)
        plan = self._plan_block_move(block, dx, dy)
        return {
            "success": not plan["refusals"],
            "block_id": block_id,
            "dx": dx,
            "dy": dy,
            "planned_changes": self._public_block_move_plan(plan),
        }

    def move_block(
        self,
        block_id: str,
        dx: float,
        dy: float,
        preserve_connectivity: bool = True,
    ) -> dict[str, Any]:
        """Move a functional block conservatively while preserving local connectivity."""
        if not preserve_connectivity:
            raise ValueError("Only connectivity-preserving block moves are supported.")
        block = self._require_functional_block(block_id)
        plan = self._plan_block_move(block, dx, dy)
        if plan["refusals"]:
            raise ValueError("; ".join(plan["refusals"]))
        return self._apply_block_move_plan(block, plan)

    def auto_spread_blocks(
        self,
        spacing_x: float = 35.0,
        spacing_y: float = 25.0,
        preserve_connectivity: bool = True,
    ) -> dict[str, Any]:
        """Spread functional blocks using translation-only moves."""
        if not preserve_connectivity:
            raise ValueError("Only connectivity-preserving block spreading is supported.")
        preview = self.preview_auto_spread_blocks(spacing_x, spacing_y, preserve_connectivity)
        if preview["refusals"]:
            raise ValueError("; ".join(cast(list[str], preview["refusals"])))
        moved_blocks = []
        for move in cast(list[dict[str, Any]], preview["moves"]):
            block = self._require_functional_block_by_symbols(move["symbols"])
            plan = self._plan_block_move(block, move["dx"], move["dy"])
            moved_blocks.append(self._apply_block_move_plan(block, plan))
        return {"moved_blocks": moved_blocks, "refusals": []}

    def preview_auto_spread_blocks(
        self,
        spacing_x: float = 35.0,
        spacing_y: float = 25.0,
        preserve_connectivity: bool = True,
    ) -> dict[str, Any]:
        """Preview a translation-only block spread without mutating."""
        if not preserve_connectivity:
            return {
                "success": False,
                "moves": [],
                "refusals": ["Only connectivity-preserving block spreading is supported."],
            }
        blocks = self._discover_functional_blocks()
        placements = self._plan_block_spread(blocks, spacing_x, spacing_y)
        moves: list[dict[str, Any]] = []
        refusals: list[str] = []
        for placement in placements:
            if self._is_zero_translation(placement["dx"], placement["dy"]):
                continue
            block = self._require_functional_block_by_symbols(placement["symbols"])
            plan = self._plan_block_move(block, placement["dx"], placement["dy"])
            if plan["refusals"]:
                refusals.extend(
                    f"{block['block_id']}: {refusal}"
                    for refusal in cast(list[str], plan["refusals"])
                )
                continue
            moves.append(
                {
                    "block_id": block["block_id"],
                    "symbols": list(block["symbols"]),
                    "dx": placement["dx"],
                    "dy": placement["dy"],
                    "planned_changes": self._public_block_move_plan(plan),
                }
            )
        return {"success": not refusals, "moves": moves, "refusals": refusals}

    def move_symbol_property(
        self,
        reference: str,
        property_name: str,
        x: float,
        y: float,
        angle: float | None = None,
    ) -> dict[str, Any]:
        """Move a symbol property."""
        property_expr = self._find_property_node(reference, property_name)
        if property_expr is None:
            raise KeyError(f"Property '{property_name}' not found on symbol {reference}")
        self._set_at(property_expr, x, y, angle)
        symbol = self.get_symbol(reference)
        if symbol is None:
            raise KeyError(f"Symbol not found: {reference}")
        property_data = symbol["properties"][property_name]
        return cast(dict[str, Any], property_data)

    def set_property(self, reference: str, property_name: str, value: str) -> dict[str, Any]:
        """Set or create a symbol property."""
        symbol = self._find_symbol_node(reference)
        if symbol is None:
            raise KeyError(f"Symbol not found: {reference}")

        property_expr = self._find_property_node(reference, property_name)
        if property_expr is None:
            position = self._parse_at(symbol)
            property_expr = SExprList(
                [
                    SExprAtom("property"),
                    SExprAtom(property_name, quoted=True),
                    SExprAtom(value, quoted=True),
                    self._build_at(position["x"], position["y"] + 4.0, 0.0),
                ]
            )
            symbol.items.append(property_expr)
        else:
            if len(property_expr.items) < 3 or not isinstance(property_expr.items[2], SExprAtom):
                raise SExpressionError(f"Property '{property_name}' on {reference} is malformed")
            property_expr.items[2] = SExprAtom(value, quoted=True)

        updated_symbol = self.get_symbol(reference)
        if updated_symbol is None:
            raise KeyError(f"Symbol not found: {reference}")
        property_data = updated_symbol["properties"][property_name]
        return cast(dict[str, Any], property_data)

    def auto_arrange_symbol_properties(self, reference: str) -> dict[str, Any]:
        """Arrange symbol properties around the symbol origin."""
        symbol = self._find_symbol_node(reference)
        if symbol is None:
            raise KeyError(f"Symbol not found: {reference}")

        position = self._parse_at(symbol)
        ordered_properties = self._property_nodes(symbol)
        property_order = ["Reference", "Value", "Footprint", "Datasheet"]
        sorted_properties = sorted(
            ordered_properties,
            key=lambda entry: (
                property_order.index(entry["name"])
                if entry["name"] in property_order
                else len(property_order),
                entry["name"],
            ),
        )

        default_offsets = {
            "Reference": (0.0, -4.0),
            "Value": (0.0, 4.0),
            "Footprint": (0.0, 8.0),
            "Datasheet": (0.0, 12.0),
        }
        next_offset = 16.0

        for property_entry in sorted_properties:
            dx, dy = default_offsets.get(property_entry["name"], (0.0, next_offset))
            if property_entry["name"] not in default_offsets:
                next_offset += 4.0
            self._set_at(
                property_entry["node"],
                position["x"] + dx,
                position["y"] + dy,
                0.0,
            )

        arranged_symbol = self.get_symbol(reference)
        if arranged_symbol is None:
            raise KeyError(f"Symbol not found: {reference}")
        return arranged_symbol

    def preview_auto_arrange_symbol_properties_all(self) -> dict[str, Any]:
        """Plan safe symbol property arrangement for the whole schematic."""
        property_moves: list[dict[str, Any]] = []
        for symbol in self.list_symbols():
            property_moves.extend(self._plan_symbol_property_arrangement(symbol["reference"]))
        return {
            "success": True,
            "property_moves": property_moves,
            "symbols_considered": [symbol["reference"] for symbol in self.list_symbols()],
        }

    def auto_arrange_symbol_properties_all(self) -> dict[str, Any]:
        """Arrange symbol properties for all symbols using planned positions."""
        preview = self.preview_auto_arrange_symbol_properties_all()
        arranged_properties = []
        for move in cast(list[dict[str, Any]], preview["property_moves"]):
            arranged_properties.append(
                self.move_symbol_property(
                    move["reference"],
                    move["property_name"],
                    move["to"]["x"],
                    move["to"]["y"],
                    move["to"]["angle"],
                )
            )
        return {
            "symbols": sorted(
                {
                    move["reference"]
                    for move in cast(list[dict[str, Any]], preview["property_moves"])
                }
            ),
            "properties_arranged": cast(list[dict[str, Any]], preview["property_moves"]),
            "arranged_count": len(arranged_properties),
        }

    def schematic_cleanup_report(
        self,
        layout_style: str = "left_to_right",
        spacing_x: float = 35.0,
        spacing_y: float = 25.0,
        arrange_properties: bool = True,
        preserve_connectivity: bool = True,
    ) -> dict[str, Any]:
        """Return a read-only cleanup diagnosis for a schematic."""
        preview = self.preview_cleanup(
            layout_style=layout_style,
            spacing_x=spacing_x,
            spacing_y=spacing_y,
            arrange_properties=arrange_properties,
            preserve_connectivity=preserve_connectivity,
        )
        blocks = cast(list[dict[str, Any]], preview["cleanup_plan"]["blocks"])
        overlaps = self.find_overlaps()
        recommendations: list[str] = []
        if arrange_properties and preview["cleanup_plan"]["property_moves"]:
            recommendations.append("Auto-arrange symbol properties")
        if preview["cleanup_plan"]["block_moves"]:
            recommendations.append("Move blocks into left-to-right layout")
        if overlaps:
            recommendations.append("Review overlaps before applying cleanup")
        recommendations.append("Export SVG preview")
        return {
            "success": True,
            "symbols": len(self.list_symbols()),
            "labels": len(self.list_labels()),
            "wires": len(self.list_wires()),
            "blocks": blocks,
            "overlaps": overlaps,
            "unsafe_moves": preview["cleanup_plan"]["refusals"],
            "label_limitations": preview["cleanup_plan"]["label_refusals"],
            "recommendations": recommendations,
        }

    def preview_cleanup(
        self,
        layout_style: str = "left_to_right",
        spacing_x: float = 35.0,
        spacing_y: float = 25.0,
        arrange_properties: bool = True,
        preserve_connectivity: bool = True,
    ) -> dict[str, Any]:
        """Plan a full cleanup workflow without mutating the schematic."""
        plan = self._build_cleanup_plan(
            layout_style=layout_style,
            spacing_x=spacing_x,
            spacing_y=spacing_y,
            arrange_properties=arrange_properties,
            preserve_connectivity=preserve_connectivity,
        )
        cleanup_plan = self._public_cleanup_plan(plan)
        if not plan["success"]:
            return {
                "success": False,
                "error": "; ".join(cast(list[str], plan["refusals"])),
                "cleanup_plan": cleanup_plan,
            }
        return {"success": True, "cleanup_plan": cleanup_plan}

    def apply_cleanup(
        self,
        layout_style: str = "left_to_right",
        spacing_x: float = 35.0,
        spacing_y: float = 25.0,
        arrange_properties: bool = True,
        preserve_connectivity: bool = True,
    ) -> dict[str, Any]:
        """Apply a full cleanup plan to the schematic."""
        plan = self._build_cleanup_plan(
            layout_style=layout_style,
            spacing_x=spacing_x,
            spacing_y=spacing_y,
            arrange_properties=arrange_properties,
            preserve_connectivity=preserve_connectivity,
        )
        if not plan["success"]:
            raise ValueError("; ".join(cast(list[str], plan["refusals"])))
        moved_blocks = []
        for move in cast(list[dict[str, Any]], plan["safe_block_moves"]):
            block = self._require_functional_block_by_symbols(move["symbols"])
            moved_blocks.append(self._apply_block_move_plan(block, move["raw_plan"]))
        property_result = (
            self.auto_arrange_symbol_properties_all()
            if arrange_properties
            else {"symbols": [], "properties_arranged": [], "arranged_count": 0}
        )
        labels_moved = sorted(
            {label_uuid for move in moved_blocks for label_uuid in cast(list[str], move["labels"])}
        )
        return {
            "blocks_moved": [
                {
                    "block_id": move["block_id"],
                    "symbols": move["symbols"],
                    "labels": move["labels"],
                    "translated_wires": move["translated_wires"],
                    "moved_wire_endpoints": move["moved_wire_endpoints"],
                }
                for move in moved_blocks
            ],
            "properties_arranged": property_result["properties_arranged"],
            "labels_moved": labels_moved,
        }

    def auto_arrange_labels(self) -> list[dict[str, Any]]:
        """Move overlapping labels to nearby free positions."""
        moved_labels: list[dict[str, Any]] = []
        for label_data in self.list_labels():
            label_uuid = label_data.get("uuid")
            if label_uuid is None:
                continue
            label_node = self._find_label_node(label_uuid)
            if label_node is None:
                continue

            if not self._label_has_overlap(label_uuid):
                continue

            position = self._parse_at(label_node)
            moved = False
            for candidate_x, candidate_y in self._candidate_positions(position["x"], position["y"]):
                self._set_at(label_node, candidate_x, candidate_y, position["angle"])
                if not self._label_has_overlap(label_uuid):
                    moved_labels.append(self._label_to_dict(label_node))
                    moved = True
                    break
            if not moved:
                self._set_at(label_node, position["x"], position["y"], position["angle"])

        return moved_labels

    def auto_arrange_label_risks(self) -> list[dict[str, Any]]:
        """Return overlapping labels that would be moved by auto-arrange."""
        risks: list[dict[str, Any]] = []
        for label_data in self.list_labels():
            label_uuid = label_data.get("uuid")
            if label_uuid is None or not self._label_has_overlap(label_uuid):
                continue
            risk = self.label_connectivity_risk(label_uuid)
            risk["would_move"] = True
            risks.append(risk)
        return risks

    def _plan_symbol_move_with_connections(
        self,
        reference: str,
        x: float,
        y: float,
        angle: float | None = None,
    ) -> dict[str, Any]:
        symbol = self._find_symbol_node(reference)
        if symbol is None:
            raise KeyError(f"Symbol not found: {reference}")
        current_position = self._parse_at(symbol)
        requested_angle = current_position["angle"] if angle is None else angle
        dx = x - current_position["x"]
        dy = y - current_position["y"]

        wire_endpoint_moves: list[dict[str, Any]] = []
        for wire in self.find_wires_intersecting_symbol(reference):
            wire_uuid = wire.get("uuid")
            if wire_uuid is None:
                raise ValueError(
                    f"Cannot move {reference} safely: attached wire has no UUID. "
                    "Please ensure all attached wires have UUIDs before attempting this operation."
                )
            if wire["point_count"] != 2:
                raise ValueError(
                    f"Cannot move {reference} safely: wire {wire_uuid} must be a straight 2-point wire."
                )
            attached_endpoints = [
                endpoint for endpoint in wire["endpoints"] if endpoint["inside_symbol"]
            ]
            if not attached_endpoints:
                raise ValueError(
                    f"Cannot move {reference} safely: symbol has intersecting wire segments and no reliable pin map yet."
                )
            for endpoint in attached_endpoints:
                point = endpoint["point"]
                wire_endpoint_moves.append(
                    {
                        "wire_uuid": wire_uuid,
                        "endpoint_index": endpoint["endpoint_index"],
                        "old_point": {"x": point["x"], "y": point["y"]},
                        "new_point": {"x": point["x"] + dx, "y": point["y"] + dy},
                    }
                )

        if wire_endpoint_moves and not math.isclose(
            requested_angle,
            current_position["angle"],
            abs_tol=FLOAT_COMPARISON_TOLERANCE,
        ):
            raise ValueError(
                f"Cannot rotate {reference} safely with attached wires: rotation with attached wires is not supported yet. "
                "Disconnect the wires first, rotate the symbol, then reconnect them."
            )

        self._refuse_junctions_at_points(
            reference,
            "symbol",
            [endpoint["old_point"] for endpoint in wire_endpoint_moves],
        )
        moved_labels = self._plan_labels_at_points_to_translate(
            [endpoint["old_point"] for endpoint in wire_endpoint_moves],
            dx,
            dy,
        )

        return {
            "symbol": reference,
            "from_position": current_position,
            "to_position": {"x": x, "y": y, "angle": requested_angle},
            "moved_wire_endpoints": wire_endpoint_moves,
            "moved_labels": moved_labels,
        }

    def _plan_label_move_with_wire(
        self,
        label_uuid: str,
        x: float,
        y: float,
        angle: float | None = None,
    ) -> dict[str, Any]:
        label = self._find_label_node(label_uuid)
        if label is None:
            raise KeyError(f"Label not found: {label_uuid}")
        current_position = self._parse_at(label)
        requested_angle = current_position["angle"] if angle is None else angle
        touching_wires = self.find_wires_touching_point(
            current_position["x"], current_position["y"]
        )

        wire_endpoint_moves: list[dict[str, Any]] = []
        seen_endpoints: set[tuple[str | None, int]] = set()
        for wire in touching_wires:
            wire_uuid = wire.get("uuid")
            if wire_uuid is None:
                raise ValueError(
                    f"Cannot move label {label_uuid} safely: attached wire has no UUID. "
                    "Please ensure all attached wires have UUIDs before attempting this operation."
                )
            if wire["point_count"] != 2:
                raise ValueError(
                    f"Cannot move label {label_uuid} safely: wire {wire_uuid} must be a straight 2-point wire."
                )
            if not wire["touching_endpoints"]:
                raise ValueError(
                    f"Cannot move label {label_uuid} safely: label must be at a wire endpoint, not mid-segment."
                )
            for endpoint_index in wire["touching_endpoints"]:
                endpoint_key = (wire_uuid, endpoint_index)
                if endpoint_key in seen_endpoints:
                    continue
                seen_endpoints.add(endpoint_key)
                point = wire["points"][endpoint_index]
                wire_endpoint_moves.append(
                    {
                        "wire_uuid": wire_uuid,
                        "endpoint_index": endpoint_index,
                        "old_point": {"x": point["x"], "y": point["y"]},
                        "new_point": {
                            "x": point["x"] + (x - current_position["x"]),
                            "y": point["y"] + (y - current_position["y"]),
                        },
                    }
                )

        if wire_endpoint_moves and not math.isclose(
            requested_angle,
            current_position["angle"],
            abs_tol=FLOAT_COMPARISON_TOLERANCE,
        ):
            raise ValueError(
                f"Cannot rotate label {label_uuid} safely with attached wires: rotation with attached wires is not supported yet. "
                "Disconnect the wires first, rotate the label, then reconnect them."
            )

        self._refuse_junctions_at_points(
            label_uuid,
            "label",
            [endpoint["old_point"] for endpoint in wire_endpoint_moves],
        )
        return {
            "label_uuid": label_uuid,
            "from_position": current_position,
            "to_position": {"x": x, "y": y, "angle": requested_angle},
            "moved_wire_endpoints": wire_endpoint_moves,
        }

    def _plan_labels_at_points_to_translate(
        self,
        points: list[dict[str, float]],
        dx: float,
        dy: float,
    ) -> list[dict[str, Any]]:
        label_moves: list[dict[str, Any]] = []
        seen_labels: set[str] = set()
        for point in points:
            for label in self.list_labels():
                label_uuid = label.get("uuid")
                if label_uuid is None or label_uuid in seen_labels:
                    continue
                position = label["position"]
                if (
                    math.dist(
                        (point["x"], point["y"]),
                        (position["x"], position["y"]),
                    )
                    > CONNECTIVITY_TOLERANCE_MM
                ):
                    continue
                seen_labels.add(label_uuid)
                label_moves.append(
                    {
                        "label_uuid": label_uuid,
                        "old_position": position,
                        "new_position": {
                            "x": position["x"] + dx,
                            "y": position["y"] + dy,
                            "angle": position["angle"],
                        },
                    }
                )
        return label_moves

    def _refuse_junctions_at_points(
        self,
        target_id: str,
        target_type: str,
        points: list[dict[str, float]],
    ) -> None:
        for point in points:
            if self.find_junctions_touching_point(point["x"], point["y"]):
                raise ValueError(
                    f"Cannot move {target_type} {target_id} safely: connection point has a junction and junction-preserving movement is not implemented yet."
                )

    def _candidate_positions(self, x: float, y: float) -> list[tuple[float, float]]:
        """Generate nearby candidate offsets in an expanding pattern for overlap resolution."""
        offsets = []
        for step in LABEL_OVERLAP_RESOLUTION_OFFSETS_MM:
            offsets.extend(
                [
                    (step, 0.0),
                    (0.0, step),
                    (step, step),
                    (-step, 0.0),
                    (0.0, -step),
                    (-step, step),
                    (step, -step),
                    (-step, -step),
                ]
            )
        return [(x + dx, y + dy) for dx, dy in offsets]

    def _discover_functional_blocks(self) -> list[dict[str, Any]]:
        seeds: list[dict[str, Any]] = []
        for symbol in self.list_symbols():
            symbol_ref = cast(str, symbol["reference"])
            wire_claims: dict[str, set[int]] = {}
            label_ids: set[str] = set()
            junction_ids: set[str] = set()
            for wire in self.find_wires_intersecting_symbol(symbol_ref):
                wire_uuid = wire.get("uuid")
                if wire_uuid is None:
                    continue
                attached_endpoints = [
                    endpoint for endpoint in wire["endpoints"] if endpoint["inside_symbol"]
                ]
                if not attached_endpoints:
                    continue
                claimed_endpoints = wire_claims.setdefault(wire_uuid, set())
                for endpoint in attached_endpoints:
                    claimed_endpoints.add(endpoint["endpoint_index"])
                    for label in self._labels_touching_point(
                        endpoint["point"]["x"], endpoint["point"]["y"]
                    ):
                        label_uuid = label.get("uuid")
                        if label_uuid is not None:
                            label_ids.add(cast(str, label_uuid))
                    for junction in self.find_junctions_touching_point(
                        endpoint["point"]["x"], endpoint["point"]["y"]
                    ):
                        junction_ids.add(self._junction_identifier(junction))
            seeds.append(
                {
                    "symbols": {symbol_ref},
                    "symbol_properties": set(self._attached_property_ids(symbol)),
                    "labels": label_ids,
                    "junctions": junction_ids,
                    "wire_claims": wire_claims,
                }
            )

        merged_blocks = [self._copy_block_seed(seed) for seed in seeds]
        changed = True
        while changed:
            changed = False
            new_blocks: list[dict[str, Any]] = []
            while merged_blocks:
                current = merged_blocks.pop(0)
                index = 0
                while index < len(merged_blocks):
                    other = merged_blocks[index]
                    if self._blocks_should_merge(current, other):
                        current = self._merge_block_members(current, other)
                        merged_blocks.pop(index)
                        changed = True
                        continue
                    index += 1
                new_blocks.append(current)
            merged_blocks = new_blocks

        enriched_blocks: list[dict[str, Any]] = []
        for block in merged_blocks:
            for symbol_ref in cast(set[str], block["symbols"]):
                symbol_data = self.get_symbol(symbol_ref)
                if symbol_data is None:
                    continue
                cast(set[str], block["symbol_properties"]).update(
                    self._attached_property_ids(symbol_data)
                )
            for wire_uuid, claimed_indices in cast(
                dict[str, set[int]], block["wire_claims"]
            ).items():
                wire_data = self._get_wire_by_uuid(wire_uuid)
                if wire_data is None:
                    continue
                endpoint_indices = set(claimed_indices)
                for endpoint_index in endpoint_indices:
                    point = wire_data["points"][endpoint_index]
                    for label in self._labels_touching_point(point["x"], point["y"]):
                        label_uuid = label.get("uuid")
                        if label_uuid is not None:
                            cast(set[str], block["labels"]).add(cast(str, label_uuid))
                    for junction in self.find_junctions_touching_point(point["x"], point["y"]):
                        cast(set[str], block["junctions"]).add(self._junction_identifier(junction))
            enriched_blocks.append(self._finalize_block(block))

        sorted_blocks = sorted(
            enriched_blocks,
            key=lambda block: (
                block["bounds"]["top"],
                block["bounds"]["left"],
                tuple(block["symbols"]),
            ),
        )
        for index, block in enumerate(sorted_blocks, start=1):
            block["block_id"] = f"block_{index:03d}"
        return sorted_blocks

    def _finalize_block(self, block: dict[str, Any]) -> dict[str, Any]:
        symbol_refs = sorted(cast(set[str], block["symbols"]))
        label_ids = sorted(cast(set[str], block["labels"]))
        property_ids = sorted(cast(set[str], block["symbol_properties"]))
        junction_ids = sorted(cast(set[str], block["junctions"]))
        wire_claims = {
            wire_uuid: sorted(endpoint_indices)
            for wire_uuid, endpoint_indices in sorted(
                cast(dict[str, set[int]], block["wire_claims"]).items()
            )
        }
        wire_ids = sorted(wire_claims)
        finalized = {
            "symbols": symbol_refs,
            "labels": label_ids,
            "symbol_properties": property_ids,
            "junctions": junction_ids,
            "wire_claims": wire_claims,
            "wires": wire_ids,
        }
        finalized["bounds"] = self._block_bounds(finalized)
        finalized["external_connections"] = self._block_external_connections(finalized)
        finalized["name_hint"], finalized["confidence"] = self._classify_block(finalized)
        return finalized

    def _public_block(self, block: dict[str, Any]) -> dict[str, Any]:
        return {
            "block_id": block["block_id"],
            "name_hint": block["name_hint"],
            "bounds": block["bounds"],
            "symbols": block["symbols"],
            "symbol_properties": block["symbol_properties"],
            "labels": block["labels"],
            "wires": block["wires"],
            "junctions": block["junctions"],
            "external_connections": block["external_connections"],
            "confidence": block["confidence"],
        }

    def _plan_block_move(self, block: dict[str, Any], dx: float, dy: float) -> dict[str, Any]:
        plan: dict[str, Any] = {
            "block_id": block["block_id"],
            "dx": dx,
            "dy": dy,
            "symbols": list(block["symbols"]),
            "labels": list(block["labels"]),
            "symbol_properties": list(block["symbol_properties"]),
            "translated_junctions": list(block["junctions"]),
            "translated_wires": [],
            "moved_wire_endpoints": [],
            "refusals": [],
        }
        for wire_uuid in cast(list[str], block["wires"]):
            wire = self._get_wire_by_uuid(wire_uuid)
            if wire is None:
                cast(list[str], plan["refusals"]).append(
                    f"Wire {wire_uuid} is missing from the schematic."
                )
                continue
            point_count = len(wire["points"])
            claimed_indices = set(cast(list[int], block["wire_claims"].get(wire_uuid, [])))
            if self._wire_fully_claimed(wire, claimed_indices):
                cast(list[str], plan["translated_wires"]).append(wire_uuid)
                continue
            if len(claimed_indices) != 1:
                cast(list[str], plan["refusals"]).append(
                    f"Cannot move block {block['block_id']} safely: wire {wire_uuid} has ambiguous block attachment."
                )
                continue
            if point_count != 2:
                cast(list[str], plan["refusals"]).append(
                    f"Cannot move block {block['block_id']} safely: boundary wire {wire_uuid} must be a straight 2-point wire."
                )
                continue
            inside_endpoint = next(iter(claimed_indices))
            outside_endpoint = 1 - inside_endpoint
            inside_point = wire["points"][inside_endpoint]
            outside_point = wire["points"][outside_endpoint]
            if self._wire_touches_junction_at_points([inside_point, outside_point]):
                cast(list[str], plan["refusals"]).append(
                    f"Cannot move block {block['block_id']} safely: boundary wire {wire_uuid} touches a junction."
                )
                continue
            cast(list[dict[str, Any]], plan["moved_wire_endpoints"]).append(
                {
                    "wire_uuid": wire_uuid,
                    "endpoint_index": inside_endpoint,
                    "old_point": dict(inside_point),
                    "new_point": {"x": inside_point["x"] + dx, "y": inside_point["y"] + dy},
                }
            )
        return plan

    def _public_block_move_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "symbols": plan["symbols"],
            "labels": plan["labels"],
            "translated_wires": plan["translated_wires"],
            "moved_wire_endpoints": [
                f"{endpoint['wire_uuid']}:{endpoint['endpoint_index']}"
                for endpoint in cast(list[dict[str, Any]], plan["moved_wire_endpoints"])
            ],
            "refusals": plan["refusals"],
        }

    def _apply_block_move_plan(self, block: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
        moved_symbols = []
        for symbol_ref in cast(list[str], block["symbols"]):
            symbol = self.get_symbol(symbol_ref)
            if symbol is None:
                raise KeyError(f"Symbol not found: {symbol_ref}")
            moved_symbols.append(
                self.move_symbol(
                    symbol_ref,
                    symbol["position"]["x"] + plan["dx"],
                    symbol["position"]["y"] + plan["dy"],
                    symbol["position"]["angle"],
                )
            )

        moved_properties = []
        for property_id in cast(list[str], block["symbol_properties"]):
            reference, property_name = property_id.split(":", maxsplit=1)
            symbol = self.get_symbol(reference)
            if symbol is None:
                raise KeyError(f"Symbol not found: {reference}")
            property_data = cast(dict[str, Any], symbol["properties"][property_name])
            moved_properties.append(
                self.move_symbol_property(
                    reference,
                    property_name,
                    property_data["position"]["x"] + plan["dx"],
                    property_data["position"]["y"] + plan["dy"],
                    property_data["position"]["angle"],
                )
            )

        moved_labels = []
        for label_uuid in cast(list[str], block["labels"]):
            label = self._find_label_node(label_uuid)
            if label is None:
                raise KeyError(f"Label not found: {label_uuid}")
            current_position = self._parse_at(label)
            moved_labels.append(
                self.move_label(
                    label_uuid,
                    current_position["x"] + plan["dx"],
                    current_position["y"] + plan["dy"],
                    current_position["angle"],
                )
            )

        translated_wires = [
            self.translate_wire(wire_uuid, plan["dx"], plan["dy"])
            for wire_uuid in plan["translated_wires"]
        ]
        moved_wire_endpoints = [
            self.move_wire_endpoint(
                endpoint["wire_uuid"],
                endpoint["old_point"]["x"],
                endpoint["old_point"]["y"],
                endpoint["new_point"]["x"],
                endpoint["new_point"]["y"],
            )
            for endpoint in cast(list[dict[str, Any]], plan["moved_wire_endpoints"])
        ]

        moved_junctions = []
        for junction_id in cast(list[str], block["junctions"]):
            junction = self._get_junction_by_identifier(junction_id)
            if junction is None:
                continue
            position = junction["position"]
            moved_junctions.append(
                self.move_junction(
                    position["x"],
                    position["y"],
                    position["x"] + plan["dx"],
                    position["y"] + plan["dy"],
                )
            )

        return {
            "block_id": block["block_id"],
            "symbols": [symbol["reference"] for symbol in moved_symbols],
            "labels": [label["uuid"] for label in moved_labels],
            "symbol_properties": list(block["symbol_properties"]),
            "translated_wires": [wire["uuid"] for wire in translated_wires],
            "moved_wire_endpoints": [
                f"{endpoint['uuid']}:{endpoint['endpoint_index']}"
                for endpoint in moved_wire_endpoints
            ],
            "translated_junctions": list(block["junctions"]),
        }

    def _plan_block_spread(
        self,
        blocks: list[dict[str, Any]],
        spacing_x: float,
        spacing_y: float,
        layout_style: str | None = None,
    ) -> list[dict[str, Any]]:
        sorted_blocks = self._sort_blocks_for_layout(blocks, layout_style)
        placements: list[dict[str, Any]] = []
        current_row_top = 0.0
        current_row_bottom = 0.0
        next_left = 0.0
        row_started = False
        for block in sorted_blocks:
            bounds = block["bounds"]
            width = bounds["right"] - bounds["left"]
            height = bounds["bottom"] - bounds["top"]
            if not row_started:
                current_row_top = bounds["top"]
                current_row_bottom = bounds["bottom"]
                next_left = bounds["left"]
                row_started = True
            elif bounds["top"] > current_row_bottom + spacing_y:
                current_row_top = current_row_bottom + spacing_y
                current_row_bottom = current_row_top + height
                next_left = bounds["left"]
            target_left = next_left
            target_top = current_row_top
            placements.append(
                {
                    "block_id": block["block_id"],
                    "symbols": list(block["symbols"]),
                    "dx": target_left - bounds["left"],
                    "dy": target_top - bounds["top"],
                }
            )
            next_left = target_left + width + spacing_x
            current_row_bottom = max(current_row_bottom, target_top + height)
        return placements

    def _build_cleanup_plan(
        self,
        *,
        layout_style: str,
        spacing_x: float,
        spacing_y: float,
        arrange_properties: bool,
        preserve_connectivity: bool,
    ) -> dict[str, Any]:
        blocks = self._discover_functional_blocks()
        overlaps = self.find_overlaps()
        refusals: list[str] = []
        if layout_style not in SUPPORTED_CLEANUP_LAYOUT_STYLES:
            refusals.append(f"Unsupported layout_style: {layout_style}")
        if not preserve_connectivity:
            refusals.append("Only connectivity-preserving cleanup is supported.")
        placements = (
            self._plan_block_spread(blocks, spacing_x, spacing_y, layout_style=layout_style)
            if not refusals
            else []
        )
        block_entries: list[dict[str, Any]] = []
        safe_block_moves: list[dict[str, Any]] = []
        for placement in placements:
            block = self._require_functional_block_by_symbols(placement["symbols"])
            if self._is_zero_translation(placement["dx"], placement["dy"]):
                block_entries.append(
                    {
                        "block_id": block["block_id"],
                        "name_hint": block["name_hint"],
                        "symbols": list(block["symbols"]),
                        "bounds": block["bounds"],
                        "dx": placement["dx"],
                        "dy": placement["dy"],
                        "safe_to_move": True,
                        "refusals": [],
                    }
                )
                continue
            raw_plan = self._plan_block_move(block, placement["dx"], placement["dy"])
            block_entry = {
                "block_id": block["block_id"],
                "name_hint": block["name_hint"],
                "symbols": list(block["symbols"]),
                "bounds": block["bounds"],
                "dx": placement["dx"],
                "dy": placement["dy"],
                "safe_to_move": not raw_plan["refusals"],
                "refusals": list(raw_plan["refusals"]),
            }
            block_entries.append(block_entry)
            if raw_plan["refusals"]:
                refusals.extend(
                    f"{block['block_id']}: {refusal}" for refusal in raw_plan["refusals"]
                )
                continue
            safe_block_moves.append(
                {
                    "block_id": block["block_id"],
                    "name_hint": block["name_hint"],
                    "symbols": list(block["symbols"]),
                    "dx": placement["dx"],
                    "dy": placement["dy"],
                    "raw_plan": raw_plan,
                    "public_plan": self._public_block_move_plan(raw_plan),
                }
            )

        preview_model = KiCadSchematic.from_text(self.to_text())
        for move in safe_block_moves:
            block = preview_model._require_functional_block_by_symbols(move["symbols"])
            preview_model._apply_block_move_plan(block, move["raw_plan"])
        property_preview = (
            preview_model.preview_auto_arrange_symbol_properties_all()
            if arrange_properties
            else {"success": True, "property_moves": [], "symbols_considered": []}
        )
        if arrange_properties:
            preview_model.auto_arrange_symbol_properties_all()
        projected_overlaps = preview_model.find_overlaps()
        label_refusals = self._cleanup_label_refusals(overlaps, safe_block_moves)
        requires_user_review = bool(
            overlaps
            or projected_overlaps
            or property_preview["property_moves"]
            or safe_block_moves
            or label_refusals
        )
        return {
            "success": not refusals,
            "layout_style": layout_style,
            "spacing_x": spacing_x,
            "spacing_y": spacing_y,
            "arrange_properties": arrange_properties,
            "blocks": block_entries,
            "safe_block_moves": safe_block_moves,
            "property_preview": property_preview,
            "refusals": refusals,
            "label_refusals": label_refusals,
            "overlaps": overlaps,
            "projected_overlaps": projected_overlaps,
            "requires_user_review": requires_user_review,
        }

    def _public_cleanup_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "layout_style": plan["layout_style"],
            "spacing_x": plan["spacing_x"],
            "spacing_y": plan["spacing_y"],
            "blocks": plan["blocks"],
            "block_moves": [
                {
                    "block_id": move["block_id"],
                    "name_hint": move["name_hint"],
                    "symbols": move["symbols"],
                    "dx": move["dx"],
                    "dy": move["dy"],
                    "planned_changes": move["public_plan"],
                }
                for move in cast(list[dict[str, Any]], plan["safe_block_moves"])
            ],
            "property_moves": plan["property_preview"]["property_moves"],
            "refusals": plan["refusals"],
            "label_refusals": plan["label_refusals"],
            "overlaps": plan["overlaps"],
            "projected_overlaps": plan["projected_overlaps"],
            "requires_user_review": plan["requires_user_review"],
        }

    def _block_connectivity_snapshot_from_block(self, block: dict[str, Any]) -> dict[str, Any]:
        boundary_wires = []
        for wire_uuid, claimed_indices in block["wire_claims"].items():
            wire = self._get_wire_by_uuid(wire_uuid)
            if wire is None:
                continue
            claimed_set = set(claimed_indices)
            if self._wire_fully_claimed(wire, claimed_set):
                continue
            boundary_wires.append(wire_uuid)
        label_texts = []
        for label_uuid in block["labels"]:
            label = self._find_label_node(label_uuid)
            if label is None:
                continue
            label_texts.append(self._label_to_dict(label)["text"])
        return {
            "block_id": block["block_id"],
            "internal_symbols": list(block["symbols"]),
            "external_connections": list(block["external_connections"]),
            "boundary_wire_count": len(boundary_wires),
            "labels": list(block["labels"]),
            "wires": list(block["wires"]),
            "label_texts": sorted(label_texts),
        }

    def _require_functional_block(self, block_id: str) -> dict[str, Any]:
        for block in self._discover_functional_blocks():
            if block["block_id"] == block_id:
                return block
        raise KeyError(f"Functional block not found: {block_id}")

    def _require_functional_block_by_symbols(self, symbol_refs: list[str] | None) -> dict[str, Any]:
        if not symbol_refs:
            raise KeyError("Functional block symbol set not provided.")
        symbol_set = sorted(symbol_refs)
        for block in self._discover_functional_blocks():
            if block["symbols"] == symbol_set:
                return block
        raise KeyError(f"Functional block not found for symbols: {', '.join(symbol_set)}")

    def _copy_block_seed(self, seed: dict[str, Any]) -> dict[str, Any]:
        return {
            "symbols": set(cast(set[str], seed["symbols"])),
            "symbol_properties": set(cast(set[str], seed["symbol_properties"])),
            "labels": set(cast(set[str], seed["labels"])),
            "junctions": set(cast(set[str], seed["junctions"])),
            "wire_claims": {
                wire_uuid: set(endpoint_indices)
                for wire_uuid, endpoint_indices in cast(
                    dict[str, set[int]], seed["wire_claims"]
                ).items()
            },
        }

    def _blocks_should_merge(self, left: dict[str, Any], right: dict[str, Any]) -> bool:
        return bool(
            set(cast(dict[str, Any], left["wire_claims"])).intersection(
                cast(dict[str, Any], right["wire_claims"])
            )
            or cast(set[str], left["labels"]).intersection(cast(set[str], right["labels"]))
            or cast(set[str], left["junctions"]).intersection(cast(set[str], right["junctions"]))
        )

    def _merge_block_members(self, left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
        merged = self._copy_block_seed(left)
        cast(set[str], merged["symbols"]).update(cast(set[str], right["symbols"]))
        cast(set[str], merged["symbol_properties"]).update(
            cast(set[str], right["symbol_properties"])
        )
        cast(set[str], merged["labels"]).update(cast(set[str], right["labels"]))
        cast(set[str], merged["junctions"]).update(cast(set[str], right["junctions"]))
        for wire_uuid, endpoint_indices in cast(dict[str, set[int]], right["wire_claims"]).items():
            cast(dict[str, set[int]], merged["wire_claims"]).setdefault(wire_uuid, set()).update(
                endpoint_indices
            )
        return merged

    def _labels_touching_point(self, x: float, y: float) -> list[dict[str, Any]]:
        return [
            label
            for label in self.list_labels()
            if math.dist((x, y), (label["position"]["x"], label["position"]["y"]))
            <= CONNECTIVITY_TOLERANCE_MM
        ]

    def _wire_touches_junction_at_points(self, points: list[dict[str, float]]) -> bool:
        return any(self.find_junctions_touching_point(point["x"], point["y"]) for point in points)

    def _is_zero_translation(self, dx: float, dy: float) -> bool:
        return math.isclose(dx, 0.0, abs_tol=FLOAT_COMPARISON_TOLERANCE) and math.isclose(
            dy, 0.0, abs_tol=FLOAT_COMPARISON_TOLERANCE
        )

    def _wire_fully_claimed(self, wire: dict[str, Any], claimed_indices: set[int]) -> bool:
        point_count = len(wire["points"])
        return point_count >= 2 and 0 in claimed_indices and point_count - 1 in claimed_indices

    def _attached_property_ids(self, symbol: dict[str, Any]) -> list[str]:
        symbol_box = self._symbol_bbox(symbol)
        attached_property_ids = []
        for property_name, property_data in symbol.get("properties", {}).items():
            if self._property_bbox(property_name, property_data).intersects(
                _expand_bbox(symbol_box, BLOCK_PROPERTY_ATTACHMENT_PADDING_MM)
            ):
                attached_property_ids.append(f"{symbol['reference']}:{property_name}")
        return attached_property_ids

    def _get_wire_by_uuid(self, wire_uuid: str) -> dict[str, Any] | None:
        for wire in self.list_wires():
            if wire.get("uuid") == wire_uuid:
                return wire
        return None

    def _junction_identifier(self, junction: dict[str, Any]) -> str:
        junction_uuid = junction.get("uuid")
        if junction_uuid is not None:
            return cast(str, junction_uuid)
        position = junction["position"]
        return f"junction@{_format_number(position['x'])},{_format_number(position['y'])}"

    def _get_junction_by_identifier(self, junction_id: str) -> dict[str, Any] | None:
        for junction in self.list_junctions():
            if self._junction_identifier(junction) == junction_id:
                return junction
        return None

    def _block_bounds(self, block: dict[str, Any]) -> dict[str, float]:
        boxes: list[BoundingBox] = []
        for symbol_ref in cast(list[str], block["symbols"]):
            symbol = self.get_symbol(symbol_ref)
            if symbol is not None:
                boxes.append(self._symbol_bbox(symbol))
        for property_id in cast(list[str], block["symbol_properties"]):
            reference, property_name = property_id.split(":", maxsplit=1)
            symbol = self.get_symbol(reference)
            if symbol is None or property_name not in symbol["properties"]:
                continue
            boxes.append(
                self._property_bbox(
                    property_name, cast(dict[str, Any], symbol["properties"][property_name])
                )
            )
        for label_uuid in cast(list[str], block["labels"]):
            label = self._find_label_node(label_uuid)
            if label is not None:
                boxes.append(self._label_bbox(self._label_to_dict(label)))
        for wire_uuid in cast(list[str], block["wires"]):
            wire = self._get_wire_by_uuid(wire_uuid)
            if wire is None or not wire["points"]:
                continue
            xs = [point["x"] for point in wire["points"]]
            ys = [point["y"] for point in wire["points"]]
            boxes.append(BoundingBox(left=min(xs), top=min(ys), right=max(xs), bottom=max(ys)))
        for junction_id in cast(list[str], block["junctions"]):
            junction = self._get_junction_by_identifier(junction_id)
            if junction is None:
                continue
            position = junction["position"]
            boxes.append(
                BoundingBox(
                    left=position["x"] - JUNCTION_MARKER_HALF_SIZE_MM,
                    top=position["y"] - JUNCTION_MARKER_HALF_SIZE_MM,
                    right=position["x"] + JUNCTION_MARKER_HALF_SIZE_MM,
                    bottom=position["y"] + JUNCTION_MARKER_HALF_SIZE_MM,
                )
            )
        if not boxes:
            return self._bbox_to_dict(BoundingBox(0.0, 0.0, 0.0, 0.0))
        return self._bbox_to_dict(
            BoundingBox(
                left=min(box.left for box in boxes),
                top=min(box.top for box in boxes),
                right=max(box.right for box in boxes),
                bottom=max(box.bottom for box in boxes),
            )
        )

    def _block_external_connections(self, block: dict[str, Any]) -> list[str]:
        connections: set[str] = set()
        for wire_uuid, claimed_indices in block["wire_claims"].items():
            wire = self._get_wire_by_uuid(wire_uuid)
            if wire is None or len(wire["points"]) < 2:
                continue
            claimed_set = set(claimed_indices)
            if self._wire_fully_claimed(wire, claimed_set):
                continue
            if len(claimed_set) != 1 or len(wire["points"]) != 2:
                continue
            outside_index = 1 - next(iter(claimed_set))
            outside_point = wire["points"][outside_index]
            for label in self._labels_touching_point(outside_point["x"], outside_point["y"]):
                label_text = label.get("text")
                if label_text:
                    connections.add(cast(str, label_text))
        return sorted(connections)

    def _classify_block(self, block: dict[str, Any]) -> tuple[str, str]:
        symbols = [self.get_symbol(reference) for reference in cast(list[str], block["symbols"])]
        refs = [cast(str, symbol["reference"]) for symbol in symbols if symbol is not None]
        values = [cast(str, symbol["value"]) for symbol in symbols if symbol is not None]
        label_texts = []
        for label_uuid in cast(list[str], block["labels"]):
            label = self._find_label_node(label_uuid)
            if label is not None:
                label_texts.append(cast(str, self._label_to_dict(label)["text"]))
        haystack = " ".join(refs + values + label_texts).upper()
        rules = [
            (
                "USB-C / Connector block",
                ("USB", "USB_C", "TYPE-C", "VBUS", "USB_D+", "USB_D-", "CC1", "CC2"),
            ),
            (
                "MCU block",
                ("ESP32", "MCU", "MICROCONTROLLER", "GPIO", "EN", "BOOT", "TX", "RX", "SDA", "SCL"),
            ),
            ("NFC block", ("PN532", "NFC", "RFID", "ANT", "IRQ")),
            ("Display block", ("LCD", "OLED", "DISPLAY", "RS", "D4", "D5", "D6", "D7")),
            ("Power block", ("LDO", "REGULATOR", "BUCK", "5V", "3V3", "VIN", "VOUT", "GND")),
        ]
        best_name = "Functional block"
        best_score = 0
        for name, terms in rules:
            score = sum(1 for term in terms if term in haystack)
            if name == "USB-C / Connector block" and any(
                reference.startswith("J") for reference in refs
            ):
                score += 1
            if name == "MCU block" and any(reference.startswith("U") for reference in refs):
                score += 1
            if score > best_score:
                best_name = name
                best_score = score
        confidence = (
            "high"
            if best_score >= BLOCK_CONFIDENCE_HIGH_THRESHOLD
            else "medium"
            if best_score >= BLOCK_CONFIDENCE_MEDIUM_THRESHOLD
            else "low"
        )
        return best_name, confidence

    def _sort_blocks_for_layout(
        self, blocks: list[dict[str, Any]], layout_style: str | None
    ) -> list[dict[str, Any]]:
        if layout_style != "left_to_right":
            return sorted(blocks, key=self._default_block_sort_key)
        return sorted(
            blocks,
            key=lambda block: (
                self._block_layout_priority(block),
                *self._default_block_sort_key(block),
            ),
        )

    def _block_layout_priority(self, block: dict[str, Any]) -> int:
        name_hint = cast(str, block["name_hint"])
        refs = cast(list[str], block["symbols"])
        label_texts = []
        for label_uuid in cast(list[str], block["labels"]):
            label = self._find_label_node(label_uuid)
            if label is not None:
                label_texts.append(cast(str, self._label_to_dict(label)["text"]).upper())
        haystack = " ".join(refs + label_texts).upper()
        if name_hint == "USB-C / Connector block":
            return BLOCK_LAYOUT_PRIORITY_USB
        if name_hint == "Power block":
            return BLOCK_LAYOUT_PRIORITY_POWER
        if name_hint == "MCU block":
            return BLOCK_LAYOUT_PRIORITY_MCU
        if name_hint == "NFC block":
            return BLOCK_LAYOUT_PRIORITY_NFC
        if name_hint == "Display block":
            return BLOCK_LAYOUT_PRIORITY_DISPLAY
        if any(reference.startswith(("J", "P", "H")) for reference in refs) or any(
            term in haystack for term in ("HEADER", "DEBUG", "SWD", "UART", "EXP")
        ):
            return BLOCK_LAYOUT_PRIORITY_HEADERS
        return BLOCK_LAYOUT_PRIORITY_OTHER

    def _default_block_sort_key(
        self, block: dict[str, Any]
    ) -> tuple[float, float, tuple[str, ...]]:
        return (block["bounds"]["top"], block["bounds"]["left"], tuple(block["symbols"]))

    def _plan_symbol_property_arrangement(self, reference: str) -> list[dict[str, Any]]:
        symbol = self.get_symbol(reference)
        if symbol is None:
            raise KeyError(f"Symbol not found: {reference}")
        planned_boxes = [
            self._property_bbox(name, cast(dict[str, Any], property_data))
            for name, property_data in symbol["properties"].items()
        ]
        symbol_boxes = [
            self._symbol_bbox(other_symbol)
            for other_symbol in self.list_symbols()
            if other_symbol["reference"] != reference
        ]
        moves: list[dict[str, Any]] = []
        for property_name, property_data in symbol["properties"].items():
            current = cast(dict[str, Any], property_data)
            current_box = self._property_bbox(property_name, current)
            if current_box in planned_boxes:
                planned_boxes.remove(current_box)
            target = self._choose_property_position(
                reference, property_name, current, symbol_boxes, planned_boxes
            )
            planned_boxes.append(
                self._property_bbox(
                    property_name, {"text": current.get("text"), "position": target}
                )
            )
            if not (
                math.isclose(
                    current["position"]["x"], target["x"], abs_tol=FLOAT_COMPARISON_TOLERANCE
                )
                and math.isclose(
                    current["position"]["y"], target["y"], abs_tol=FLOAT_COMPARISON_TOLERANCE
                )
                and math.isclose(
                    current["position"]["angle"],
                    target["angle"],
                    abs_tol=FLOAT_COMPARISON_TOLERANCE,
                )
            ):
                moves.append(
                    {
                        "reference": reference,
                        "property_name": property_name,
                        "from": dict(current["position"]),
                        "to": target,
                    }
                )
        return moves

    def _choose_property_position(
        self,
        reference: str,
        property_name: str,
        property_data: dict[str, Any],
        symbol_boxes: list[BoundingBox],
        planned_boxes: list[BoundingBox],
    ) -> dict[str, float]:
        symbol = self.get_symbol(reference)
        if symbol is None:
            raise KeyError(f"Symbol not found: {reference}")
        symbol_position = symbol["position"]
        for dx, dy in self._property_candidate_offsets(property_name):
            candidate = {
                "x": symbol_position["x"] + dx,
                "y": symbol_position["y"] + dy,
                "angle": 0.0,
            }
            candidate_box = self._property_bbox(
                property_name,
                {
                    "text": property_data.get("text"),
                    "position": candidate,
                },
            )
            if any(candidate_box.intersects(box, padding=0.5) for box in symbol_boxes):
                continue
            if any(candidate_box.intersects(box, padding=0.25) for box in planned_boxes):
                continue
            return candidate
        return {
            "x": symbol_position["x"],
            "y": symbol_position["y"] + self._property_candidate_offsets(property_name)[0][1],
            "angle": 0.0,
        }

    def _property_candidate_offsets(self, property_name: str) -> list[tuple[float, float]]:
        defaults = {
            "Reference": [(0.0, -4.0), (0.0, -8.0), (-8.0, -4.0), (8.0, -4.0)],
            "Value": [(0.0, 4.0), (0.0, 8.0), (-8.0, 4.0), (8.0, 4.0)],
            "Footprint": [(0.0, 8.0), (0.0, 12.0), (-8.0, 8.0), (8.0, 8.0)],
            "Datasheet": [(0.0, 12.0), (0.0, 16.0), (-8.0, 12.0), (8.0, 12.0)],
        }
        if property_name in defaults:
            return defaults[property_name]
        return [(0.0, 16.0), (0.0, 20.0), (-8.0, 16.0), (8.0, 16.0)]

    def _cleanup_label_refusals(
        self, overlaps: list[dict[str, Any]], safe_block_moves: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        moved_labels = {
            label_uuid
            for move in safe_block_moves
            for label_uuid in cast(list[str], move["raw_plan"]["labels"])
        }
        refused_labels = []
        seen: set[str] = set()
        for label in self.list_labels():
            label_uuid = label.get("uuid")
            if label_uuid is None or label_uuid in moved_labels or label_uuid in seen:
                continue
            if any(label_uuid in overlap["objects"] for overlap in overlaps):
                seen.add(cast(str, label_uuid))
                refused_labels.append(
                    {
                        "label_uuid": label_uuid,
                        "text": label["text"],
                        "reason": (
                            "Standalone label cleanup is not applied automatically; "
                            "labels only move when they are part of a safe block move."
                        ),
                    }
                )
        return refused_labels

    def _label_has_overlap(self, label_uuid: str) -> bool:
        return any(label_uuid in overlap["objects"] for overlap in self.find_overlaps())

    def _ensure_lib_symbols(self) -> SExprList:
        lib_symbols = self.root.first_child("lib_symbols")
        if lib_symbols is None:
            lib_symbols = SExprList([SExprAtom("lib_symbols")])
            self.root.items.insert(min(len(self.root.items), 5), lib_symbols)
        return lib_symbols

    def _top_level(self, head: str) -> list[SExprList]:
        return [
            item
            for item in self.root.items[1:]
            if isinstance(item, SExprList) and item.head() == head
        ]

    def _find_symbol_node(self, reference: str) -> SExprList | None:
        for symbol in self._top_level("symbol"):
            if self._symbol_reference(symbol) == reference:
                return symbol
        return None

    def _find_label_node(self, label_uuid: str) -> SExprList | None:
        for head in ("label", "global_label", "hierarchical_label"):
            for label in self._top_level(head):
                if self._get_uuid(label) == label_uuid:
                    return label
        return None

    def _find_wire_node(self, wire_uuid: str) -> SExprList | None:
        for wire in self._top_level("wire"):
            if self._get_uuid(wire) == wire_uuid:
                return wire
        return None

    def _find_junction_node(self, x: float, y: float) -> SExprList | None:
        for junction in self._top_level("junction"):
            xy_expr = junction.first_child("xy")
            if xy_expr is None:
                continue
            position = self._parse_xy(xy_expr)
            if math.dist((x, y), (position["x"], position["y"])) <= CONNECTIVITY_TOLERANCE_MM:
                return junction
        return None

    def _find_property_node(self, reference: str, property_name: str) -> SExprList | None:
        symbol = self._find_symbol_node(reference)
        if symbol is None:
            return None
        for property_expr in symbol.child_lists("property"):
            if self._atom_text(property_expr.items[1], default="") == property_name:
                return property_expr
        return None

    def _symbol_reference(self, symbol: SExprList) -> str:
        for property_expr in symbol.child_lists("property"):
            if self._atom_text(property_expr.items[1], default="") == "Reference":
                return self._atom_text(property_expr.items[2], default="Unknown") or "Unknown"
        return self._get_uuid(symbol) or "Unknown"

    def _symbol_to_dict(self, symbol: SExprList) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        value = ""
        reference = "Unknown"
        footprint = None
        for property_expr in symbol.child_lists("property"):
            property_name = self._atom_text(property_expr.items[1], default="") or ""
            property_value = self._atom_text(property_expr.items[2], default="") or ""
            property_dict = {
                "text": property_value,
                "position": self._parse_at(property_expr),
            }
            property_uuid = self._get_uuid(property_expr)
            if property_uuid is not None:
                property_dict["uuid"] = property_uuid
            properties[property_name] = property_dict
            if property_name == "Reference":
                reference = property_value
            elif property_name == "Value":
                value = property_value
            elif property_name == "Footprint":
                footprint = property_value

        return {
            "reference": reference,
            "value": value,
            "lib_id": self._child_text(symbol, "lib_id"),
            "uuid": self._get_uuid(symbol),
            "position": self._parse_at(symbol),
            "properties": properties,
            "footprint": footprint,
            "bounds": self._bbox_to_dict(self._symbol_bbox_from_position(self._parse_at(symbol))),
        }

    def _label_to_dict(self, label: SExprList) -> dict[str, Any]:
        head = label.head() or "label"
        label_data = {
            "type": {
                "label": "local",
                "global_label": "global",
                "hierarchical_label": "hierarchical",
            }.get(head, head),
            "text": self._atom_text(label.items[1], default=""),
            "uuid": self._get_uuid(label),
            "position": self._parse_at(label),
        }
        shape_expr = label.first_child("shape")
        if shape_expr is not None and len(shape_expr.items) >= 2:
            label_data["shape"] = self._atom_text(shape_expr.items[1], default="")
        return label_data

    def _property_nodes(self, symbol: SExprList) -> list[dict[str, Any]]:
        properties = []
        for property_expr in symbol.child_lists("property"):
            properties.append(
                {
                    "name": self._atom_text(property_expr.items[1], default=""),
                    "node": property_expr,
                }
            )
        return properties

    def _wire_point_nodes(self, wire: SExprList) -> list[SExprList]:
        pts_expr = wire.first_child("pts")
        if pts_expr is None:
            return []
        return pts_expr.child_lists("xy")

    def _wire_to_dict(self, wire: SExprList) -> dict[str, Any]:
        points = [self._parse_xy(point_node) for point_node in self._wire_point_nodes(wire)]
        return {"uuid": self._get_uuid(wire), "points": points}

    def _parse_at(self, expr: SExprList) -> dict[str, float]:
        at_expr = expr.first_child("at")
        if at_expr is None:
            return {"x": 0.0, "y": 0.0, "angle": 0.0}
        values = self._atoms_to_floats(at_expr.items[1:4])
        if len(values) == 2:
            values.append(0.0)
        if len(values) < 3:
            return {"x": 0.0, "y": 0.0, "angle": 0.0}
        return {"x": values[0], "y": values[1], "angle": values[2]}

    def _parse_xy(self, expr: SExprList) -> dict[str, float]:
        values = self._atoms_to_floats(expr.items[1:3])
        if len(values) < 2:
            return {"x": 0.0, "y": 0.0}
        return {"x": values[0], "y": values[1]}

    def _set_at(self, expr: SExprList, x: float, y: float, angle: float | None = None) -> None:
        at_expr = expr.first_child("at")
        current_angle = self._parse_at(expr)["angle"]
        new_angle = current_angle if angle is None else angle
        replacement = self._build_at(x, y, new_angle)
        if at_expr is None:
            expr.items.append(replacement)
            return
        for index, item in enumerate(expr.items):
            if item is at_expr:
                expr.items[index] = replacement
                return

    def _set_xy(self, expr: SExprList, x: float, y: float) -> None:
        replacement = self._build_xy(x, y)
        expr.items = replacement.items

    def _build_at(self, x: float, y: float, angle: float) -> SExprList:
        return SExprList(
            [
                SExprAtom("at"),
                SExprAtom(_format_number(x)),
                SExprAtom(_format_number(y)),
                SExprAtom(_format_number(angle)),
            ]
        )

    def _build_xy(self, x: float, y: float) -> SExprList:
        return SExprList(
            [
                SExprAtom("xy"),
                SExprAtom(_format_number(x)),
                SExprAtom(_format_number(y)),
            ]
        )

    def _build_property(
        self,
        name: str,
        value: str,
        x: float,
        y: float,
        angle: float = 0.0,
        hidden: bool = False,
    ) -> SExprList:
        items: list[SExprNode] = [
            SExprAtom("property"),
            SExprAtom(name, quoted=True),
            SExprAtom(value, quoted=True),
            self._build_at(x, y, angle),
            SExprList(
                [
                    SExprAtom("effects"),
                    SExprList(
                        [
                            SExprAtom("font"),
                            SExprList([SExprAtom("size"), SExprAtom("1.27"), SExprAtom("1.27")]),
                        ]
                    ),
                ]
            ),
        ]
        if hidden:
            items.append(SExprList([SExprAtom("hide"), SExprAtom("yes")]))
        return SExprList(items)

    def _child_text(self, expr: SExprList, head: str) -> str | None:
        child = expr.first_child(head)
        if child is None or len(child.items) < 2:
            return None
        return self._atom_text(child.items[1], default="")

    def _get_uuid(self, expr: SExprList) -> str | None:
        return self._child_text(expr, "uuid")

    def _atom_text(self, node: SExprNode, default: str | None = None) -> str | None:
        if isinstance(node, SExprAtom):
            return node.value
        return default

    def _atoms_to_floats(self, nodes: list[SExprNode]) -> list[float]:
        values = []
        for node in nodes:
            if isinstance(node, SExprAtom):
                try:
                    values.append(float(node.value))
                except ValueError:
                    continue
        return values

    def _symbol_bbox(self, symbol: dict[str, Any]) -> BoundingBox:
        return self._symbol_bbox_from_position(symbol["position"])

    def _symbol_bbox_from_position(self, position: dict[str, float]) -> BoundingBox:
        # Approximate default symbol footprint for coarse overlap/connectivity risk detection.
        half_width = DEFAULT_SYMBOL_HALF_WIDTH_MM
        half_height = DEFAULT_SYMBOL_HALF_HEIGHT_MM
        return BoundingBox(
            left=position["x"] - half_width,
            top=position["y"] - half_height,
            right=position["x"] + half_width,
            bottom=position["y"] + half_height,
        )

    def _label_bbox(self, label: dict[str, Any]) -> BoundingBox:
        position = label["position"]
        width = max(3.0, len(label["text"]) * TEXT_CHAR_WIDTH_MM)
        height = 1.8
        return BoundingBox(
            left=position["x"],
            top=position["y"] - height / 2,
            right=position["x"] + width,
            bottom=position["y"] + height / 2,
        )

    def _property_bbox(self, property_name: str, property_data: dict[str, Any]) -> BoundingBox:
        position = property_data["position"]
        text = property_data.get("text") or property_name
        width = max(4.0, len(text) * TEXT_CHAR_WIDTH_MM)
        height = 1.8
        return BoundingBox(
            left=position["x"],
            top=position["y"] - height / 2,
            right=position["x"] + width,
            bottom=position["y"] + height / 2,
        )

    def _bbox_to_dict(self, bbox: BoundingBox) -> dict[str, float]:
        return {
            "left": bbox.left,
            "top": bbox.top,
            "right": bbox.right,
            "bottom": bbox.bottom,
        }


def _expand_bbox(bbox: BoundingBox, amount: float) -> BoundingBox:
    return BoundingBox(
        left=bbox.left - amount,
        top=bbox.top - amount,
        right=bbox.right + amount,
        bottom=bbox.bottom + amount,
    )


def _wire_segments(wire: dict[str, Any]) -> list[dict[str, tuple[float, float]]]:
    points = wire.get("points", [])
    return [
        {
            "start": (points[index]["x"], points[index]["y"]),
            "end": (points[index + 1]["x"], points[index + 1]["y"]),
        }
        for index in range(len(points) - 1)
    ]


def _point_in_bbox(point: tuple[float, float], bbox: BoundingBox) -> bool:
    x, y = point
    return bbox.left <= x <= bbox.right and bbox.top <= y <= bbox.bottom


def _point_on_segment(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    tolerance: float,
) -> bool:
    px, py = point
    x1, y1 = start
    x2, y2 = end

    dx = x2 - x1
    dy = y2 - y1
    if math.isclose(dx, 0.0, abs_tol=FLOAT_COMPARISON_TOLERANCE) and math.isclose(
        dy, 0.0, abs_tol=FLOAT_COMPARISON_TOLERANCE
    ):
        return math.dist(point, start) <= tolerance

    segment_length_squared = dx * dx + dy * dy
    projection = ((px - x1) * dx + (py - y1) * dy) / segment_length_squared
    clamped_projection = max(0.0, min(1.0, projection))
    closest_point = (x1 + clamped_projection * dx, y1 + clamped_projection * dy)
    return math.dist(point, closest_point) <= tolerance


def _segment_intersects_bbox(
    start: tuple[float, float],
    end: tuple[float, float],
    bbox: BoundingBox,
) -> bool:
    if _point_in_bbox(start, bbox) or _point_in_bbox(end, bbox):
        return True

    min_x = min(start[0], end[0])
    max_x = max(start[0], end[0])
    min_y = min(start[1], end[1])
    max_y = max(start[1], end[1])
    if max_x < bbox.left or min_x > bbox.right or max_y < bbox.top or min_y > bbox.bottom:
        return False

    return (
        any(
            _point_on_segment(corner, start, end, tolerance=0.01)
            for corner in (
                (bbox.left, bbox.top),
                (bbox.left, bbox.bottom),
                (bbox.right, bbox.top),
                (bbox.right, bbox.bottom),
            )
        )
        or (
            math.isclose(start[0], end[0], abs_tol=FLOAT_COMPARISON_TOLERANCE)
            and bbox.left <= start[0] <= bbox.right
            and not (max_y < bbox.top or min_y > bbox.bottom)
        )
        or (
            math.isclose(start[1], end[1], abs_tol=FLOAT_COMPARISON_TOLERANCE)
            and bbox.top <= start[1] <= bbox.bottom
            and not (max_x < bbox.left or min_x > bbox.right)
        )
    )


def compare_connectivity_snapshots(
    target_type: str,
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    """Compare two geometry-based connectivity snapshots for one target."""
    if target_type == "symbol":
        before_label_texts = sorted(label["text"] for label in before.get("nearby_labels", []))
        after_label_texts = sorted(label["text"] for label in after.get("nearby_labels", []))
        before_point_wire_counts = sorted(
            len(point.get("wires", [])) for point in before.get("connection_points", [])
        )
        after_point_wire_counts = sorted(
            len(point.get("wires", [])) for point in after.get("connection_points", [])
        )
        preserved = (
            sorted(before.get("nearby_wires", [])) == sorted(after.get("nearby_wires", []))
            and before_label_texts == after_label_texts
            and before_point_wire_counts == after_point_wire_counts
        )
        reason = "connectivity preserved" if preserved else "attached wires or labels changed"
        return {
            "preserved": preserved,
            "reason": reason,
            "before": before,
            "after": after,
        }

    if target_type == "label":
        preserved = sorted(before.get("touching_wires", [])) == sorted(
            after.get("touching_wires", [])
        ) and sorted(before.get("wire_contacts", []), key=lambda entry: entry["uuid"]) == sorted(
            after.get("wire_contacts", []), key=lambda entry: entry["uuid"]
        )
        reason = "connectivity preserved" if preserved else "touching wires changed"
        return {
            "preserved": preserved,
            "reason": reason,
            "before": before,
            "after": after,
        }

    raise ValueError(f"Unsupported connectivity comparison target type: {target_type}")


def compare_block_connectivity_snapshots(
    before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    """Compare two coarse block connectivity snapshots."""
    preserved = (
        sorted(before.get("external_connections", []))
        == sorted(after.get("external_connections", []))
        and before.get("boundary_wire_count") == after.get("boundary_wire_count")
        and sorted(before.get("internal_symbols", [])) == sorted(after.get("internal_symbols", []))
        and sorted(before.get("labels", [])) == sorted(after.get("labels", []))
        and sorted(before.get("wires", [])) == sorted(after.get("wires", []))
    )
    reason = "block connectivity preserved" if preserved else "block connectivity changed"
    return {"preserved": preserved, "reason": reason, "before": before, "after": after}


def _point_key(x: float, y: float) -> tuple[float, float]:
    return (round(x, 6), round(y, 6))


def _coerce_point(point: dict[str, float]) -> dict[str, float]:
    try:
        return {"x": float(point["x"]), "y": float(point["y"])}
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Point must contain numeric x and y values: {point}") from exc


def _needs_quotes(value: str) -> bool:
    return value == "" or any(
        char.isspace() or char in S_EXPRESSION_SPECIAL_CHARS for char in value
    )


def _format_number(value: float) -> str:
    if math.isclose(value, round(value), abs_tol=FLOAT_COMPARISON_TOLERANCE):
        return str(int(round(value)))
    formatted = f"{value:.6f}".rstrip("0").rstrip(".")
    return formatted or "0"


def tokenize_s_expression(content: str) -> list[SExprAtom | str]:
    """Tokenize KiCad S-expression text."""
    tokens: list[SExprAtom | str] = []
    index = 0
    while index < len(content):
        char = content[index]
        if char.isspace():
            index += 1
            continue
        if char == ";":
            while index < len(content) and content[index] != "\n":
                index += 1
            continue
        if char in {"(", ")"}:
            tokens.append(char)
            index += 1
            continue
        if char == '"':
            index += 1
            value_chars: list[str] = []
            while index < len(content):
                current = content[index]
                if current == "\\":
                    index += 1
                    if index >= len(content):
                        raise SExpressionError("Unterminated escape sequence in quoted string")
                    value_chars.append(content[index])
                    index += 1
                    continue
                if current == '"':
                    index += 1
                    break
                value_chars.append(current)
                index += 1
            else:
                raise SExpressionError("Unterminated quoted string")
            tokens.append(SExprAtom("".join(value_chars), quoted=True))
            continue

        start = index
        while (
            index < len(content)
            and not content[index].isspace()
            and content[index] not in {"(", ")"}
        ):
            index += 1
        tokens.append(SExprAtom(content[start:index], quoted=False))
    return tokens


def parse_s_expression(content: str) -> SExprList:
    """Parse KiCad S-expression text into a structured tree."""
    tokens = tokenize_s_expression(content)
    node_stack: list[list[SExprNode]] = []
    root: SExprList | None = None

    for token in tokens:
        if token == "(":
            node_stack.append([])
            continue
        if token == ")":
            if not node_stack:
                raise SExpressionError("Unexpected closing parenthesis")
            finished = SExprList(node_stack.pop())
            if node_stack:
                node_stack[-1].append(finished)
            else:
                if root is not None:
                    raise SExpressionError("Multiple root expressions are not supported")
                root = finished
            continue

        if not node_stack:
            raise SExpressionError("Atom found outside S-expression list")
        if not isinstance(token, SExprAtom):
            raise SExpressionError(f"Expected SExprAtom but got {type(token).__name__}")
        node_stack[-1].append(token)

    if node_stack:
        raise SExpressionError("Unbalanced S-expression: missing closing parenthesis")
    if root is None:
        raise SExpressionError("Empty S-expression content")
    return root


def serialize_s_expression(node: SExprNode, indent: int = 0) -> str:
    """Serialize a structured S-expression tree."""
    if isinstance(node, SExprAtom):
        return node.to_source()

    if not node.items:
        return "()"

    inline = _should_inline(node)
    rendered = [serialize_s_expression(item, indent + 1) for item in node.items]
    if inline:
        return f"({' '.join(rendered)})"

    lines = [f"({rendered[0]}"]
    for item in node.items[1:]:
        lines.append(f"{'  ' * (indent + 1)}{serialize_s_expression(item, indent + 1)}")
    lines.append(f"{'  ' * indent})")
    return "\n".join(lines)


def _should_inline(node: SExprList) -> bool:
    head = node.head()
    if head in {"at", "xy", "uuid", "shape", "paper", "version", "generator", "lib_id"}:
        return True
    if all(isinstance(item, SExprAtom) for item in node.items):
        return True
    if head == "pts":
        return True
    if head == "property":
        return False
    return len(node.items) <= 4 and all(
        isinstance(item, SExprAtom) or (isinstance(item, SExprList) and _should_inline(item))
        for item in node.items
    )


def validate_schematic_text(content: str) -> dict[str, Any]:
    """Validate schematic S-expression structure from raw text."""
    schematic = KiCadSchematic.from_text(content)
    return {
        "valid": True,
        "root": schematic.root.head(),
        "symbol_count": len(schematic.list_symbols()),
        "label_count": len(schematic.list_labels()),
        "wire_count": len(schematic.list_wires()),
    }


def validate_schematic_file(schematic_path: str) -> dict[str, Any]:
    """Validate a schematic file from disk."""
    content = Path(schematic_path).read_text(encoding="utf-8")
    validation = validate_schematic_text(content)
    validation["schematic_path"] = schematic_path
    return validation
