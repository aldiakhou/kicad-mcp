"""Batched KiCad CLI validation with per-file-revision caching."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import time
from typing import Any

from kicad_mcp.utils.native_netlist import export_native_netlist, run_erc_via_cli


@dataclass
class SchematicValidationBundle:
    schematic_path: str
    file_hash: str
    native_netlist: dict[str, Any] | None
    erc: dict[str, Any] | None
    svg_export: dict[str, Any] | None
    elapsed_ms: dict[str, float]


_SCHEMATIC_VALIDATION_CACHE: dict[
    tuple[str, str, bool, bool, bool, float],
    SchematicValidationBundle,
] = {}


def validate_schematic_batch(
    schematic_path: str,
    *,
    need_netlist: bool = True,
    need_erc: bool = False,
    need_svg: bool = False,
    timeout_seconds: float = 60.0,
    cache: bool = True,
) -> SchematicValidationBundle:
    """Run requested KiCad CLI validations at most once per schematic content hash."""
    path = str(Path(schematic_path).resolve())
    file_hash = _file_hash(path)
    key = (path, file_hash, need_netlist, need_erc, need_svg, float(timeout_seconds))
    if cache and key in _SCHEMATIC_VALIDATION_CACHE:
        return _SCHEMATIC_VALIDATION_CACHE[key]

    elapsed_ms: dict[str, float] = {}
    native_netlist = None
    erc = None
    svg_export = None

    if need_netlist:
        start = time.perf_counter()
        native_netlist = export_native_netlist(path, timeout_seconds=timeout_seconds)
        elapsed_ms["native_netlist"] = _elapsed_ms(start)
    if need_erc:
        start = time.perf_counter()
        erc = run_erc_via_cli(path, timeout_seconds=timeout_seconds)
        elapsed_ms["erc"] = _elapsed_ms(start)
    if need_svg:
        from kicad_mcp.utils.transactional_edit import export_schematic_svg_file

        start = time.perf_counter()
        svg_export = export_schematic_svg_file(path, None)
        elapsed_ms["svg_export"] = _elapsed_ms(start)

    bundle = SchematicValidationBundle(
        schematic_path=path,
        file_hash=file_hash,
        native_netlist=native_netlist,
        erc=erc,
        svg_export=svg_export,
        elapsed_ms=elapsed_ms,
    )
    if cache:
        _SCHEMATIC_VALIDATION_CACHE[key] = bundle
    return bundle


def invalidate_schematic_validation_cache(schematic_path: str) -> None:
    """Drop cached validation bundles for a schematic path after a write or rollback."""
    path = str(Path(schematic_path).resolve())
    stale = [key for key in _SCHEMATIC_VALIDATION_CACHE if key[0] == path]
    for key in stale:
        _SCHEMATIC_VALIDATION_CACHE.pop(key, None)


def clear_schematic_validation_cache() -> None:
    """Clear all cached schematic validation bundles."""
    _SCHEMATIC_VALIDATION_CACHE.clear()


def _file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000.0, 3)
