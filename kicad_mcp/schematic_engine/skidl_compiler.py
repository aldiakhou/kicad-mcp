"""SKiDL circuit compiler.

Converts a CanonicalCircuit into a SKiDL circuit, runs ERC, and generates
the expected netlist as the ground truth for verification.

Requires optional dependency: skidl>=2.2.3
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import json
import logging
import os
from typing import Any

from kicad_mcp.schematic_engine.library_map import resolve_lib_id
from kicad_mcp.schematic_engine.models import CanonicalCircuit, NetlistEntry, NormalizedNetlist

logger = logging.getLogger(__name__)


@dataclass
class SkidlCompileResult:
    """Result of SKiDL compilation."""

    success: bool
    error: str | None = None
    expected_netlist: NormalizedNetlist | None = None
    expected_netlist_path: str | None = None
    skidl_netlist_path: str | None = None
    skidl_resolved_netlist: NormalizedNetlist | None = None
    verification_quality: str = "canonical_only"
    part_count: int = 0
    net_count: int = 0
    endpoint_count: int = 0
    erc_warnings: list[str] = field(default_factory=list)
    erc_errors: list[str] = field(default_factory=list)


# Check if skidl is available
_SKIDL_AVAILABLE = False
try:
    import skidl  # noqa: F401
    _SKIDL_AVAILABLE = True
except ImportError:
    pass


class SkidlCompiler:
    """Compiles a CanonicalCircuit using SKiDL for netlist generation and ERC.
    """

    def __init__(self, artifact_dir: str | None = None):
        """Initialize the compiler.

        Args:
            artifact_dir: Directory to write netlist artifacts. If None, uses
                         a temp directory.
        """
        self.artifact_dir = artifact_dir

    def compile(self, canonical: CanonicalCircuit) -> SkidlCompileResult:
        """Compile a CanonicalCircuit to produce expected netlist.

        Args:
            canonical: The canonical circuit representation.

        Returns:
            SkidlCompileResult with expected netlist and diagnostics.
        """
        if not _SKIDL_AVAILABLE:
            raise RuntimeError(
                "SKiDL is required. Install kicad-mcp with required dependencies."
            )
        return self._compile_with_skidl(canonical)

    def _compile_fallback(self, canonical: CanonicalCircuit) -> SkidlCompileResult:
        """Pure-Python fallback: build expected netlist from canonical endpoints.

        This provides the same netlist output without SKiDL's ERC checking.
        """
        try:
            nets: dict[str, set[NetlistEntry]] = defaultdict(set)

            for endpoint in canonical.endpoints:
                entry = NetlistEntry(ref=endpoint.ref, pin=endpoint.pin)
                nets[endpoint.net].add(entry)

            # Remove single-endpoint nets (these are unconnected)
            # But keep them for verification purposes
            expected = NormalizedNetlist(nets=dict(nets))

            # Save artifacts
            netlist_path = self._save_expected_netlist(canonical, expected)

            return SkidlCompileResult(
                success=True,
                expected_netlist=expected,
                expected_netlist_path=netlist_path,
                part_count=len(canonical.parts),
                net_count=len(expected.nets),
                endpoint_count=len(canonical.endpoints),
                erc_warnings=["SKiDL not installed; ERC not performed"],
            )
        except Exception as e:
            return SkidlCompileResult(
                success=False,
                error=f"Fallback netlist compilation failed: {e}",
            )

    def _compile_with_skidl(self, canonical: CanonicalCircuit) -> SkidlCompileResult:
        """Compile using SKiDL for full ERC and netlist generation."""
        try:
            from skidl import ERC as run_erc
            from skidl import KICAD8, Circuit, Net, Part

            circuit = Circuit()
            parts_by_ref: dict[str, Any] = {}
            nets_by_name: dict[str, Any] = {}
            erc_warnings: list[str] = []
            erc_errors: list[str] = []

            # Create parts
            parts_failed = 0
            for part_def in canonical.parts:
                lib, name = resolve_lib_id(part_def.lib_id)
                try:
                    skidl_part = Part(
                        lib,
                        name,
                        ref=part_def.ref,
                        value=part_def.value,
                        footprint=part_def.footprint or "",
                        circuit=circuit,
                    )
                    parts_by_ref[part_def.ref] = skidl_part
                except Exception as e:
                    parts_failed += 1
                    erc_warnings.append(
                        f"Could not create SKiDL part {part_def.ref} "
                        f"({part_def.lib_id}): {e}"
                    )

            # If most parts failed to load, SKiDL libraries are not available
            # Fall back to pure-Python netlist generation
            if parts_failed > 0 and parts_failed >= len(canonical.parts) * 0.5:
                return SkidlCompileResult(
                    success=False,
                    error="SKiDL library loading failed for most parts",
                    erc_errors=["Part not found" for _ in range(parts_failed)],
                    erc_warnings=erc_warnings,
                )

            # Create nets and connect endpoints
            for endpoint in canonical.endpoints:
                if endpoint.ref not in parts_by_ref:
                    if endpoint.required:
                        erc_errors.append(
                            f"Part {endpoint.ref} not found for endpoint "
                            f"{endpoint.ref}.{endpoint.pin} -> {endpoint.net}"
                        )
                    continue

                part = parts_by_ref[endpoint.ref]
                if endpoint.net not in nets_by_name:
                    nets_by_name[endpoint.net] = Net(endpoint.net, circuit=circuit)

                net = nets_by_name[endpoint.net]
                try:
                    pin = part[endpoint.pin]
                    net += pin
                except Exception as e:
                    if endpoint.required and not endpoint.allow_hidden:
                        erc_errors.append(
                            f"Pin {endpoint.pin} not found on {endpoint.ref}: {e}"
                        )
                    else:
                        erc_warnings.append(
                            f"Optional pin {endpoint.pin} on {endpoint.ref} "
                            f"not resolved: {e}"
                        )

            # Run ERC
            try:
                run_erc()
            except Exception as e:
                erc_warnings.append(f"SKiDL ERC exception: {e}")

            # Generate expected netlist from canonical (source of truth for comparison)
            nets_dict: dict[str, set[NetlistEntry]] = defaultdict(set)
            for endpoint in canonical.endpoints:
                entry = NetlistEntry(ref=endpoint.ref, pin=endpoint.pin)
                nets_dict[endpoint.net].add(entry)

            expected = NormalizedNetlist(nets=dict(nets_dict))

            # Generate SKiDL-resolved netlist from the actual SKiDL circuit
            # This reflects what SKiDL actually connected (may differ from canonical)
            skidl_resolved: NormalizedNetlist | None = None
            verification_quality = "canonical_only"
            try:
                skidl_nets_dict: dict[str, set[NetlistEntry]] = defaultdict(set)
                for net_name, net_obj in nets_by_name.items():
                    for pin in net_obj.pins:
                        ref_str = pin.part.ref if hasattr(pin, "part") else ""
                        pin_name = pin.name if hasattr(pin, "name") else ""
                        if ref_str and pin_name:
                            skidl_nets_dict[net_name].add(
                                NetlistEntry(ref=ref_str, pin=pin_name)
                            )
                if skidl_nets_dict:
                    skidl_resolved = NormalizedNetlist(nets=dict(skidl_nets_dict))
                    verification_quality = "skidl_verified"
            except Exception as e:
                erc_warnings.append(f"SKiDL resolved netlist extraction failed: {e}")

            # Save SKiDL netlist
            skidl_netlist_path: str | None = None
            if self.artifact_dir:
                os.makedirs(self.artifact_dir, exist_ok=True)
                skidl_netlist_path = os.path.join(self.artifact_dir, "expected.net")
                try:
                    circuit.generate_netlist(tool=KICAD8, file_=skidl_netlist_path)
                except Exception as e:
                    erc_warnings.append(f"SKiDL netlist export failed: {e}")
                    skidl_netlist_path = None

            netlist_path = self._save_expected_netlist(canonical, expected)

            return SkidlCompileResult(
                success=len(erc_errors) == 0,
                expected_netlist=expected,
                expected_netlist_path=netlist_path,
                skidl_netlist_path=skidl_netlist_path,
                skidl_resolved_netlist=skidl_resolved,
                verification_quality=verification_quality,
                part_count=len(canonical.parts),
                net_count=len(expected.nets),
                endpoint_count=len(canonical.endpoints),
                erc_warnings=erc_warnings,
                erc_errors=erc_errors,
                error="; ".join(erc_errors) if erc_errors else None,
            )
        except ImportError as e:
            return SkidlCompileResult(
                success=False,
                error=f"SKiDL import failed: {e}",
            )
        except Exception as e:
            return SkidlCompileResult(
                success=False,
                error=f"SKiDL compilation failed: {e}",
            )

    def _save_expected_netlist(
        self,
        canonical: CanonicalCircuit,
        netlist: NormalizedNetlist,
    ) -> str | None:
        """Save expected netlist to JSON artifact."""
        if not self.artifact_dir:
            # Use project's .kicad_mcp directory
            project_dir = os.path.dirname(canonical.project_path)
            self.artifact_dir = os.path.join(project_dir, ".kicad_mcp")

        os.makedirs(self.artifact_dir, exist_ok=True)
        path = os.path.join(self.artifact_dir, "expected_netlist.json")

        try:
            data = {
                "nets": netlist.to_dict(),
                "metadata": {
                    "part_count": len(canonical.parts),
                    "net_count": len(netlist.nets),
                    "endpoint_count": len(canonical.endpoints),
                    "no_connect_count": len(canonical.no_connects),
                },
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            return path
        except Exception as e:
            logger.warning("Failed to save expected netlist: %s", e)
            return None
