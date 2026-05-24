"""
MCP server creation and configuration.
"""

import atexit
from collections.abc import Callable
import functools
import inspect
import logging
import os
import signal
from typing import Any

from fastmcp import FastMCP

# Import context management
from kicad_mcp.context import kicad_lifespan
from kicad_mcp.prompts.bom_prompts import register_bom_prompts
from kicad_mcp.prompts.drc_prompt import register_drc_prompts
from kicad_mcp.prompts.pattern_prompts import register_pattern_prompts

# Import prompt handlers
from kicad_mcp.prompts.templates import register_prompts
from kicad_mcp.resources.bom_resources import register_bom_resources
from kicad_mcp.resources.drc_resources import register_drc_resources
from kicad_mcp.resources.files import register_file_resources
from kicad_mcp.resources.netlist_resources import register_netlist_resources
from kicad_mcp.resources.pattern_resources import register_pattern_resources

# Import resource handlers
from kicad_mcp.resources.projects import register_project_resources
from kicad_mcp.tools.analysis_tools import register_analysis_tools
from kicad_mcp.tools.bom_tools import register_bom_tools
from kicad_mcp.tools.creation_tools import register_creation_tools
from kicad_mcp.tools.drc_tools import register_drc_tools
from kicad_mcp.tools.export_tools import register_export_tools
from kicad_mcp.tools.netlist_tools import register_netlist_tools
from kicad_mcp.tools.pattern_tools import register_pattern_tools

# Import tool handlers
from kicad_mcp.tools.project_tools import register_project_tools
from kicad_mcp.tools.schematic_edit_tools import register_schematic_edit_tools

# Track cleanup handlers
cleanup_handlers = []

# Flag to track whether we're already in shutdown process
_shutting_down = False

# Store server instance for clean shutdown
_server_instance = None

SUPPORTED_TRANSPORTS = {"stdio", "sse", "streamable-http", "http"}
DEFAULT_TRANSPORT = "stdio"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_SSE_PATH = "/sse"
DEFAULT_HTTP_PATH = "/mcp"
SUPPORTED_TOOL_PROFILES = {"agent", "default", "advanced", "debug", "all"}
DEFAULT_TOOL_PROFILE = "agent"
AGENT_PROFILE_TOOLS = {
    "project_design_state",
    "create_kicad_project",
    "discover_projects",
    "get_project_structure",
    "schematic_apply_design_intent",
    "schematic_preview_design_intent",
    "schematic_build_from_spec_v2",
    "schematic_apply_connection_plan",
    "schematic_quality_report",
    "run_erc_check",
    "find_symbols",
    "find_footprints",
    "resolve_symbol",
    "resolve_footprint",
}
ADVANCED_PROFILE_TOOLS = {
    "create_schematic_file",
    "schematic_add_symbol",
    "schematic_snap_to_grid",
    "schematic_delete_item",
    "schematic_apply_functional_layout",
    "list_symbol_libraries",
    "list_footprint_libraries",
}
DEBUG_PROFILE_TOOLS = {
    "schematic_add_wire",
    "schematic_add_label",
    "schematic_connect_points",
    "schematic_get_pin_map",
    "schematic_attach_net_to_pin",
    "schematic_preview_build_from_spec",
    "schematic_build_from_spec",
}


def add_cleanup_handler(handler: Callable) -> None:
    """Register a function to be called during cleanup.

    Args:
        handler: Function to call during cleanup
    """
    cleanup_handlers.append(handler)


def run_cleanup_handlers() -> None:
    """Run all registered cleanup handlers."""
    logging.info("Running cleanup handlers...")

    global _shutting_down

    # Prevent running cleanup handlers multiple times
    if _shutting_down:
        return

    _shutting_down = True
    logging.info("Running cleanup handlers...")

    for handler in cleanup_handlers:
        try:
            handler()
            logging.info(f"Cleanup handler {handler.__name__} completed successfully")
        except Exception as e:
            logging.error(f"Error in cleanup handler {handler.__name__}: {str(e)}", exc_info=True)


def shutdown_server():
    """Properly shutdown the server if it exists."""
    global _server_instance

    if _server_instance:
        try:
            logging.info("Shutting down KiCad MCP server")
            stop = getattr(_server_instance, "stop", None)
            if callable(stop):
                stop()
            _server_instance = None
            logging.info("KiCad MCP server shutdown complete")
        except Exception as e:
            logging.error(f"Error shutting down server: {str(e)}", exc_info=True)


def register_signal_handlers(server: FastMCP) -> None:
    """Register handlers for system signals to ensure clean shutdown.

    Args:
        server: The FastMCP server instance
    """

    def handle_exit_signal(signum, frame):
        logging.info(f"Received signal {signum}, initiating shutdown...")

        # Run cleanup first
        run_cleanup_handlers()

        # Then shutdown server
        shutdown_server()

        raise SystemExit(0)

    # Register for common termination signals
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_exit_signal)
            logging.info(f"Registered handler for signal {sig}")
        except (ValueError, AttributeError) as e:
            # Some signals may not be available on all platforms
            logging.error(f"Could not register handler for signal {sig}: {str(e)}")


def create_server() -> FastMCP:
    """Create and configure the KiCad MCP server."""
    global _server_instance
    logging.info("Initializing KiCad MCP server")

    # Try to set up KiCad Python path - Removed
    # kicad_modules_available = setup_kicad_python_path()
    kicad_modules_available = False  # Set to False as we removed the setup logic

    # if kicad_modules_available:
    #     print("KiCad Python modules successfully configured")
    # else:
    # Always print this now, as we rely on CLI
    logging.info("KiCad Python module setup removed; relying on kicad-cli for external operations.")

    # Build a lifespan callable with the kwarg baked in (FastMCP 2.x dropped lifespan_kwargs)
    lifespan_factory = functools.partial(
        kicad_lifespan, kicad_modules_available=kicad_modules_available
    )

    # Initialize FastMCP server
    mcp = FastMCP("KiCad", lifespan=lifespan_factory)
    _server_instance = mcp
    logging.info("Created FastMCP server instance with lifespan management")

    # Register resources
    logging.info("Registering resources...")
    register_project_resources(mcp)
    register_file_resources(mcp)
    register_drc_resources(mcp)
    register_bom_resources(mcp)
    register_netlist_resources(mcp)
    register_pattern_resources(mcp)

    # Register tools
    logging.info("Registering tools...")
    register_project_tools(mcp)
    register_analysis_tools(mcp)
    register_export_tools(mcp)
    register_drc_tools(mcp)
    register_bom_tools(mcp)
    register_netlist_tools(mcp)
    register_pattern_tools(mcp)
    register_schematic_edit_tools(mcp)
    register_creation_tools(mcp)
    _apply_tool_profile(mcp, get_tool_profile())

    # Register prompts
    logging.info("Registering prompts...")
    register_prompts(mcp)
    register_drc_prompts(mcp)
    register_bom_prompts(mcp)
    register_pattern_prompts(mcp)

    # Register signal handlers and cleanup
    register_signal_handlers(mcp)
    atexit.register(run_cleanup_handlers)

    # Add specific cleanup handlers
    add_cleanup_handler(lambda: logging.info("KiCad MCP server shutdown complete"))

    # Add temp directory cleanup
    def cleanup_temp_dirs():
        """Clean up any temporary directories created by the server."""
        import shutil

        from kicad_mcp.utils.temp_dir_manager import get_temp_dirs

        temp_dirs = get_temp_dirs()
        logging.info(f"Cleaning up {len(temp_dirs)} temporary directories")

        for temp_dir in temp_dirs:
            try:
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    logging.info(f"Removed temporary directory: {temp_dir}")
            except Exception as e:
                logging.error(f"Error cleaning up temporary directory {temp_dir}: {str(e)}")

    add_cleanup_handler(cleanup_temp_dirs)

    logging.info("Server initialization complete")
    return mcp


def setup_signal_handlers() -> None:
    """Setup signal handlers for graceful shutdown."""
    # Signal handlers are set up in register_signal_handlers
    pass


def cleanup_handler() -> None:
    """Handle cleanup during shutdown."""
    run_cleanup_handlers()


def setup_logging() -> None:
    """Configure logging for the server."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )


def _normalize_transport(value: str | None) -> str:
    """Normalize and validate the configured MCP transport."""
    transport = (value or DEFAULT_TRANSPORT).strip().lower().replace("_", "-")
    if transport not in SUPPORTED_TRANSPORTS:
        raise ValueError(
            f"Unsupported MCP transport '{value}'. Supported values: {', '.join(sorted(SUPPORTED_TRANSPORTS))}"
        )
    return transport


def _coerce_port(value: str | None) -> int:
    """Parse a port value from the environment."""
    if value in (None, ""):
        return DEFAULT_PORT
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError(f"KICAD_MCP_PORT must be an integer, got: {value}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"KICAD_MCP_PORT must be between 1 and 65535, got: {port}")
    return port


def get_tool_profile() -> str:
    """Return the configured MCP tool exposure profile."""
    profile = os.getenv("KICAD_MCP_TOOL_PROFILE", DEFAULT_TOOL_PROFILE).strip().lower()
    if profile not in SUPPORTED_TOOL_PROFILES:
        raise ValueError(
            "Unsupported KICAD_MCP_TOOL_PROFILE "
            f"'{profile}'. Supported values: {', '.join(sorted(SUPPORTED_TOOL_PROFILES))}"
        )
    return "agent" if profile == "default" else profile


def _apply_tool_profile(mcp: FastMCP, profile: str) -> None:
    """Hide tools that do not belong to the configured LLM tool surface."""
    tool_manager = getattr(mcp, "_tool_manager", None)
    tool_names = list(getattr(tool_manager, "_tools", {}).keys())
    if profile == "all":
        logging.info("Using all tool profile; all %d tools are exposed.", len(tool_names))
        return

    allowed_tools = set(AGENT_PROFILE_TOOLS)
    if profile in {"advanced", "debug"}:
        allowed_tools.update(ADVANCED_PROFILE_TOOLS)
    if profile == "debug":
        allowed_tools.update(DEBUG_PROFILE_TOOLS)

    hidden = []
    for tool_name in tool_names:
        if tool_name in allowed_tools:
            continue
        mcp.remove_tool(tool_name)
        hidden.append(tool_name)
    logging.info(
        "Using %s tool profile; exposed %d tools and hid %d tools.",
        profile,
        len(allowed_tools),
        len(hidden),
    )


def get_transport_config() -> dict[str, Any]:
    """Read MCP transport configuration from environment variables."""
    transport = _normalize_transport(os.getenv("KICAD_MCP_TRANSPORT"))
    default_path = DEFAULT_SSE_PATH if transport == "sse" else DEFAULT_HTTP_PATH
    return {
        "transport": transport,
        "host": os.getenv("KICAD_MCP_HOST", DEFAULT_HOST),
        "port": _coerce_port(os.getenv("KICAD_MCP_PORT")),
        "path": os.getenv("KICAD_MCP_PATH", default_path),
    }


def _run_server_with_config(server: FastMCP, transport_config: dict[str, Any]) -> None:
    """Run the FastMCP server with the configured transport.

    The installed FastMCP version owns the actual transport implementation. This wrapper only
    passes arguments supported by the local FastMCP.run signature, so stdio users keep the old
    behavior while remote MCP users can opt into SSE/HTTP transports.
    """
    run_signature = inspect.signature(server.run)
    accepted_args = set(run_signature.parameters)
    accepts_var_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in run_signature.parameters.values()
    )
    transport = transport_config["transport"]

    kwargs: dict[str, Any] = {}
    if "transport" in accepted_args:
        kwargs["transport"] = transport
    elif transport != "stdio":
        raise RuntimeError(
            "Installed FastMCP version does not expose transport selection in run()."
        )

    if transport != "stdio":
        for key in ("host", "port", "path"):
            if key in accepted_args or accepts_var_kwargs:
                kwargs[key] = transport_config[key]

    logging.info("Running KiCad MCP server with %s transport", transport)
    if transport != "stdio":
        logging.info(
            "KiCad MCP HTTP endpoint: http://%s:%s%s",
            transport_config["host"],
            transport_config["port"],
            transport_config["path"],
        )
    server.run(**kwargs)


def main() -> None:
    """Start the KiCad MCP server (blocking)."""
    global _server_instance
    setup_logging()
    transport_config = get_transport_config()
    logging.info("Starting KiCad MCP server...")

    server = create_server()

    try:
        _run_server_with_config(server, transport_config)
    except KeyboardInterrupt:
        logging.info("Server interrupted by user")
    except Exception as e:
        logging.error(f"Server error: {e}")
    finally:
        _server_instance = None
        logging.info("Server shutdown complete")


if __name__ == "__main__":
    main()
