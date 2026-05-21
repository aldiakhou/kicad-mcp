"""
Structured KiCad schematic S-expression parsing and editing utilities.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any, TypeAlias, cast


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

    def to_text(self) -> str:
        """Serialize the schematic back to KiCad S-expression text."""
        return f"{serialize_s_expression(self.root)}\n"

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

    def move_junction(self, old_x: float, old_y: float, new_x: float, new_y: float) -> dict[str, Any]:
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
            label["uuid"]: self._label_bbox(label) for label in labels if label.get("uuid") is not None
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

    def move_symbol(self, reference: str, x: float, y: float, angle: float | None = None) -> dict[str, Any]:
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
            cast(str, label["uuid"]): label for label in self.list_labels() if label.get("uuid") is not None
        }
        nearby_labels: list[dict[str, Any]] = []
        nearby_label_ids: set[str] = set()
        for group in point_groups.values():
            for label in labels_by_id.values():
                label_position = label["position"]
                if math.dist(
                    (group["x"], group["y"]),
                    (label_position["x"], label_position["y"]),
                ) > CONNECTIVITY_TOLERANCE_MM:
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
                {
                    wire["uuid"]
                    for wire in touching_wires
                    if wire.get("uuid") is not None
                }
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
                raise ValueError(f"Cannot move {reference} safely: attached wire has no UUID.")
            if wire["point_count"] != 2:
                raise ValueError(
                    f"Cannot move {reference} safely: wire {wire_uuid} must be a straight 2-point wire."
                )
            attached_endpoints = [endpoint for endpoint in wire["endpoints"] if endpoint["inside_symbol"]]
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
                f"Cannot rotate {reference} safely with attached wires: rotation-aware pin mapping is not implemented yet."
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
        touching_wires = self.find_wires_touching_point(current_position["x"], current_position["y"])

        wire_endpoint_moves: list[dict[str, Any]] = []
        seen_endpoints: set[tuple[str | None, int]] = set()
        for wire in touching_wires:
            wire_uuid = wire.get("uuid")
            if wire_uuid is None:
                raise ValueError(f"Cannot move label {label_uuid} safely: attached wire has no UUID.")
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
                f"Cannot rotate label {label_uuid} safely with attached wires: rotation-aware pin mapping is not implemented yet."
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
                if math.dist(
                    (point["x"], point["y"]),
                    (position["x"], position["y"]),
                ) > CONNECTIVITY_TOLERANCE_MM:
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

    def _label_has_overlap(self, label_uuid: str) -> bool:
        return any(label_uuid in overlap["objects"] for overlap in self.find_overlaps())

    def _top_level(self, head: str) -> list[SExprList]:
        return [item for item in self.root.items[1:] if isinstance(item, SExprList) and item.head() == head]

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

    return any(
        _point_on_segment(corner, start, end, tolerance=0.01)
        for corner in (
            (bbox.left, bbox.top),
            (bbox.left, bbox.bottom),
            (bbox.right, bbox.top),
            (bbox.right, bbox.bottom),
        )
    ) or (
        math.isclose(start[0], end[0], abs_tol=FLOAT_COMPARISON_TOLERANCE)
        and bbox.left <= start[0] <= bbox.right
        and not (max_y < bbox.top or min_y > bbox.bottom)
    ) or (
        math.isclose(start[1], end[1], abs_tol=FLOAT_COMPARISON_TOLERANCE)
        and bbox.top <= start[1] <= bbox.bottom
        and not (max_x < bbox.left or min_x > bbox.right)
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
        after_point_wire_counts = sorted(len(point.get("wires", [])) for point in after.get("connection_points", []))
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
        preserved = (
            sorted(before.get("touching_wires", [])) == sorted(after.get("touching_wires", []))
            and sorted(before.get("wire_contacts", []), key=lambda entry: entry["uuid"])
            == sorted(after.get("wire_contacts", []), key=lambda entry: entry["uuid"])
        )
        reason = "connectivity preserved" if preserved else "touching wires changed"
        return {
            "preserved": preserved,
            "reason": reason,
            "before": before,
            "after": after,
        }

    raise ValueError(f"Unsupported connectivity comparison target type: {target_type}")


def _point_key(x: float, y: float) -> tuple[float, float]:
    return (round(x, 6), round(y, 6))


def _needs_quotes(value: str) -> bool:
    return value == "" or any(char.isspace() or char in S_EXPRESSION_SPECIAL_CHARS for char in value)


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
        while index < len(content) and not content[index].isspace() and content[index] not in {"(", ")"}:
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
