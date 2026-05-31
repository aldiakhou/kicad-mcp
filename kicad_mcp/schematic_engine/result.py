"""Engine result type for schematic generation pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EngineResult:
    """Result of the full schematic engine pipeline.

    Contains all outputs from each pipeline stage for inspection and
    commit/rollback decisions.
    """

    success: bool
    changed: bool = False
    rolled_back: bool = False
    engine: str = "skidl_kiutils_kicad_cli"
    stage: str = "unknown"
    error: str | None = None
    project_path: str | None = None

    # Stage outputs
    sheets: list[str] = field(default_factory=list)
    expected_netlist_match: bool | None = None
    erc: dict[str, Any] = field(default_factory=dict)
    visual_lint: dict[str, Any] = field(default_factory=dict)
    netlist_compare: dict[str, Any] = field(default_factory=dict)

    # Artifact paths
    artifact_dir: str | None = None
    expected_netlist_path: str | None = None
    kicad_netlist_path: str | None = None
    svg_dir: str | None = None
    erc_path: str | None = None
    generated_schematic_artifacts: list[str] = field(default_factory=list)

    # Counts
    part_count: int = 0
    net_count: int = 0
    endpoint_count: int = 0
    output_symbol_count: int = 0
    intent_action: str | None = None
    intent_state_path: str | None = None

    # Job progress
    progress: dict[str, Any] = field(default_factory=dict)

    # Recommended next
    recommended_next_tool: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to MCP tool response dict."""
        result: dict[str, Any] = {
            "success": self.success,
            "changed": self.changed,
            "engine": self.engine,
            "stage": self.stage,
        }
        if self.error:
            result["error"] = self.error
        if self.project_path:
            result["project_path"] = self.project_path
        if self.rolled_back:
            result["rolled_back"] = self.rolled_back
        if self.sheets:
            result["sheets"] = self.sheets
        if self.expected_netlist_match is not None:
            result["expected_netlist_match"] = self.expected_netlist_match
        if self.erc:
            result["erc"] = self.erc
        if self.visual_lint:
            result["visual_lint"] = self.visual_lint
        if self.netlist_compare:
            result["netlist_compare"] = self.netlist_compare
        if self.artifact_dir:
            result["artifact_dir"] = self.artifact_dir
        if self.expected_netlist_path:
            result["expected_netlist_path"] = self.expected_netlist_path
        if self.kicad_netlist_path:
            result["kicad_netlist_path"] = self.kicad_netlist_path
        if self.svg_dir:
            result["svg_dir"] = self.svg_dir
        if self.erc_path:
            result["erc_path"] = self.erc_path
        if self.generated_schematic_artifacts:
            result["generated_schematic_artifacts"] = self.generated_schematic_artifacts
        if self.part_count:
            result["part_count"] = self.part_count
            result["net_count"] = self.net_count
            result["endpoint_count"] = self.endpoint_count
        result["output_symbol_count"] = self.output_symbol_count
        if self.intent_action:
            result["intent_action"] = self.intent_action
        if self.intent_state_path:
            result["intent_state_path"] = self.intent_state_path
        if self.recommended_next_tool:
            result["recommended_next_tool"] = self.recommended_next_tool
        return result
