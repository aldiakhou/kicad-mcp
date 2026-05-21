"""
KiCad MCP Server.

A Model Context Protocol (MCP) server for KiCad electronic design automation (EDA) files.
"""

from . import config as _config
from . import context as _context
from . import server as _server
from .context import KiCadAppContext, kicad_lifespan
from .server import (
    add_cleanup_handler,
    create_server,
    run_cleanup_handlers,
    shutdown_server,
)

__version__ = "0.1.0"
__author__ = "Lama Al Rajih"
__description__ = "Model Context Protocol server for KiCad on Mac, Windows, and Linux"
_DELEGATED_PUBLIC_NAMES = {
    name: getattr(module, name)
    for module in (_server, _config, _context)
    for name in dir(module)
    if not name.startswith("_")
}

__all__ = [
    # Package metadata
    "__version__",
    "__author__",
    "__description__",

    # Server creation / shutdown helpers
    "create_server",
    "add_cleanup_handler",
    "run_cleanup_handlers",
    "shutdown_server",

    # Lifespan / context helpers
    "kicad_lifespan",
    "KiCadAppContext",
]


def __getattr__(name: str) -> object:
    """Preserve package-level access to public names from legacy re-exports."""
    if name in _DELEGATED_PUBLIC_NAMES:
        return _DELEGATED_PUBLIC_NAMES[name]
    raise AttributeError(f"module 'kicad_mcp' has no attribute {name!r}")


def __dir__() -> list[str]:
    """Expose delegated public names for interactive discovery."""
    return sorted(set(__all__) | set(_DELEGATED_PUBLIC_NAMES))
