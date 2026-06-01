"""Bounded grid autorouter for agent-safe PCB layout jobs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import heapq
import math
from typing import Any

from kicad_mcp.utils.kicad_pcb_s_expr import KiCadPcb


@dataclass(frozen=True)
class Rect:
    left: float
    top: float
    right: float
    bottom: float

    def inflated(self, amount: float) -> Rect:
        return Rect(
            self.left - amount,
            self.top - amount,
            self.right + amount,
            self.bottom + amount,
        )

    def contains(self, point: dict[str, float]) -> bool:
        return self.left <= point["x"] <= self.right and self.top <= point["y"] <= self.bottom


def autoroute_pcb(
    pcb: KiCadPcb,
    board_width_mm: float,
    board_height_mm: float,
    *,
    layer: str = "F.Cu",
    track_width_mm: float = 0.25,
    clearance_mm: float = 0.35,
    grid_mm: float = 1.27,
    max_connections: int | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Route currently-unconnected assigned pads using a conservative grid router."""
    if track_width_mm <= 0:
        raise ValueError("routing.track_width_mm must be positive")
    if clearance_mm < 0:
        raise ValueError("routing.clearance_mm must be zero or positive")
    if grid_mm <= 0:
        raise ValueError("routing.grid_mm must be positive")

    connections = _unrouted_connections(pcb)
    if max_connections is not None:
        connections = connections[: max(0, int(max_connections))]
    routes = []
    failures = []
    for index, connection in enumerate(connections):
        if cancel_check and cancel_check():
            return {
                "success": False,
                "cancelled": True,
                "stage": "autoroute_cancelled",
                "attempted_count": index,
                "routed_count": len(routes),
                "failed_count": len(failures),
                "routes": routes,
                "failures": failures,
            }
        obstacles = _routing_obstacles(
            pcb,
            clearance_mm + track_width_mm / 2.0,
            excluded_refs={
                connection["from"]["reference"],
                connection["to"]["reference"],
            },
            excluded_net=connection["net_name"],
        )
        path = _route_grid_path(
            connection["from"]["position"],
            connection["to"]["position"],
            board_width_mm,
            board_height_mm,
            obstacles,
            grid_mm,
            clearance_mm + track_width_mm / 2.0,
        )
        if path is None:
            failures.append(
                {
                    "net_name": connection["net_name"],
                    "from": connection["from"],
                    "to": connection["to"],
                    "reason": "No obstacle-free grid path found",
                }
            )
            continue
        route = pcb.add_track(connection["net_name"], path, layer=layer, width_mm=track_width_mm)
        routes.append(
            {
                "net_name": connection["net_name"],
                "from": connection["from"],
                "to": connection["to"],
                "point_count": len(path),
                "segment_count": len(route.get("segments", [])),
                "layer": layer,
                "track_width_mm": track_width_mm,
            }
        )
    return {
        "success": True,
        "stage": "autorouted" if not failures else "autorouted_partial",
        "attempted_count": len(connections),
        "routed_count": len(routes),
        "failed_count": len(failures),
        "routes": routes,
        "failures": failures,
        "rules": {
            "layer": layer,
            "track_width_mm": track_width_mm,
            "clearance_mm": clearance_mm,
            "grid_mm": grid_mm,
            "max_connections": max_connections,
        },
    }


def _unrouted_connections(pcb: KiCadPcb) -> list[dict[str, Any]]:
    pads_by_net: dict[str, list[dict[str, Any]]] = {}
    for pad in pcb.footprint_pad_positions():
        if pad.get("net_name"):
            pads_by_net.setdefault(str(pad["net_name"]), []).append(pad)

    connections = []
    for net_name, pads in sorted(pads_by_net.items()):
        if len(pads) < 2:
            continue
        components = _pad_components(pads, _segments_for_net(pcb, net_name))
        connected: list[dict[str, Any]] = [pads[0]]
        connected_components = {components[_pad_key(pads[0])]}
        remaining = [pad for pad in pads[1:] if components[_pad_key(pad)] not in connected_components]
        while remaining:
            start, end = _nearest_pair(connected, remaining)
            connections.append(
                {
                    "net_name": net_name,
                    "from": _pad_endpoint(start),
                    "to": _pad_endpoint(end),
                }
            )
            connected.append(end)
            connected_components.add(components[_pad_key(end)])
            remaining = [
                pad
                for pad in pads
                if components[_pad_key(pad)] not in connected_components
            ]
    return connections


def _segments_for_net(pcb: KiCadPcb, net_name: str) -> list[dict[str, Any]]:
    return [segment for segment in pcb.list_track_segments() if segment.get("net_name") == net_name]


def _pad_components(
    pads: list[dict[str, Any]],
    track_segments: list[dict[str, Any]],
) -> dict[tuple[str, str], tuple[int, int]]:
    parent: dict[tuple[int, int], tuple[int, int]] = {}

    def coord_key(point: dict[str, float]) -> tuple[int, int]:
        return (round(float(point["x"]) / 0.05), round(float(point["y"]) / 0.05))

    def find(node: tuple[int, int]) -> tuple[int, int]:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: tuple[int, int], b: tuple[int, int]) -> None:
        root_a = find(a)
        root_b = find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    pad_nodes = {_pad_key(pad): coord_key(pad["position"]) for pad in pads}
    for node in pad_nodes.values():
        find(node)
    for segment in track_segments:
        union(coord_key(segment["start"]), coord_key(segment["end"]))
    return {key: find(node) for key, node in pad_nodes.items()}


def _routing_obstacles(
    pcb: KiCadPcb,
    inflate_mm: float,
    *,
    excluded_refs: set[str],
    excluded_net: str,
) -> list[Rect]:
    obstacles = []
    for footprint in pcb.list_footprints():
        if str(footprint.get("reference", "")) in excluded_refs:
            continue
        bounds = footprint.get("bounds", {})
        if bounds:
            obstacles.append(
                Rect(
                    float(bounds["left"]),
                    float(bounds["top"]),
                    float(bounds["right"]),
                    float(bounds["bottom"]),
                ).inflated(inflate_mm)
            )
    for segment in pcb.list_track_segments():
        if segment.get("net_name") == excluded_net:
            continue
        obstacles.append(_segment_rect(segment, inflate_mm))
    for via in pcb.list_vias():
        if via.get("net_name") == excluded_net:
            continue
        point = via["position"]
        radius = max(float(via.get("diameter_mm") or 0.0) / 2.0, inflate_mm)
        obstacles.append(
            Rect(point["x"] - radius, point["y"] - radius, point["x"] + radius, point["y"] + radius)
        )
    return obstacles


def _route_grid_path(
    start: dict[str, float],
    end: dict[str, float],
    board_width_mm: float,
    board_height_mm: float,
    obstacles: list[Rect],
    grid_mm: float,
    margin_mm: float,
) -> list[dict[str, float]] | None:
    min_x = max(margin_mm, 0.25)
    min_y = max(margin_mm, 0.25)
    max_x = max(min_x, board_width_mm - margin_mm)
    max_y = max(min_y, board_height_mm - margin_mm)
    start_cell = _nearest_free_cell(start, min_x, min_y, max_x, max_y, grid_mm, obstacles)
    end_cell = _nearest_free_cell(end, min_x, min_y, max_x, max_y, grid_mm, obstacles)
    if start_cell is None or end_cell is None:
        return None

    cell_path = _astar(start_cell, end_cell, min_x, min_y, max_x, max_y, grid_mm, obstacles)
    if not cell_path:
        return None
    grid_points = [_cell_point(cell, min_x, min_y, grid_mm) for cell in cell_path]
    compressed = _compress_points(grid_points)
    return _dedupe_points(
        _endpoint_bridge(start, compressed[0])
        + compressed[1:-1]
        + _endpoint_bridge(compressed[-1], end)
    )


def _astar(
    start: tuple[int, int],
    end: tuple[int, int],
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
    grid_mm: float,
    obstacles: list[Rect],
) -> list[tuple[int, int]] | None:
    max_ix = int(math.floor((max_x - min_x) / grid_mm))
    max_iy = int(math.floor((max_y - min_y) / grid_mm))
    queue: list[tuple[float, int, tuple[int, int]]] = [(0.0, 0, start)]
    came_from: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
    cost: dict[tuple[int, int], float] = {start: 0.0}
    counter = 0
    while queue and len(came_from) < 50000:
        _, _, current = heapq.heappop(queue)
        if current == end:
            return _reconstruct_path(came_from, current)
        for neighbor in _neighbors(current, max_ix, max_iy):
            if _cell_blocked(neighbor, min_x, min_y, grid_mm, obstacles):
                continue
            if _edge_blocked(current, neighbor, min_x, min_y, grid_mm, obstacles):
                continue
            new_cost = cost[current] + grid_mm
            if new_cost >= cost.get(neighbor, float("inf")):
                continue
            cost[neighbor] = new_cost
            counter += 1
            priority = new_cost + _manhattan_cells(neighbor, end) * grid_mm
            heapq.heappush(queue, (priority, counter, neighbor))
            came_from[neighbor] = current
    return None


def _nearest_free_cell(
    point: dict[str, float],
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
    grid_mm: float,
    obstacles: list[Rect],
) -> tuple[int, int] | None:
    base = (
        int(round((min(max(point["x"], min_x), max_x) - min_x) / grid_mm)),
        int(round((min(max(point["y"], min_y), max_y) - min_y) / grid_mm)),
    )
    max_ix = int(math.floor((max_x - min_x) / grid_mm))
    max_iy = int(math.floor((max_y - min_y) / grid_mm))
    for radius in range(0, 12):
        candidates = []
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                if max(abs(dx), abs(dy)) != radius:
                    continue
                cell = (base[0] + dx, base[1] + dy)
                if 0 <= cell[0] <= max_ix and 0 <= cell[1] <= max_iy:
                    candidates.append(cell)
        candidates.sort(key=lambda cell: _manhattan_cells(cell, base))
        for cell in candidates:
            if not _cell_blocked(cell, min_x, min_y, grid_mm, obstacles):
                return cell
    return None


def _cell_blocked(
    cell: tuple[int, int],
    min_x: float,
    min_y: float,
    grid_mm: float,
    obstacles: list[Rect],
) -> bool:
    point = _cell_point(cell, min_x, min_y, grid_mm)
    return any(obstacle.contains(point) for obstacle in obstacles)


def _edge_blocked(
    start: tuple[int, int],
    end: tuple[int, int],
    min_x: float,
    min_y: float,
    grid_mm: float,
    obstacles: list[Rect],
) -> bool:
    start_point = _cell_point(start, min_x, min_y, grid_mm)
    end_point = _cell_point(end, min_x, min_y, grid_mm)
    midpoint = {
        "x": (start_point["x"] + end_point["x"]) / 2.0,
        "y": (start_point["y"] + end_point["y"]) / 2.0,
    }
    return any(obstacle.contains(midpoint) for obstacle in obstacles)


def _neighbors(cell: tuple[int, int], max_ix: int, max_iy: int) -> list[tuple[int, int]]:
    x, y = cell
    candidates = ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))
    return [(nx, ny) for nx, ny in candidates if 0 <= nx <= max_ix and 0 <= ny <= max_iy]


def _cell_point(cell: tuple[int, int], min_x: float, min_y: float, grid_mm: float) -> dict[str, float]:
    return {
        "x": round(min_x + cell[0] * grid_mm, 6),
        "y": round(min_y + cell[1] * grid_mm, 6),
    }


def _reconstruct_path(
    came_from: dict[tuple[int, int], tuple[int, int] | None],
    current: tuple[int, int],
) -> list[tuple[int, int]]:
    path = [current]
    while came_from[current] is not None:
        current = came_from[current]  # type: ignore[assignment]
        path.append(current)
    path.reverse()
    return path


def _endpoint_bridge(start: dict[str, float], end: dict[str, float]) -> list[dict[str, float]]:
    if _same_point(start, end):
        return [start]
    if start["x"] == end["x"] or start["y"] == end["y"]:
        return [start, end]
    return [start, {"x": end["x"], "y": start["y"]}, end]


def _compress_points(points: list[dict[str, float]]) -> list[dict[str, float]]:
    if len(points) <= 2:
        return points
    compressed = [points[0]]
    for previous, current, following in zip(points, points[1:], points[2:]):
        horizontal = previous["y"] == current["y"] == following["y"]
        vertical = previous["x"] == current["x"] == following["x"]
        if not horizontal and not vertical:
            compressed.append(current)
    compressed.append(points[-1])
    return compressed


def _dedupe_points(points: list[dict[str, float]]) -> list[dict[str, float]]:
    deduped = []
    for point in points:
        normalized = {"x": round(float(point["x"]), 6), "y": round(float(point["y"]), 6)}
        if not deduped or not _same_point(deduped[-1], normalized):
            deduped.append(normalized)
    return deduped


def _nearest_pair(
    connected: list[dict[str, Any]],
    remaining: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    pairs = [
        (_distance(a["position"], b["position"]), a, b)
        for a in connected
        for b in remaining
    ]
    _, start, end = min(pairs, key=lambda item: item[0])
    return start, end


def _segment_rect(segment: dict[str, Any], inflate_mm: float) -> Rect:
    start = segment["start"]
    end = segment["end"]
    return Rect(
        min(start["x"], end["x"]) - inflate_mm,
        min(start["y"], end["y"]) - inflate_mm,
        max(start["x"], end["x"]) + inflate_mm,
        max(start["y"], end["y"]) + inflate_mm,
    )


def _pad_endpoint(pad: dict[str, Any]) -> dict[str, Any]:
    return {
        "reference": pad["reference"],
        "pad": pad["pad"],
        "position": pad["position"],
    }


def _pad_key(pad: dict[str, Any]) -> tuple[str, str]:
    return str(pad.get("reference", "")), str(pad.get("pad", ""))


def _distance(a: dict[str, float], b: dict[str, float]) -> float:
    return abs(float(a["x"]) - float(b["x"])) + abs(float(a["y"]) - float(b["y"]))


def _manhattan_cells(a: tuple[int, int], b: tuple[int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _same_point(a: dict[str, float], b: dict[str, float]) -> bool:
    return abs(float(a["x"]) - float(b["x"])) < 1e-6 and abs(float(a["y"]) - float(b["y"])) < 1e-6
