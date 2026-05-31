"""KiCad symbol and footprint lookup MCP tools."""

from typing import Any

from fastmcp import FastMCP

import kicad_mcp.tools.creation_tools as ct
from kicad_mcp.utils.library_resolver import KiCadLibraryError


def register_library_lookup_tools(mcp: FastMCP) -> None:
    """Register symbol and footprint discovery tools."""

    @mcp.tool()
    def list_symbol_libraries(query: str | None = None) -> dict[str, Any]:
        """List available KiCad symbol libraries."""
        libraries = ct.resolve_symbol_libraries(query)
        return {"success": True, "query": query, "count": len(libraries), "libraries": libraries}

    @mcp.tool()
    def list_footprint_libraries(query: str | None = None) -> dict[str, Any]:
        """List available KiCad footprint libraries."""
        libraries = ct.resolve_footprint_libraries(query)
        return {"success": True, "query": query, "count": len(libraries), "libraries": libraries}

    @mcp.tool()
    def resolve_symbol(
        lib_id: str | None = None,
        detail: str = "compact",
        include_source: bool = False,
        include_pins: bool = True,
        symbol: str | None = None,
        symbol_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a KiCad symbol from installed libraries."""
        try:
            resolved_lib_id = ct._resolve_symbol_id_alias(lib_id, symbol, symbol_id)
            return ct._run_heavy_library_tool(
                lambda: ct._resolve_symbol_for_tool(
                    resolved_lib_id,
                    detail=detail,
                    include_source=include_source,
                    include_pins=include_pins,
                )
            )
        except (KiCadLibraryError, ValueError) as exc:
            return {"success": False, "lib_id": lib_id or symbol_id or symbol, "error": str(exc)}

    @mcp.tool()
    def resolve_symbols(
        lib_ids: list[str] | None = None,
        symbols: list[Any] | None = None,
        detail: str = "compact",
        include_source: bool = False,
        include_pins: bool = True,
        items: list[Any] | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Resolve multiple KiCad symbols with per-item success and failure results."""

        def operation() -> dict[str, Any]:
            resolved_detail = "pins" if str(mode or "").lower() == "pin_map" else detail
            requests = ct._normalize_resolve_symbol_requests(lib_ids, symbols, items)
            if not requests:
                return {
                    "success": False,
                    "results": [],
                    "resolved_count": 0,
                    "failed_count": 0,
                    "error": "lib_ids, symbols, or items is required",
                }
            results = []
            resolved_count = 0
            failed_count = 0
            for request in requests:
                requested_lib_id = request.get("lib_id", "")
                ref = request.get("ref")
                if request.get("error") or not requested_lib_id:
                    failed_count += 1
                    results.append(
                        {
                            "success": False,
                            "lib_id": requested_lib_id,
                            "ref": ref,
                            "error": request.get("error") or "lib_id is required",
                        }
                    )
                    continue
                try:
                    result = ct._resolve_symbol_for_tool(
                        requested_lib_id,
                        detail=resolved_detail,
                        include_source=include_source,
                        include_pins=include_pins,
                    )
                    if ref:
                        result["ref"] = ref
                    results.append(result)
                    resolved_count += 1
                except (KiCadLibraryError, ValueError) as exc:
                    failed_count += 1
                    results.append(
                        {
                            "success": False,
                            "lib_id": requested_lib_id,
                            "ref": ref,
                            "error": str(exc),
                        }
                    )
            return {
                "success": resolved_count > 0,
                "partial_success": resolved_count > 0 and failed_count > 0,
                "resolved_count": resolved_count,
                "failed_count": failed_count,
                "results": results,
            }

        return ct._run_heavy_library_tool(operation)

    @mcp.tool()
    def find_symbols(
        query: str | None = None,
        max_results: int = 10,
        library: str | None = None,
        limit: int | None = None,
        filter: str | None = None,
        queries: list[str] | None = None,
    ) -> dict[str, Any]:
        """Fuzzy-search KiCad symbols before resolving an exact lib_id."""
        try:
            resolved_library = library or filter
            resolved_limit = limit if limit is not None else max_results
            if queries is not None:
                return ct._run_heavy_library_tool(
                    lambda: ct._find_symbols_batch_for_tool(
                        queries,
                        resolved_limit,
                        resolved_library,
                    )
                )
            if not query:
                return {"success": False, "query": query, "error": "query or queries is required"}
            return ct._run_heavy_library_tool(
                lambda: ct._find_symbols_for_tool(query, resolved_limit, resolved_library)
            )
        except Exception as exc:
            return {"success": False, "query": query, "error": str(exc)}

    @mcp.tool()
    def find_footprints(
        query: str | None = None,
        max_results: int = 10,
        library: str | None = None,
        limit: int | None = None,
        filter: str | None = None,
        queries: list[str] | None = None,
    ) -> dict[str, Any]:
        """Fuzzy-search KiCad footprints before resolving an exact footprint_id."""
        try:
            resolved_library = library or filter
            resolved_limit = limit if limit is not None else max_results
            if queries is not None:
                return ct._run_heavy_library_tool(
                    lambda: ct._find_footprints_batch_for_tool(
                        queries,
                        resolved_limit,
                        resolved_library,
                    )
                )
            if not query:
                return {"success": False, "query": query, "error": "query or queries is required"}
            return ct._run_heavy_library_tool(
                lambda: ct._find_footprints_for_tool(query, resolved_limit, resolved_library)
            )
        except Exception as exc:
            return {"success": False, "query": query, "error": str(exc)}

    @mcp.tool()
    def resolve_footprints(
        footprint_ids: list[str] | None = None,
        footprints: list[Any] | None = None,
        items: list[Any] | None = None,
        detail: str = "compact",
        include_source: bool = False,
    ) -> dict[str, Any]:
        """Resolve multiple KiCad footprints with per-item success and failure results."""

        def operation() -> dict[str, Any]:
            requests = ct._normalize_resolve_footprint_requests(
                footprint_ids,
                footprints,
                items,
            )
            if not requests:
                return {
                    "success": False,
                    "results": [],
                    "resolved_count": 0,
                    "failed_count": 0,
                    "error": "footprint_ids, footprints, or items is required",
                }
            results = []
            resolved_count = 0
            failed_count = 0
            for request in requests:
                requested_footprint_id = request.get("footprint_id", "")
                ref = request.get("ref")
                if not requested_footprint_id:
                    failed_count += 1
                    results.append(
                        {
                            "success": False,
                            "footprint_id": requested_footprint_id,
                            "ref": ref,
                            "error": request.get("error") or "footprint_id is required",
                        }
                    )
                    continue
                try:
                    result = ct._resolve_footprint_for_tool(
                        requested_footprint_id,
                        detail=detail,
                        include_source=include_source,
                    )
                    if ref:
                        result["ref"] = ref
                    results.append(result)
                    resolved_count += 1
                except (KiCadLibraryError, ValueError) as exc:
                    failed_count += 1
                    results.append(
                        {
                            "success": False,
                            "footprint_id": requested_footprint_id,
                            "ref": ref,
                            "error": str(exc),
                        }
                    )
            return {
                "success": resolved_count > 0,
                "partial_success": resolved_count > 0 and failed_count > 0,
                "resolved_count": resolved_count,
                "failed_count": failed_count,
                "results": results,
            }

        return ct._run_heavy_library_tool(operation)

    @mcp.tool()
    def resolve_footprint(
        footprint_id: str | None = None,
        footprint: str | None = None,
        detail: str = "compact",
        include_source: bool = False,
    ) -> dict[str, Any]:
        """Resolve a KiCad footprint from installed libraries."""
        try:
            resolved_footprint_id = ct._resolve_footprint_id_alias(footprint_id, footprint)
            return ct._run_heavy_library_tool(
                lambda: ct._resolve_footprint_for_tool(
                    resolved_footprint_id,
                    detail=detail,
                    include_source=include_source,
                )
            )
        except (KiCadLibraryError, ValueError) as exc:
            return {"success": False, "footprint_id": footprint_id or footprint, "error": str(exc)}
