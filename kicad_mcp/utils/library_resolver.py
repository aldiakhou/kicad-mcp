"""
KiCad symbol and footprint library discovery.
"""

from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import platform
from typing import Any

from kicad_mcp.utils.kicad_s_expr import SExprAtom, parse_s_expression, serialize_s_expression


class KiCadLibraryError(FileNotFoundError):
    """Raised when a requested KiCad library item cannot be resolved."""


def list_symbol_libraries(query: str | None = None) -> list[dict[str, Any]]:
    """List available KiCad symbol library files."""
    libraries = []
    normalized_query = query.lower() if query else None
    for root in _symbol_roots():
        for library_file in sorted(root.glob("*.kicad_sym")):
            name = library_file.stem
            if normalized_query and normalized_query not in name.lower():
                continue
            libraries.append({"name": name, "path": str(library_file)})
    return libraries


def list_footprint_libraries(query: str | None = None) -> list[dict[str, Any]]:
    """List available KiCad footprint library directories."""
    libraries = []
    normalized_query = query.lower() if query else None
    for root in _footprint_roots():
        for library_dir in sorted(root.glob("*.pretty")):
            name = library_dir.stem
            if normalized_query and normalized_query not in name.lower():
                continue
            libraries.append({"name": name, "path": str(library_dir)})
    return libraries


def resolve_symbol(lib_id: str) -> dict[str, Any]:
    """Resolve a KiCad symbol by full lib_id, for example Device:R."""
    library_name, symbol_name = _split_library_id(lib_id)
    library_file = _find_symbol_library(library_name)
    if library_file is None:
        raise KiCadLibraryError(f"Symbol library not found: {library_name}")

    root = parse_s_expression(library_file.read_text(encoding="utf-8"))
    for symbol in root.child_lists("symbol"):
        if _atom_text(symbol.items[1] if len(symbol.items) > 1 else None) == symbol_name:
            embedded = deepcopy(symbol)
            embedded.items[1] = SExprAtom(lib_id, quoted=True)
            return {
                "success": True,
                "lib_id": lib_id,
                "library": library_name,
                "symbol": symbol_name,
                "path": str(library_file),
                "node": embedded,
                "source": serialize_s_expression(embedded),
            }
    raise KiCadLibraryError(f"Symbol not found: {lib_id}")


def resolve_footprint(footprint_id: str) -> dict[str, Any]:
    """Resolve a KiCad footprint by full footprint_id, for example Resistor_SMD:R_0603_1608Metric."""
    library_name, footprint_name = _split_library_id(footprint_id)
    footprint_file = _find_footprint_file(library_name, footprint_name)
    if footprint_file is None:
        raise KiCadLibraryError(f"Footprint not found: {footprint_id}")
    footprint = parse_s_expression(footprint_file.read_text(encoding="utf-8"))
    if footprint.head() != "footprint":
        raise KiCadLibraryError(f"Invalid footprint file for {footprint_id}: {footprint_file}")
    footprint = deepcopy(footprint)
    footprint.items[1] = SExprAtom(footprint_name, quoted=True)
    return {
        "success": True,
        "footprint_id": footprint_id,
        "library": library_name,
        "footprint": footprint_name,
        "path": str(footprint_file),
        "node": footprint,
        "source": serialize_s_expression(footprint),
    }


def _split_library_id(item_id: str) -> tuple[str, str]:
    if not item_id or ":" not in item_id:
        raise KiCadLibraryError(f"Expected KiCad library id in Library:Item form, got: {item_id}")
    library_name, item_name = item_id.split(":", 1)
    if not library_name or not item_name:
        raise KiCadLibraryError(f"Expected KiCad library id in Library:Item form, got: {item_id}")
    return library_name, item_name


def _find_symbol_library(library_name: str) -> Path | None:
    for root in _symbol_roots():
        candidate = root / f"{library_name}.kicad_sym"
        if candidate.exists():
            return candidate
    return None


def _find_footprint_file(library_name: str, footprint_name: str) -> Path | None:
    for root in _footprint_roots():
        candidate = root / f"{library_name}.pretty" / f"{footprint_name}.kicad_mod"
        if candidate.exists():
            return candidate
    return None


def _symbol_roots() -> list[Path]:
    return (
        _library_roots_from_env("KICAD_SYMBOL_DIR", "KICAD_SYMBOL_PATHS") + _common_symbol_roots()
    )


def _footprint_roots() -> list[Path]:
    return (
        _library_roots_from_env("KICAD_FOOTPRINT_DIR", "KICAD_FOOTPRINT_PATHS")
        + _common_footprint_roots()
    )


def _library_roots_from_env(primary: str, multi: str) -> list[Path]:
    roots = []
    for raw in [os.getenv(primary, ""), *os.getenv(multi, "").split(os.pathsep)]:
        if raw.strip():
            path = Path(os.path.expanduser(raw.strip())).resolve()
            if path.exists() and path not in roots:
                roots.append(path)
    return roots


def _common_symbol_roots() -> list[Path]:
    return [root / "symbols" for root in _common_share_roots() if (root / "symbols").exists()]


def _common_footprint_roots() -> list[Path]:
    return [root / "footprints" for root in _common_share_roots() if (root / "footprints").exists()]


def _common_share_roots() -> list[Path]:
    candidates: list[Path] = []
    system = platform.system()
    if system == "Windows":
        for base in (Path(r"C:\Program Files\KiCad"), Path(r"C:\Program Files (x86)\KiCad")):
            if base.exists():
                candidates.extend(sorted(base.glob(r"*\share\kicad"), reverse=True))
        candidates.append(Path(r"C:\Program Files\KiCad\share\kicad"))
    elif system == "Darwin":
        candidates.extend(
            [
                Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport"),
                Path("/Applications/KiCad/kicad.app/Contents/SharedSupport"),
            ]
        )
    else:
        candidates.extend([Path("/usr/share/kicad"), Path("/usr/local/share/kicad")])
    seen = []
    for candidate in candidates:
        resolved = candidate.resolve() if candidate.exists() else candidate
        if resolved.exists() and resolved not in seen:
            seen.append(resolved)
    return seen


def _atom_text(node: object | None) -> str | None:
    return node.value if isinstance(node, SExprAtom) else None
