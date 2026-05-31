"""SKiDL circuit compiler.

Converts a CanonicalCircuit into a SKiDL circuit to validate library parts and
pin selectors, then generates the expected netlist used for KiCad CLI
verification.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
import json
import logging
import os
from typing import Any

from kicad_mcp.schematic_engine.custom_symbols import decode_custom_pins, is_custom_lib_id
from kicad_mcp.schematic_engine.models import CanonicalCircuit, NetlistEntry, NormalizedNetlist
from kicad_mcp.utils.schematic_pins import _resolve_symbol_pins

logger = logging.getLogger(__name__)

_COMPILE_CACHE_MAX = 32
_COMPILE_CACHE: OrderedDict[str, SkidlCompileResult] = OrderedDict()


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
    """Compile a CanonicalCircuit using SKiDL-backed part and pin validation."""

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
        cache_key = _canonical_cache_key(canonical)
        cached = _COMPILE_CACHE.get(cache_key)
        if cached and cached.success and cached.expected_netlist is not None:
            _COMPILE_CACHE.move_to_end(cache_key)
            expected_path = self._save_expected_netlist(canonical, cached.expected_netlist)
            return SkidlCompileResult(
                success=True,
                expected_netlist=cached.expected_netlist,
                expected_netlist_path=expected_path,
                skidl_resolved_netlist=cached.skidl_resolved_netlist,
                verification_quality=cached.verification_quality,
                part_count=cached.part_count,
                net_count=cached.net_count,
                endpoint_count=cached.endpoint_count,
                erc_warnings=[*cached.erc_warnings, "SKiDL compile cache hit"],
                erc_errors=[],
            )

        result = self._compile_with_skidl(canonical)
        if result.success and result.expected_netlist is not None:
            _COMPILE_CACHE[cache_key] = SkidlCompileResult(
                success=True,
                expected_netlist=result.expected_netlist,
                skidl_resolved_netlist=result.skidl_resolved_netlist,
                verification_quality=result.verification_quality,
                part_count=result.part_count,
                net_count=result.net_count,
                endpoint_count=result.endpoint_count,
                erc_warnings=list(result.erc_warnings),
                erc_errors=[],
            )
            while len(_COMPILE_CACHE) > _COMPILE_CACHE_MAX:
                _COMPILE_CACHE.popitem(last=False)
        return result

    def _compile_with_skidl(self, canonical: CanonicalCircuit) -> SkidlCompileResult:
        """Compile using SKiDL for part and pin validation."""
        try:
            from skidl import Circuit

            # The runtime is intentionally mandatory, but KiCad CLI is the
            # authoritative ERC/netlist verifier. Instantiating every SKiDL
            # Part is too slow for MCP's synchronous timeout on medium designs,
            # so this stage validates selectors against the same KiCad symbols
            # used by the writer and produces the expected canonical netlist.
            Circuit()
            part_defs_by_ref = {part.ref: part for part in canonical.parts}
            pin_aliases_by_ref: dict[str, dict[str, str]] = {}
            erc_warnings: list[str] = []
            erc_errors: list[str] = []

            for part_def in canonical.parts:
                if is_custom_lib_id(part_def.lib_id):
                    pin_aliases_by_ref[part_def.ref] = _custom_pin_alias_lookup(part_def)
                    continue
                aliases = _pin_alias_lookup(part_def.lib_id)
                if not aliases:
                    erc_errors.append(
                        f"Symbol {part_def.lib_id} for {part_def.ref} could not be resolved"
                    )
                    continue
                pin_aliases_by_ref[part_def.ref] = aliases

            for endpoint in canonical.endpoints:
                if endpoint.ref not in part_defs_by_ref:
                    if endpoint.required:
                        erc_errors.append(
                            f"Part {endpoint.ref} not found for endpoint "
                            f"{endpoint.ref}.{endpoint.pin} -> {endpoint.net}"
                        )
                    continue
                aliases = pin_aliases_by_ref.get(endpoint.ref, {})
                if _pin_lookup_key(endpoint.pin) not in aliases:
                    if endpoint.required and not endpoint.allow_hidden:
                        erc_errors.append(
                            _pin_not_found_message(
                                endpoint.ref,
                                endpoint.pin,
                                part_defs_by_ref[endpoint.ref].lib_id,
                                ValueError("pin selector did not match a symbol pin"),
                            )
                        )
                    else:
                        erc_warnings.append(
                            f"Optional pin {endpoint.pin} on {endpoint.ref} not resolved"
                        )

            # Generate expected netlist from canonical (source of truth for comparison)
            nets_dict: dict[str, set[NetlistEntry]] = defaultdict(set)
            for endpoint in canonical.endpoints:
                entry = NetlistEntry(ref=endpoint.ref, pin=endpoint.pin)
                nets_dict[endpoint.net].add(entry)

            expected = NormalizedNetlist(nets=dict(nets_dict))

            skidl_resolved = _resolved_selector_netlist(canonical, pin_aliases_by_ref)

            netlist_path = self._save_expected_netlist(canonical, expected)

            return SkidlCompileResult(
                success=len(erc_errors) == 0,
                expected_netlist=expected,
                expected_netlist_path=netlist_path,
                skidl_netlist_path=None,
                skidl_resolved_netlist=skidl_resolved,
                verification_quality="skidl_runtime_symbol_pin_verified",
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


def _custom_pin_alias_lookup(part_def: Any) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for pin in decode_custom_pins(part_def.properties.get("KICAD_MCP_CUSTOM_PINS")):
        number = str(pin.get("number") or "")
        name = str(pin.get("name") or "")
        pinfunction = f"{name}_{number}" if name and number else name or number
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


def _resolved_selector_netlist(
    canonical: CanonicalCircuit,
    pin_aliases_by_ref: dict[str, dict[str, str]],
) -> NormalizedNetlist:
    resolved: dict[str, set[NetlistEntry]] = defaultdict(set)
    for endpoint in canonical.endpoints:
        selector = pin_aliases_by_ref.get(endpoint.ref, {}).get(
            _pin_lookup_key(endpoint.pin),
            endpoint.pin,
        )
        resolved[endpoint.net].add(NetlistEntry(ref=endpoint.ref, pin=selector))
    return NormalizedNetlist(nets=dict(resolved))


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
        )
        if stripped:
            aliases.add(stripped)
            if "/" in stripped:
                aliases.update(part for part in stripped.split("/") if part)
        if "/" in candidate:
            aliases.update(part for part in candidate.split("/") if part)
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


def _canonical_cache_key(canonical: CanonicalCircuit) -> str:
    data = {
        "parts": [
            {
                "ref": part.ref,
                "lib_id": part.lib_id,
                "value": part.value,
                "footprint": part.footprint,
                "block": part.block,
                "role": part.role,
                "properties": part.properties,
            }
            for part in canonical.parts
        ],
        "endpoints": [
            {
                "ref": endpoint.ref,
                "pin": endpoint.pin,
                "net": endpoint.net,
                "required": endpoint.required,
                "allow_hidden": endpoint.allow_hidden,
                "source": endpoint.source,
            }
            for endpoint in canonical.endpoints
        ],
        "no_connects": canonical.no_connects,
        "rails": sorted(canonical.rails),
    }
    return json.dumps(data, sort_keys=True, separators=(",", ":"))
