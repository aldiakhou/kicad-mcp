"""
KiCad symbol and footprint library discovery.
"""

from __future__ import annotations

from copy import deepcopy
from difflib import SequenceMatcher
from functools import lru_cache
import os
from pathlib import Path
import platform
import re
from typing import Any

from kicad_mcp.utils.kicad_s_expr import (
    SExpressionError,
    SExprAtom,
    SExprList,
    parse_s_expression,
    serialize_s_expression,
)


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


@lru_cache(maxsize=256)
def find_symbols(
    query: str, max_results: int = 10, library: str | None = None
) -> list[dict[str, Any]]:
    """Fuzzy-search installed KiCad symbols by library, symbol name, and metadata."""
    normalized_query = _normalize_search_text(query)
    normalized_library = _normalize_search_text(library or "")
    if not normalized_query:
        return []
    matches: list[tuple[float, dict[str, Any]]] = []
    for library in list_symbol_libraries():
        library_name = library["name"]
        if normalized_library and normalized_library not in _normalize_search_text(library_name):
            continue
        library_query_match = normalized_query in _normalize_search_text(library_name)
        try:
            library_text = Path(library["path"]).read_text(encoding="utf-8")
            if (
                not library_query_match
                and query.lower() not in library_text.lower()
                and SequenceMatcher(
                    None, normalized_query, _normalize_search_text(library_name)
                ).ratio()
                < 0.62
            ):
                continue
            root = parse_s_expression(library_text)
        except (OSError, UnicodeDecodeError, SExpressionError):
            continue
        for symbol in root.child_lists("symbol"):
            symbol_name = _atom_text(symbol.items[1] if len(symbol.items) > 1 else None) or ""
            lib_id = f"{library_name}:{symbol_name}"
            properties = _symbol_properties(symbol)
            default_footprint = properties.get("Footprint", "")
            footprint_filters = _split_footprint_filters(properties.get("ki_fp_filters", ""))
            searchable = " ".join(
                [
                    lib_id,
                    library_name,
                    symbol_name,
                    properties.get("Value", ""),
                    properties.get("Description", ""),
                    properties.get("ki_description", ""),
                    properties.get("Keywords", ""),
                    properties.get("ki_keywords", ""),
                    properties.get("ki_fp_filters", ""),
                    default_footprint,
                ]
            )
            score = _search_score(normalized_query, searchable, lib_id)
            if score <= 0:
                continue
            matches.append(
                (
                    score,
                    {
                        "lib_id": lib_id,
                        "library": library_name,
                        "symbol": symbol_name,
                        "description": properties.get("Description")
                        or properties.get("ki_description", ""),
                        "keywords": properties.get("Keywords")
                        or properties.get("ki_keywords", ""),
                        "footprint_filters": footprint_filters,
                        "default_footprint": default_footprint,
                    },
                )
            )
    matches.sort(key=lambda item: (-item[0], item[1]["lib_id"]))
    return [item for _, item in matches[: max(1, int(max_results))]]


@lru_cache(maxsize=256)
def find_footprints(
    query: str, max_results: int = 10, library: str | None = None
) -> list[dict[str, Any]]:
    """Fuzzy-search installed KiCad footprints by library and footprint name."""
    normalized_query = _normalize_search_text(query)
    normalized_library = _normalize_search_text(library or "")
    if not normalized_query:
        return []
    matches: list[tuple[float, dict[str, Any]]] = []
    for library in list_footprint_libraries():
        library_name = library["name"]
        if normalized_library and normalized_library not in _normalize_search_text(library_name):
            continue
        for footprint_file in sorted(Path(library["path"]).glob("*.kicad_mod")):
            footprint_name = footprint_file.stem
            footprint_id = f"{library_name}:{footprint_name}"
            searchable = f"{footprint_id} {library_name} {footprint_name}"
            score = _search_score(normalized_query, searchable, footprint_id)
            if score <= 0:
                continue
            matches.append(
                (
                    score,
                    {
                        "footprint_id": footprint_id,
                        "library": library_name,
                        "footprint": footprint_name,
                        "path": str(footprint_file),
                    },
                )
            )
    matches.sort(key=lambda item: (-item[0], item[1]["footprint_id"]))
    return [item for _, item in matches[: max(1, int(max_results))]]


def symbol_footprint_suggestions(lib_id: str, max_results: int = 5) -> list[dict[str, Any]]:
    """Return footprint suggestions from a symbol's default Footprint and ki_fp_filters."""
    symbol = resolve_symbol(lib_id)
    properties = _symbol_properties(symbol["node"])
    suggestions: list[dict[str, Any]] = []
    default_footprint = properties.get("Footprint", "").strip()
    if default_footprint:
        suggestions.append({"footprint": default_footprint, "source": "symbol_default"})
    for footprint_filter in _split_footprint_filters(properties.get("ki_fp_filters", "")):
        for footprint in _footprints_matching_filter(footprint_filter):
            if all(item["footprint"] != footprint for item in suggestions):
                suggestions.append(
                    {
                        "footprint": footprint,
                        "source": "footprint_filter",
                        "filter": footprint_filter,
                    }
                )
            if len(suggestions) >= max(1, int(max_results)):
                return suggestions
    return suggestions[: max(1, int(max_results))]


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
    resolution = "exact"
    resolved_from = None
    if footprint_file is None:
        fuzzy = _resolve_footprint_fuzzy(footprint_id, library_name, footprint_name)
        if fuzzy is None:
            suggestions = find_footprints(footprint_name, max_results=5, library=library_name)
            suggestion_ids = [item["footprint_id"] for item in suggestions]
            detail = (
                f" Suggestions: {', '.join(suggestion_ids)}"
                if suggestion_ids
                else ""
            )
            raise KiCadLibraryError(f"Footprint not found: {footprint_id}.{detail}")
        footprint_id = fuzzy["footprint_id"]
        library_name = fuzzy["library"]
        footprint_name = fuzzy["footprint"]
        footprint_file = Path(fuzzy["path"])
        resolution = "fuzzy"
        resolved_from = fuzzy["resolved_from"]
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
        "resolution": resolution,
        "resolved_from": resolved_from,
    }


def _resolve_footprint_fuzzy(
    requested_id: str,
    requested_library: str,
    requested_name: str,
) -> dict[str, Any] | None:
    """Resolve common manufacturer punctuation variants within one footprint library."""
    requested_library_normalized = _normalize_search_text(requested_library)
    requested_name_normalized = _normalize_search_text(requested_name)
    candidates: list[dict[str, Any]] = []
    for library in list_footprint_libraries(requested_library):
        library_name = library["name"]
        if _normalize_search_text(library_name) != requested_library_normalized:
            continue
        for footprint_file in sorted(Path(library["path"]).glob("*.kicad_mod")):
            footprint_name = footprint_file.stem
            if _normalize_search_text(footprint_name) != requested_name_normalized:
                continue
            candidates.append(
                {
                    "footprint_id": f"{library_name}:{footprint_name}",
                    "library": library_name,
                    "footprint": footprint_name,
                    "path": str(footprint_file),
                    "resolved_from": requested_id,
                }
            )
    unique: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        unique.setdefault(candidate["footprint_id"], candidate)
    if len(unique) == 1:
        return next(iter(unique.values()))
    return None


def _split_library_id(item_id: str) -> tuple[str, str]:
    if not item_id or ":" not in item_id:
        raise KiCadLibraryError(f"Expected KiCad library id in Library:Item form, got: {item_id}")
    library_name, item_name = item_id.split(":", 1)
    if not library_name or not item_name:
        raise KiCadLibraryError(f"Expected KiCad library id in Library:Item form, got: {item_id}")
    return library_name, item_name


def _symbol_properties(symbol: SExprList) -> dict[str, str]:
    properties = {}
    for child in symbol.child_lists("property"):
        if len(child.items) < 3:
            continue
        name = _atom_text(child.items[1])
        value = _atom_text(child.items[2])
        if name:
            properties[name] = value or ""
    return properties


def _split_footprint_filters(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split() if item.strip()]


def _footprints_matching_filter(footprint_filter: str) -> list[str]:
    regex = _footprint_filter_regex(footprint_filter)
    matches = []
    for library in list_footprint_libraries():
        library_name = library["name"]
        for footprint_file in sorted(Path(library["path"]).glob("*.kicad_mod")):
            footprint_name = footprint_file.stem
            footprint_id = f"{library_name}:{footprint_name}"
            if regex.fullmatch(footprint_id) or regex.fullmatch(footprint_name):
                matches.append(footprint_id)
    return matches


def _footprint_filter_regex(footprint_filter: str) -> Any:
    pattern = re.escape(footprint_filter).replace(r"\*", ".*").replace(r"\?", ".")
    return re.compile(pattern, re.IGNORECASE)


def _normalize_search_text(value: str) -> str:
    return "".join(character.lower() for character in str(value) if character.isalnum())


def _search_score(normalized_query: str, searchable: str, identifier: str) -> float:
    normalized_searchable = _normalize_search_text(searchable)
    normalized_identifier = _normalize_search_text(identifier)
    if normalized_query in normalized_identifier:
        return 100.0 - (len(normalized_identifier) - len(normalized_query)) * 0.01
    if normalized_query in normalized_searchable:
        return 80.0 - (len(normalized_searchable) - len(normalized_query)) * 0.001
    ratio = SequenceMatcher(None, normalized_query, normalized_identifier).ratio()
    if ratio >= 0.62:
        return ratio * 60.0
    return 0.0


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
