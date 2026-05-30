"""Stable internal data models for the schematic engine.

The internal source of truth is parts + exact net endpoints, not placed
schematic objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CircuitPart:
    """A component in the canonical circuit."""

    ref: str
    lib_id: str
    value: str
    footprint: str | None = None
    block: str = "default"
    role: str | None = None
    properties: dict[str, str] = field(default_factory=dict)


@dataclass
class CircuitEndpoint:
    """A pin-to-net connection in the canonical circuit."""

    ref: str
    pin: str
    net: str
    required: bool = True
    allow_hidden: bool = False
    source: str | None = None


@dataclass
class CanonicalCircuit:
    """The canonical representation of a circuit design.

    This is the single source of truth for what is electrically connected.
    """

    project_path: str
    parts: list[CircuitPart]
    endpoints: list[CircuitEndpoint]
    no_connects: list[tuple[str, str]]
    blocks: dict[str, list[str]]
    rails: set[str] = field(default_factory=set)

    def part_by_ref(self, ref: str) -> CircuitPart | None:
        """Look up a part by reference designator."""
        for part in self.parts:
            if part.ref == ref:
                return part
        return None

    def endpoints_for_net(self, net: str) -> list[CircuitEndpoint]:
        """Get all endpoints on a given net."""
        return [ep for ep in self.endpoints if ep.net == net]

    def endpoints_for_ref(self, ref: str) -> list[CircuitEndpoint]:
        """Get all endpoints for a given reference designator."""
        return [ep for ep in self.endpoints if ep.ref == ref]

    def net_names(self) -> set[str]:
        """Get all unique net names."""
        return {ep.net for ep in self.endpoints}


@dataclass
class PlacementInfo:
    """Placement coordinates and metadata for a symbol."""

    ref: str
    x: float
    y: float
    angle: float = 0.0
    mirror: bool = False
    sheet: str = "root"


@dataclass
class SheetPlan:
    """Plan for how parts should be distributed across schematic sheets."""

    sheets: dict[str, list[str]]  # sheet_name -> list of refs
    placements: dict[str, PlacementInfo]  # ref -> placement
    sheet_sizes: dict[str, str]  # sheet_name -> paper size (e.g., "A3")
    cross_sheet_nets: set[str] = field(default_factory=set)
    local_nets: dict[str, set[str]] = field(default_factory=dict)  # sheet -> local nets


@dataclass
class NetlistEntry:
    """A single endpoint in a normalized netlist."""

    ref: str
    pin: str

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, NetlistEntry):
            return NotImplemented
        return self.ref == other.ref and self.pin == other.pin

    def __hash__(self) -> int:
        return hash((self.ref, self.pin))


@dataclass
class NormalizedNetlist:
    """A normalized netlist for comparison purposes."""

    nets: dict[str, set[NetlistEntry]]

    def to_dict(self) -> dict[str, list[dict[str, str]]]:
        """Convert to serializable dict."""
        return {
            net: sorted(
                [{"ref": ep.ref, "pin": ep.pin} for ep in endpoints],
                key=lambda x: (x["ref"], x["pin"]),
            )
            for net, endpoints in sorted(self.nets.items())
        }

    @classmethod
    def from_dict(cls, data: dict[str, list[dict[str, str]]]) -> NormalizedNetlist:
        """Create from serialized dict."""
        nets: dict[str, set[NetlistEntry]] = {}
        for net_name, entries in data.items():
            nets[net_name] = {
                NetlistEntry(ref=e["ref"], pin=e["pin"]) for e in entries
            }
        return cls(nets=nets)


@dataclass
class NetlistCompareResult:
    """Result of comparing expected vs actual netlists."""

    success: bool
    missing_endpoints: list[dict[str, str]]
    extra_endpoints: list[dict[str, str]]
    mismatched_nets: list[dict[str, Any]]
    expected_net_count: int = 0
    actual_net_count: int = 0


@dataclass
class VisualLintIssue:
    """A single visual lint issue."""

    type: str
    ref: str | None = None
    label: str | None = None
    sheet: str | None = None
    severity: str = "blocking"
    message: str = ""


@dataclass
class VisualLintResult:
    """Result of visual lint checks."""

    success: bool
    blocking_count: int
    warning_count: int
    issues: list[VisualLintIssue]


@dataclass
class SupportCircuitSpec:
    """Specification for a support circuit (decoupling, crystal, etc.)."""

    type: str
    target: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)
