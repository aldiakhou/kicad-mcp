"""Fallback PCB backend using the existing S-expression editor."""

from __future__ import annotations

from typing import Any

from kicad_mcp.pcb_engine.backends.base import BoardModel, attach_backend_metadata
from kicad_mcp.utils.kicad_pcb_s_expr import KiCadPcb


class SexprBoardBackend:
    """Board backend backed by the local KiCadPcb S-expression helper."""

    name = "sexpr"

    def status(self) -> dict[str, Any]:
        return {
            "success": True,
            "available": True,
            "backend": self.name,
            "fallback": True,
            "reason": "Using internal S-expression PCB backend",
        }

    def empty(self, board_width_mm: float, board_height_mm: float) -> BoardModel:
        return _attach(KiCadPcb.empty(board_width_mm, board_height_mm))

    def from_file(self, pcb_path: str) -> BoardModel:
        return _attach(KiCadPcb.from_file(pcb_path))

    def from_text(self, content: str) -> BoardModel:
        return _attach(KiCadPcb.from_text(content))


def _attach(model: KiCadPcb) -> BoardModel:
    return attach_backend_metadata(model, SexprBoardBackend.name, SexprBoardBackend().status())
