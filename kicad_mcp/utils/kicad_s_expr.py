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

    def move_label(
        self, label_uuid: str, x: float, y: float, angle: float | None = None
    ) -> dict[str, Any]:
        """Move a label instance."""
        label = self._find_label_node(label_uuid)
        if label is None:
            raise KeyError(f"Label not found: {label_uuid}")
        self._set_at(label, x, y, angle)
        return self._label_to_dict(label)

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
            for candidate_x, candidate_y in self._candidate_positions(position["x"], position["y"]):
                self._set_at(label_node, candidate_x, candidate_y, position["angle"])
                if not self._label_has_overlap(label_uuid):
                    moved_labels.append(self._label_to_dict(label_node))
                    break
            else:
                self._set_at(label_node, position["x"], position["y"], position["angle"])

        return moved_labels

    def _candidate_positions(self, x: float, y: float) -> list[tuple[float, float]]:
        offsets = []
        for step in (3.0, 6.0, 9.0, 12.0, 15.0):
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

    def _build_at(self, x: float, y: float, angle: float) -> SExprList:
        return SExprList(
            [
                SExprAtom("at"),
                SExprAtom(_format_number(x)),
                SExprAtom(_format_number(y)),
                SExprAtom(_format_number(angle)),
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
        half_width = 5.0
        half_height = 4.0
        return BoundingBox(
            left=position["x"] - half_width,
            top=position["y"] - half_height,
            right=position["x"] + half_width,
            bottom=position["y"] + half_height,
        )

    def _label_bbox(self, label: dict[str, Any]) -> BoundingBox:
        position = label["position"]
        width = max(3.0, len(label["text"]) * 0.9)
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
        width = max(4.0, len(text) * 0.9)
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


def _needs_quotes(value: str) -> bool:
    return value == "" or any(char.isspace() or char in '()"' for char in value)


def _format_number(value: float) -> str:
    if math.isclose(value, round(value), abs_tol=1e-9):
        return str(int(round(value)))
    return f"{value:.6f}".rstrip("0").rstrip(".")


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
        assert isinstance(token, SExprAtom)
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
