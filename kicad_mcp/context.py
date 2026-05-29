"""
Lifespan context management for KiCad MCP Server.
"""

from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging  # Import logging
from typing import Any

from fastmcp import FastMCP

# Get PID for logging
# _PID = os.getpid()

DEFAULT_CACHE_MAX_ENTRIES = 128


class BoundedCache(OrderedDict[str, Any]):
    """Small LRU cache for lifespan-scoped expensive operation results."""

    def __init__(self, max_entries: int = DEFAULT_CACHE_MAX_ENTRIES):
        super().__init__()
        self.max_entries = max_entries

    def __getitem__(self, key: str) -> Any:
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value

    def get(self, key: str, default: Any = None) -> Any:
        if key not in self:
            return default
        return self[key]

    def __setitem__(self, key: str, value: Any) -> None:
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self.max_entries:
            oldest_key = next(iter(self))
            super().__delitem__(oldest_key)


@dataclass
class KiCadAppContext:
    """Type-safe context for KiCad MCP server."""

    kicad_modules_available: bool

    # Optional cache for expensive operations
    cache: BoundedCache


def get_kicad_app_context(ctx: Any | None) -> KiCadAppContext | None:
    """Safely extract the KiCad lifespan context from a FastMCP request context."""
    if ctx is None:
        return None
    request_context = getattr(ctx, "request_context", None)
    lifespan_context = getattr(request_context, "lifespan_context", None)
    return lifespan_context if isinstance(lifespan_context, KiCadAppContext) else None


@asynccontextmanager
async def kicad_lifespan(
    server: FastMCP, kicad_modules_available: bool = False
) -> AsyncIterator[KiCadAppContext]:
    """Manage KiCad MCP server lifecycle with type-safe context.

    This function handles:
    1. Initializing shared resources when the server starts
    2. Providing a typed context object to all request handlers
    3. Properly cleaning up resources when the server shuts down

    Args:
        server: The FastMCP server instance
        kicad_modules_available: Flag indicating if Python modules were found (passed from create_server)

    Yields:
        KiCadAppContext: A typed context object shared across all handlers
    """
    logging.info("Starting KiCad MCP server initialization")

    # Resources initialization - Python path setup removed
    # print("Setting up KiCad Python modules")
    # kicad_modules_available = setup_kicad_python_path() # Now passed as arg
    logging.info(
        f"KiCad Python module availability: {kicad_modules_available} (Setup logic removed)"
    )

    # Create in-memory cache for expensive operations
    cache = BoundedCache()

    try:
        # --- Removed Python module preloading section ---
        # if kicad_modules_available:
        #     try:
        #         print("Preloading KiCad Python modules")
        #         ...
        #     except ImportError as e:
        #         print(f"Failed to preload some KiCad modules: {str(e)}")

        # Yield the context to the server - server runs during this time
        logging.info("KiCad MCP server initialization complete")
        yield KiCadAppContext(
            kicad_modules_available=kicad_modules_available,  # Pass the flag through
            cache=cache,
        )
    finally:
        # Clean up resources when server shuts down
        logging.info("Shutting down KiCad MCP server")

        # Clear the cache
        if cache:
            logging.info(f"Clearing cache with {len(cache)} entries")
            cache.clear()

        logging.info("KiCad MCP server shutdown complete")
