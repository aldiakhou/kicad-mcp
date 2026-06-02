"""Common PCB backend protocol and factory."""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
from typing import Any, Protocol

from kicad_mcp.utils.pcbnew_runtime import pcbnew_runtime_status


class BoardModel(Protocol):
    """Board model operations used by PCB jobs and MCP tools."""

    backend_name: str
    backend_status: dict[str, Any]

    def save_to(self, pcb_path: str) -> None: ...
    def to_text(self) -> str: ...
    def create_board_outline(self, width_mm: float, height_mm: float) -> dict[str, Any]: ...
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
    ) -> dict[str, Any]: ...
    def move_footprint(
        self, reference: str, x: float, y: float, angle: float | None = None
    ) -> dict[str, Any]: ...
    def assign_footprint_pad_nets(
        self, reference: str, net_assignments: dict[str, str], clear_stale: bool = True
    ) -> dict[str, Any]: ...
    def ensure_net(self, net_name: str) -> int: ...
    def find_footprint(self, reference: str) -> Any | None: ...
    def list_footprints(self) -> list[dict[str, Any]]: ...
    def footprint_bounds(self, footprint: Any) -> dict[str, float]: ...
    def footprint_pad_positions(self) -> list[dict[str, Any]]: ...
    def list_nets(self) -> list[dict[str, Any]]: ...
    def list_track_segments(self) -> list[dict[str, Any]]: ...
    def list_vias(self) -> list[dict[str, Any]]: ...
    def clear_routing(self, *, include_zones: bool = True) -> dict[str, int]: ...
    def add_track(
        self,
        net_name: str,
        points: list[dict[str, float]],
        layer: str = "F.Cu",
        width_mm: float = 0.25,
    ) -> dict[str, Any]: ...
    def add_via(
        self,
        net_name: str,
        x: float,
        y: float,
        drill_mm: float = 0.3,
        diameter_mm: float = 0.6,
    ) -> dict[str, Any]: ...
    def add_zone(
        self,
        net_name: str,
        layer: str,
        outline: list[dict[str, float]],
        *,
        clearance_mm: float = 0.3,
        min_width_mm: float = 0.25,
    ) -> dict[str, Any]: ...


class BoardBackend(Protocol):
    """Factory for loading and creating board models."""

    name: str

    def status(self) -> dict[str, Any]: ...
    def empty(self, board_width_mm: float, board_height_mm: float) -> BoardModel: ...
    def from_file(self, pcb_path: str) -> BoardModel: ...
    def from_text(self, content: str) -> BoardModel: ...


def get_board_backend(preferred: str | None = None) -> BoardBackend:
    """Return the configured board backend, preferring pcbnew when available."""
    preference = (preferred or os.getenv("KICAD_MCP_PCB_BACKEND") or "auto").strip().lower()
    if preference in {"pcbnew", "auto"}:
        status = pcbnew_runtime_status()
        if status.get("available"):
            from kicad_mcp.pcb_engine.backends.pcbnew_backend import PcbnewBoardBackend

            return PcbnewBoardBackend(status)
        if preference == "pcbnew":
            raise RuntimeError(status.get("error") or "pcbnew backend is unavailable")
    from kicad_mcp.pcb_engine.backends.sexpr_backend import SexprBoardBackend

    return SexprBoardBackend()


def attach_backend_metadata(model: Any, backend_name: str, status: dict[str, Any]) -> Any:
    """Attach backend metadata to an existing model object."""
    model.backend_name = backend_name
    model.backend_status = status
    if not hasattr(model, "save_to"):
        model.save_to = lambda pcb_path: Path(pcb_path).write_text(
            model.to_text(),
            encoding="utf-8",
        )
    if not hasattr(model, "add_zone"):
        model.add_zone = _unsupported_zone
    return model


def _unsupported_zone(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise NotImplementedError("Copper zones require the pcbnew backend")


def run_with_board_transaction(
    pcb_path: str,
    mutator: Callable[[BoardModel], dict[str, Any]],
    *,
    backend: BoardBackend | None = None,
) -> tuple[dict[str, Any], BoardBackend]:
    """Load a board with the backend, run a mutator, and return changed objects."""
    resolved_backend = backend or get_board_backend()
    model = resolved_backend.from_file(pcb_path)
    return mutator(model), resolved_backend
