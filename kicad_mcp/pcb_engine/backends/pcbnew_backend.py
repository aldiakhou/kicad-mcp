"""Primary PCB backend using KiCad's pcbnew Python bindings."""

from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any

from kicad_mcp.utils.library_resolver import resolve_footprint
from kicad_mcp.utils.pcbnew_runtime import get_pcbnew


class PcbnewBoardBackend:
    """Board backend backed by KiCad's native pcbnew module."""

    name = "pcbnew"

    def __init__(self, status: dict[str, Any] | None = None):
        self._status = status or {"success": True, "available": True, "backend": self.name}
        self._pcbnew = get_pcbnew(required=True)

    def status(self) -> dict[str, Any]:
        return dict(self._status)

    def empty(self, board_width_mm: float, board_height_mm: float) -> PcbnewBoard:
        board = self._pcbnew.BOARD()
        model = PcbnewBoard(board, self.status())
        model.create_board_outline(board_width_mm, board_height_mm)
        return model

    def from_file(self, pcb_path: str) -> PcbnewBoard:
        board = self._pcbnew.LoadBoard(str(pcb_path))
        return PcbnewBoard(board, self.status())

    def from_text(self, content: str) -> PcbnewBoard:
        with tempfile.TemporaryDirectory(prefix="kicad_mcp_pcbnew_load_") as temp_dir:
            pcb_path = Path(temp_dir) / "board.kicad_pcb"
            pcb_path.write_text(content, encoding="utf-8")
            return self.from_file(str(pcb_path))


class PcbnewBoard:
    """Small compatibility wrapper around pcbnew.BOARD."""

    backend_name = "pcbnew"

    def __init__(self, board: Any, status: dict[str, Any]):
        self.board = board
        self.backend_status = status
        self._pcbnew = get_pcbnew(required=True)

    def save_to(self, pcb_path: str) -> None:
        self._pcbnew.SaveBoard(str(pcb_path), self.board)

    def to_text(self) -> str:
        with tempfile.TemporaryDirectory(prefix="kicad_mcp_pcbnew_save_") as temp_dir:
            pcb_path = Path(temp_dir) / "board.kicad_pcb"
            self.save_to(str(pcb_path))
            return pcb_path.read_text(encoding="utf-8")

    def create_board_outline(
        self,
        width_mm: float,
        height_mm: float,
        origin_x: float = 0.0,
        origin_y: float = 0.0,
    ) -> dict[str, Any]:
        edge_layer = self._layer_id("Edge.Cuts")
        for drawing in list(self.board.GetDrawings()):
            if getattr(drawing, "GetLayer", lambda: None)() == edge_layer:
                self.board.Remove(drawing)
        points = [
            (origin_x, origin_y),
            (origin_x + width_mm, origin_y),
            (origin_x + width_mm, origin_y + height_mm),
            (origin_x, origin_y + height_mm),
            (origin_x, origin_y),
        ]
        for start, end in zip(points, points[1:]):
            shape = self._pcbnew.PCB_SHAPE(self.board)
            shape.SetShape(self._pcbnew.SHAPE_T_SEGMENT)
            shape.SetLayer(edge_layer)
            shape.SetWidth(self._iu(0.1))
            shape.SetStart(self._vec(start[0], start[1]))
            shape.SetEnd(self._vec(end[0], end[1]))
            self.board.Add(shape)
        return {
            "width_mm": width_mm,
            "height_mm": height_mm,
            "origin": {"x": origin_x, "y": origin_y},
        }

    def add_footprint(
        self,
        footprint_id: str,
        footprint_node: Any,
        reference: str,
        value: str,
        x: float,
        y: float,
        angle: float = 0.0,
        net_assignments: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if self.find_footprint(reference) is not None:
            raise ValueError(f"Footprint reference already exists: {reference}")
        footprint = self._load_footprint(footprint_id, footprint_node)
        footprint.SetReference(reference)
        footprint.SetValue(value)
        footprint.SetPosition(self._vec(x, y))
        footprint.SetOrientationDegrees(float(angle))
        self.board.Add(footprint)
        if net_assignments:
            self.assign_footprint_pad_nets(reference, net_assignments, clear_stale=False)
        return {
            "reference": reference,
            "value": value,
            "footprint_id": footprint_id,
            "position": {"x": x, "y": y, "angle": angle},
            "net_assignments": net_assignments or {},
        }

    def move_footprint(
        self,
        reference: str,
        x: float,
        y: float,
        angle: float | None = None,
    ) -> dict[str, Any]:
        footprint = self.find_footprint(reference)
        if footprint is None:
            raise KeyError(f"Footprint not found: {reference}")
        current_angle = float(footprint.GetOrientationDegrees())
        new_angle = current_angle if angle is None else float(angle)
        footprint.SetPosition(self._vec(x, y))
        footprint.SetOrientationDegrees(new_angle)
        return {"reference": reference, "position": {"x": x, "y": y, "angle": new_angle}}

    def assign_footprint_pad_nets(
        self,
        reference: str,
        net_assignments: dict[str, str],
        clear_stale: bool = True,
    ) -> dict[str, Any]:
        footprint = self.find_footprint(reference)
        if footprint is None:
            raise KeyError(f"Footprint not found: {reference}")
        pads = list(footprint.Pads())
        available = {str(pad.GetPadName()) for pad in pads}
        missing = sorted(pad for pad in net_assignments if pad not in available)
        if clear_stale:
            for pad in pads:
                pad.SetNetCode(0)
        for pad in pads:
            pad_name = str(pad.GetPadName())
            if pad_name in net_assignments:
                pad.SetNet(self._ensure_net_item(str(net_assignments[pad_name])))
        return {
            "reference": reference,
            "assigned_pads": sorted(pad for pad in net_assignments if pad in available),
            "missing_pads": missing,
        }

    def ensure_net(self, net_name: str) -> int:
        return int(self._ensure_net_item(net_name).GetNetCode())

    def find_footprint(self, reference: str) -> Any | None:
        return self.board.FindFootprintByReference(str(reference))

    def list_footprints(self) -> list[dict[str, Any]]:
        footprints = []
        for footprint in self.board.GetFootprints():
            footprints.append(
                {
                    "reference": str(footprint.GetReference()),
                    "value": str(footprint.GetValue()),
                    "footprint_name": self._footprint_name(footprint),
                    "position": self._position(footprint.GetPosition())
                    | {"angle": float(footprint.GetOrientationDegrees())},
                    "bounds": self.footprint_bounds(footprint),
                }
            )
        return footprints

    def footprint_bounds(self, footprint: Any) -> dict[str, float]:
        box = footprint.GetBoundingBox()
        return _box_to_bounds(self._pcbnew, box)

    def footprint_pad_positions(self) -> list[dict[str, Any]]:
        pads = []
        for footprint in self.board.GetFootprints():
            reference = str(footprint.GetReference())
            footprint_name = self._footprint_name(footprint)
            for pad in footprint.Pads():
                size = pad.GetSize()
                position = self._position(pad.GetPosition())
                pads.append(
                    {
                        "reference": reference,
                        "footprint_name": footprint_name,
                        "pad": str(pad.GetPadName()),
                        "position": position,
                        "size": {
                            "width": self._mm(size.x),
                            "height": self._mm(size.y),
                        },
                        "bounds": _box_to_bounds(self._pcbnew, pad.GetBoundingBox()),
                        "net_id": int(pad.GetNetCode()),
                        "net_name": str(pad.GetNetname() or ""),
                    }
                )
        return pads

    def list_nets(self) -> list[dict[str, Any]]:
        return sorted(
            [
                {"id": int(net.GetNetCode()), "name": str(net.GetNetname())}
                for net in self.board.GetNetsByName().values()
            ],
            key=lambda item: item["id"],
        )

    def list_track_segments(self) -> list[dict[str, Any]]:
        segments = []
        for item in self.board.GetTracks():
            if isinstance(item, self._pcbnew.PCB_VIA):
                continue
            if not hasattr(item, "GetStart") or not hasattr(item, "GetEnd"):
                continue
            segments.append(
                {
                    "start": self._position(item.GetStart()),
                    "end": self._position(item.GetEnd()),
                    "width_mm": self._mm(item.GetWidth()),
                    "layer": self._layer_name(item.GetLayer()),
                    "net_id": int(item.GetNetCode()),
                    "net_name": str(item.GetNetname() or ""),
                }
            )
        return segments

    def list_vias(self) -> list[dict[str, Any]]:
        vias = []
        for item in self.board.GetTracks():
            if not isinstance(item, self._pcbnew.PCB_VIA):
                continue
            vias.append(
                {
                    "position": self._position(item.GetPosition()),
                    "diameter_mm": self._mm(item.GetWidth(self._layer_id("F.Cu"))),
                    "drill_mm": self._mm(item.GetDrillValue()),
                    "net_id": int(item.GetNetCode()),
                    "net_name": str(item.GetNetname() or ""),
                }
            )
        return vias

    def clear_routing(self, *, include_zones: bool = True) -> dict[str, int]:
        removed_segments = 0
        removed_vias = 0
        removed_zones = 0
        for item in list(self.board.GetTracks()):
            if isinstance(item, self._pcbnew.PCB_VIA):
                removed_vias += 1
            else:
                removed_segments += 1
            self.board.Remove(item)
        if include_zones:
            for zone in list(self.board.Zones()):
                removed_zones += 1
                self.board.Remove(zone)
        return {
            "removed_segments": removed_segments,
            "removed_vias": removed_vias,
            "removed_zones": removed_zones,
            "removed_total": removed_segments + removed_vias + removed_zones,
        }

    def add_track(
        self,
        net_name: str,
        points: list[dict[str, float]],
        layer: str = "F.Cu",
        width_mm: float = 0.25,
    ) -> dict[str, Any]:
        if len(points) < 2:
            raise ValueError("A track requires at least two points")
        net = self._ensure_net_item(net_name)
        segments = []
        for start, end in zip(points, points[1:]):
            track = self._pcbnew.PCB_TRACK(self.board)
            track.SetStart(self._vec(float(start["x"]), float(start["y"])))
            track.SetEnd(self._vec(float(end["x"]), float(end["y"])))
            track.SetWidth(self._iu(width_mm))
            track.SetLayer(self._layer_id(layer))
            track.SetNet(net)
            self.board.Add(track)
            segments.append({"start": dict(start), "end": dict(end)})
        return {
            "net_name": net_name,
            "net_id": int(net.GetNetCode()),
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
        net = self._ensure_net_item(net_name)
        via = self._pcbnew.PCB_VIA(self.board)
        via.SetViaType(self._pcbnew.VIATYPE_THROUGH)
        via.SetPosition(self._vec(x, y))
        via.SetWidth(self._iu(diameter_mm))
        via.SetDrill(self._iu(drill_mm))
        via.SetLayerPair(self._layer_id("F.Cu"), self._layer_id("B.Cu"))
        via.SetNet(net)
        self.board.Add(via)
        return {
            "net_name": net_name,
            "net_id": int(net.GetNetCode()),
            "position": {"x": x, "y": y},
            "diameter_mm": diameter_mm,
            "drill_mm": drill_mm,
        }

    def add_zone(
        self,
        net_name: str,
        layer: str,
        outline: list[dict[str, float]],
        *,
        clearance_mm: float = 0.3,
        min_width_mm: float = 0.25,
    ) -> dict[str, Any]:
        if len(outline) < 3:
            raise ValueError("A copper zone outline requires at least three points")
        zone = self._pcbnew.ZONE(self.board)
        zone.SetNet(self._ensure_net_item(net_name))
        zone.SetLayer(self._layer_id(layer))
        zone.SetLocalClearance(self._iu(clearance_mm))
        zone.SetMinThickness(self._iu(min_width_mm))
        zone.SetIsFilled(True)
        zone_outline = zone.Outline()
        zone_outline.NewOutline()
        for point in outline:
            zone_outline.Append(self._vec(float(point["x"]), float(point["y"])))
        self.board.Add(zone)
        return {
            "net_name": net_name,
            "layer": layer,
            "point_count": len(outline),
            "clearance_mm": clearance_mm,
            "min_width_mm": min_width_mm,
        }

    def _load_footprint(self, footprint_id: str, footprint_node: Any) -> Any:
        library_path = ""
        footprint_name = ""
        try:
            resolved = resolve_footprint(footprint_id)
            library_path = str(Path(resolved["path"]).parent)
            footprint_name = str(resolved["footprint"])
        except Exception:
            if ":" in footprint_id:
                _library, footprint_name = footprint_id.split(":", 1)
        if not library_path or not footprint_name:
            raise ValueError(f"Footprint could not be resolved for pcbnew: {footprint_id}")
        footprint = self._pcbnew.FootprintLoad(library_path, footprint_name)
        if footprint is None:
            raise ValueError(f"pcbnew failed to load footprint: {footprint_id}")
        return footprint

    def _ensure_net_item(self, net_name: str) -> Any:
        if not net_name:
            raise ValueError("net_name must be non-empty")
        existing = self.board.FindNet(str(net_name))
        if existing is not None:
            return existing
        net = self._pcbnew.NETINFO_ITEM(self.board, str(net_name))
        self.board.Add(net)
        return self.board.FindNet(str(net_name)) or net

    def _layer_id(self, layer: str) -> int:
        try:
            return int(self.board.GetLayerID(str(layer)))
        except Exception:
            attr = str(layer).replace(".", "_").replace("-", "_")
            value = getattr(self._pcbnew, attr, None)
            if value is not None:
                return int(value)
            raise ValueError(f"Unknown PCB layer: {layer}") from None

    def _layer_name(self, layer_id: int) -> str:
        try:
            return str(self.board.GetLayerName(layer_id))
        except Exception:
            return str(layer_id)

    def _footprint_name(self, footprint: Any) -> str:
        try:
            fpid = footprint.GetFPID()
            return str(fpid.GetLibItemName())
        except Exception:
            return str(footprint.GetValue())

    def _vec(self, x: float, y: float) -> Any:
        return self._pcbnew.VECTOR2I(self._iu(x), self._iu(y))

    def _iu(self, value_mm: float) -> int:
        return int(self._pcbnew.FromMM(float(value_mm)))

    def _mm(self, value_iu: int) -> float:
        return float(self._pcbnew.ToMM(value_iu))

    def _position(self, vector: Any) -> dict[str, float]:
        return {"x": self._mm(vector.x), "y": self._mm(vector.y)}


def _box_to_bounds(pcbnew: Any, box: Any) -> dict[str, float]:
    return {
        "left": float(pcbnew.ToMM(box.GetLeft())),
        "top": float(pcbnew.ToMM(box.GetTop())),
        "right": float(pcbnew.ToMM(box.GetRight())),
        "bottom": float(pcbnew.ToMM(box.GetBottom())),
    }
