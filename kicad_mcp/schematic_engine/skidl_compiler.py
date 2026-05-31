"""SKiDL circuit compiler.

Converts a CanonicalCircuit into a SKiDL circuit, runs ERC, and generates
the expected netlist as the ground truth for verification.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import json
import logging
import os
from typing import Any

from kicad_mcp.schematic_engine.custom_symbols import is_custom_lib_id
from kicad_mcp.schematic_engine.library_map import resolve_lib_id
from kicad_mcp.schematic_engine.models import CanonicalCircuit, NetlistEntry, NormalizedNetlist
from kicad_mcp.utils.schematic_pins import _resolve_symbol_pins

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

    def _compile_with_skidl(self, canonical: CanonicalCircuit) -> SkidlCompileResult:
        """Compile using SKiDL for full ERC and netlist generation."""
        try:
            from skidl import ERC as run_erc
            from skidl import KICAD8, Circuit, Net, Part

            circuit = Circuit()
            parts_by_ref: dict[str, Any] = {}
            part_defs_by_ref = {part.ref: part for part in canonical.parts}
            pin_aliases_by_ref: dict[str, dict[str, str]] = {}
            nets_by_name: dict[str, Any] = {}
            erc_warnings: list[str] = []
            erc_errors: list[str] = []

            # Create parts
            parts_failed = 0
            custom_refs: set[str] = set()
            for part_def in canonical.parts:
                if is_custom_lib_id(part_def.lib_id):
                    custom_refs.add(part_def.ref)
                    erc_warnings.append(
                        f"Using inline custom symbol for {part_def.ref}; "
                        "KiCad CLI netlist export is authoritative for this part"
                    )
                    continue
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
                    pin_aliases_by_ref[part_def.ref] = _pin_alias_lookup(part_def.lib_id)
                except Exception as e:
                    parts_failed += 1
                    erc_warnings.append(
                        f"Could not create SKiDL part {part_def.ref} "
                        f"({part_def.lib_id}): {e}"
                    )

            # If most parts failed to load, SKiDL libraries are not available
            # Fall back to pure-Python netlist generation
            library_part_count = max(0, len(canonical.parts) - len(custom_refs))
            if (
                library_part_count > 0
                and parts_failed > 0
                and parts_failed >= library_part_count * 0.5
            ):
                return SkidlCompileResult(
                    success=False,
                    error="SKiDL library loading failed for most parts",
                    erc_errors=["Part not found" for _ in range(parts_failed)],
                    erc_warnings=erc_warnings,
                )

            # Create nets and connect endpoints
            for endpoint in canonical.endpoints:
                if endpoint.ref in custom_refs:
                    continue
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
                    continue
                except Exception as original_error:
                    last_error: Exception = original_error
                    fallback_selector = pin_aliases_by_ref.get(endpoint.ref, {}).get(
                        _pin_lookup_key(endpoint.pin)
                    )
                    if fallback_selector and fallback_selector != endpoint.pin:
                        try:
                            pin = part[fallback_selector]
                            net += pin
                            continue
                        except Exception as alias_error:
                            last_error = alias_error
                    if endpoint.required and not endpoint.allow_hidden:
                        erc_errors.append(
                            _pin_not_found_message(
                                endpoint.ref,
                                endpoint.pin,
                                part_defs_by_ref.get(endpoint.ref).lib_id
                                if part_defs_by_ref.get(endpoint.ref)
                                else "",
                                last_error,
                            )
                        )
                    else:
                        erc_warnings.append(
                            f"Optional pin {endpoint.pin} on {endpoint.ref} "
                            f"not resolved: {last_error}"
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


def _pin_alias_lookup(lib_id: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    try:
        pins = _resolve_symbol_pins(lib_id)
    except Exception:
        return aliases
    for pin in pins:
        number = str(pin.get("number") or "")
        name = str(pin.get("name") or "")
        pinfunction = str(pin.get("pinfunction") or "")
        selector = number or name or pinfunction
        candidates = [
            number,
            name,
            pinfunction,
            *_pin_aliases(name, number),
            *_pin_aliases(pinfunction, number),
        ]
        for candidate in candidates:
            key = _pin_lookup_key(candidate)
            if key and selector:
                aliases.setdefault(key, selector)
    return aliases


def _pin_aliases(pin_name: str, pin_number: str = "") -> set[str]:
    aliases: set[str] = set()
    raw = str(pin_name or "")
    if not raw:
        return aliases
    aliases.add(raw)
    if pin_number:
        suffix = f"_{pin_number}"
        if raw.endswith(suffix) and len(raw) > len(suffix):
            aliases.add(raw[: -len(suffix)])
    for candidate in list(aliases):
        stripped = (
            candidate.replace("~{", "")
            .replace("}", "")
            .replace("{", "")
            .replace("~", "")
            .replace("/", "")
        )
        if stripped:
            aliases.add(stripped)
    return aliases


def _pin_lookup_key(value: str) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _pin_not_found_message(
    ref: str,
    pin: str,
    lib_id: str,
    error: Exception,
) -> str:
    available: list[str] = []
    if lib_id:
        try:
            for symbol_pin in _resolve_symbol_pins(lib_id):
                name = str(symbol_pin.get("name") or "")
                number = str(symbol_pin.get("number") or "")
                if name and number:
                    available.append(f"{name}({number})")
                elif name or number:
                    available.append(name or number)
        except Exception:
            available = []
    suffix = f"; available pins include: {', '.join(available[:16])}" if available else ""
    return f"Pin {pin} not found on {ref} ({lib_id}): {error}{suffix}"
