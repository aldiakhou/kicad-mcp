"""
MCP server creation and configuration.
"""

import atexit
from collections.abc import Callable
import functools
import importlib
import inspect
import json
import logging
import os
from pathlib import Path
import signal
import time
from typing import Any
import uuid

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
from kicad_mcp.tools import (
    ADVANCED_PROFILE_TOOLS,
    AGENT_PROFILE_TOOLS,
    DEBUG_PROFILE_TOOLS,
    register_all_tools,
)

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
DEFAULT_MAX_INLINE_BYTES = 50_000
LARGE_RESPONSE_FIELD_NAMES = {
    "source",
    "diff",
    "preview",
    "svg",
    "report",
    "violations",
    "nets",
    "components",
    "native_netlist",
    "full_native_netlist",
}

_SCHEMATIC_RUNTIME_DEPENDENCIES = (
    ("skidl", "skidl"),
    ("kiutils", "kiutils"),
    ("skip", "kicad-skip"),
)


def _install_fastmcp_compat() -> None:
    """Normalize FastMCP API differences across supported local versions."""
    if not hasattr(FastMCP, "list_tools") and hasattr(FastMCP, "_list_tools"):
        FastMCP.list_tools = FastMCP._list_tools  # type: ignore[attr-defined]
    if not hasattr(FastMCP, "list_resource_templates") and hasattr(
        FastMCP,
        "_list_resource_templates",
    ):
        FastMCP.list_resource_templates = FastMCP._list_resource_templates  # type: ignore[attr-defined]


_install_fastmcp_compat()


def require_schematic_runtime() -> None:
    """Fail fast when required schematic runtime dependencies are missing."""
    missing: list[str] = []
    for module_name, package_name in _SCHEMATIC_RUNTIME_DEPENDENCIES:
        try:
            importlib.import_module(module_name)
        except ImportError:
            missing.append(package_name)

    if missing:
        missing_text = ", ".join(missing)
        raise RuntimeError(
            "Missing required schematic dependencies: "
            f"{missing_text}. Install the package dependencies before running kicad-mcp."
        )


def add_cleanup_handler(handler: Callable) -> None:
    """Register a function to be called during cleanup.

    Args:
        handler: Function to call during cleanup
    """
    cleanup_handlers.append(handler)


def run_cleanup_handlers() -> None:
    """Run all registered cleanup handlers."""
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
    require_schematic_runtime()

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
    register_all_tools(mcp)
    _instrument_registered_tools(mcp)
    _apply_tool_profile(mcp, get_tool_profile())

    # Compatibility shim: older tests use get_tools()/get_resource_templates() returning dicts.
    async def _compat_get_tools():
        tools = await _list_mcp_tools(mcp)
        return {t.name: t for t in tools}

    async def _compat_get_resource_templates():
        templates = await _list_mcp_resource_templates(mcp)
        return {t.uri_template for t in templates}

    mcp.get_tools = _compat_get_tools
    mcp.get_resource_templates = _compat_get_resource_templates

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


def _max_inline_bytes() -> int:
    try:
        return max(0, int(os.getenv("KICAD_MCP_MAX_INLINE_BYTES", str(DEFAULT_MAX_INLINE_BYTES))))
    except ValueError:
        return DEFAULT_MAX_INLINE_BYTES


def _json_bytes(payload: Any) -> int:
    return len(json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8"))


def _artifact_root_for_payload(payload: Any) -> Path:
    configured = os.getenv("KICAD_MCP_ARTIFACT_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if isinstance(payload, dict):
        project_path = payload.get("project_path")
        if isinstance(project_path, str) and project_path:
            project = Path(project_path).expanduser()
            base = project.parent if project.suffix else project
            return (base / ".kicad_mcp" / "artifacts").resolve()
    return (Path.cwd() / ".kicad_mcp" / "artifacts").resolve()


def _write_response_artifact(tool_name: str, request_id: str, payload: Any) -> Path:
    artifact_root = _artifact_root_for_payload(payload)
    artifact_root.mkdir(parents=True, exist_ok=True)
    safe_tool_name = "".join(
        character if character.isalnum() or character in "-_" else "_" for character in tool_name
    )
    artifact_path = artifact_root / f"{safe_tool_name}_{request_id}.json"
    artifact_path.write_text(
        json.dumps(payload, default=str, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return artifact_path


def _compact_oversized_value(
    value: Any,
    *,
    artifact_path: str,
    max_inline_bytes: int,
    omitted_fields: list[str],
    field_path: str,
) -> Any:
    try:
        value_bytes = _json_bytes(value)
    except TypeError:
        return value
    field_budget = max(4096, max_inline_bytes // 4)
    large_field_threshold = min(1024, max(256, max_inline_bytes // 10))
    if isinstance(value, dict):
        compacted: dict[str, Any] = {}
        for key, child in value.items():
            child_path = f"{field_path}.{key}" if field_path else str(key)
            child_bytes = _json_bytes(child)
            if str(key) in LARGE_RESPONSE_FIELD_NAMES and child_bytes > large_field_threshold:
                omitted_fields.append(child_path)
                compacted[key] = {
                    "omitted": True,
                    "bytes": child_bytes,
                    "artifact_path": artifact_path,
                }
                continue
            if child_bytes > field_budget:
                omitted_fields.append(child_path)
                compacted[key] = {
                    "omitted": True,
                    "bytes": child_bytes,
                    "artifact_path": artifact_path,
                }
                continue
            compacted[key] = _compact_oversized_value(
                child,
                artifact_path=artifact_path,
                max_inline_bytes=max_inline_bytes,
                omitted_fields=omitted_fields,
                field_path=child_path,
            )
        for key in LARGE_RESPONSE_FIELD_NAMES:
            omitted_marker = compacted.get(key)
            if isinstance(omitted_marker, dict) and omitted_marker.get("omitted") is True:
                compacted[f"{key}_omitted"] = True
        return compacted
    if isinstance(value, list) and len(value) > 25 and value_bytes > field_budget:
        omitted_fields.append(field_path or "<list>")
        return {
            "omitted": True,
            "item_count": len(value),
            "bytes": value_bytes,
            "artifact_path": artifact_path,
        }
    return value


def _minimal_truncated_response(
    tool_name: str,
    payload: Any,
    *,
    max_inline_bytes: int,
    original_inline_bytes: int,
    artifact_path: str | None = None,
    artifact_error: str | None = None,
) -> dict[str, Any]:
    success = payload.get("success", True) if isinstance(payload, dict) else True
    response: dict[str, Any] = {
        "success": success,
        "tool": tool_name,
        "truncated": True,
        "payload_policy": {
            "max_inline_bytes": max_inline_bytes,
            "original_inline_bytes": original_inline_bytes,
        },
    }
    if artifact_path:
        response["artifact_path"] = artifact_path
        response["payload_policy"]["artifact_path"] = artifact_path
    if artifact_error:
        response["payload_policy"]["artifact_error"] = artifact_error
    return response


def _enforce_response_policy(tool_name: str, request_id: str, payload: Any) -> Any:
    max_inline_bytes = _max_inline_bytes()
    if max_inline_bytes <= 0:
        return payload
    payload_bytes = _json_bytes(payload)
    if payload_bytes <= max_inline_bytes:
        return payload
    try:
        artifact_path = _write_response_artifact(tool_name, request_id, payload)
    except Exception as exc:
        logging.error(
            "MCP tool response artifact write failed request_id=%s tool=%s error=%s",
            request_id,
            tool_name,
            exc,
            exc_info=True,
        )
        return _minimal_truncated_response(
            tool_name,
            payload,
            max_inline_bytes=max_inline_bytes,
            original_inline_bytes=payload_bytes,
            artifact_error=str(exc),
        )
    artifact_path_text = str(artifact_path)
    omitted_fields: list[str] = []
    compacted = _compact_oversized_value(
        payload,
        artifact_path=artifact_path_text,
        max_inline_bytes=max_inline_bytes,
        omitted_fields=omitted_fields,
        field_path="",
    )
    if isinstance(compacted, dict):
        compacted["truncated"] = True
        compacted["artifact_path"] = artifact_path_text
        compacted["payload_policy"] = {
            "max_inline_bytes": max_inline_bytes,
            "original_inline_bytes": payload_bytes,
            "artifact_path": artifact_path_text,
            "omitted_fields": omitted_fields,
        }
        if _json_bytes(compacted) <= max_inline_bytes:
            return compacted
    return _minimal_truncated_response(
        tool_name,
        payload,
        max_inline_bytes=max_inline_bytes,
        original_inline_bytes=payload_bytes,
        artifact_path=artifact_path_text,
    )


def _looks_like_timeout(exc: BaseException) -> bool:
    return "timeout" in exc.__class__.__name__.lower() or "timed out" in str(exc).lower()


def _instrument_registered_tools(mcp: FastMCP) -> None:
    tool_manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(tool_manager, "_tools", {})
    for tool_name, tool in tools.items():
        fn = getattr(tool, "fn", None)
        if not callable(fn) or getattr(fn, "_kicad_mcp_instrumented", False):
            continue
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(
                *args: Any, __fn: Callable = fn, __tool_name: str = tool_name, **kwargs: Any
            ) -> Any:
                request_id = uuid.uuid4().hex[:12]
                started = time.monotonic()
                logging.info(
                    "MCP tool request start request_id=%s tool=%s", request_id, __tool_name
                )
                try:
                    result = await __fn(*args, **kwargs)
                    result = _enforce_response_policy(__tool_name, request_id, result)
                    duration_ms = (time.monotonic() - started) * 1000
                    logging.info(
                        "MCP tool request end request_id=%s tool=%s duration_ms=%.1f payload_bytes=%d",
                        request_id,
                        __tool_name,
                        duration_ms,
                        _json_bytes(result),
                    )
                    return result
                except Exception as exc:
                    duration_ms = (time.monotonic() - started) * 1000
                    logging.exception(
                        "MCP tool request failed request_id=%s tool=%s duration_ms=%.1f timeout=%s",
                        request_id,
                        __tool_name,
                        duration_ms,
                        _looks_like_timeout(exc),
                    )
                    raise

            async_wrapper._kicad_mcp_instrumented = True  # type: ignore[attr-defined]
            tool.fn = async_wrapper
            continue

        @functools.wraps(fn)
        def sync_wrapper(
            *args: Any, __fn: Callable = fn, __tool_name: str = tool_name, **kwargs: Any
        ) -> Any:
            request_id = uuid.uuid4().hex[:12]
            started = time.monotonic()
            logging.info("MCP tool request start request_id=%s tool=%s", request_id, __tool_name)
            try:
                result = __fn(*args, **kwargs)
                result = _enforce_response_policy(__tool_name, request_id, result)
                duration_ms = (time.monotonic() - started) * 1000
                logging.info(
                    "MCP tool request end request_id=%s tool=%s duration_ms=%.1f payload_bytes=%d",
                    request_id,
                    __tool_name,
                    duration_ms,
                    _json_bytes(result),
                )
                return result
            except Exception as exc:
                duration_ms = (time.monotonic() - started) * 1000
                logging.exception(
                    "MCP tool request failed request_id=%s tool=%s duration_ms=%.1f timeout=%s",
                    request_id,
                    __tool_name,
                    duration_ms,
                    _looks_like_timeout(exc),
                )
                raise

        sync_wrapper._kicad_mcp_instrumented = True  # type: ignore[attr-defined]
        tool.fn = sync_wrapper


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


async def _list_mcp_tools(mcp: FastMCP) -> list[Any]:
    """Return registered tools across FastMCP API variants."""
    if hasattr(mcp, "list_tools"):
        return await mcp.list_tools()
    if hasattr(mcp, "_list_tools"):
        return await mcp._list_tools()
    raise AttributeError("FastMCP instance does not expose list_tools or _list_tools")


async def _list_mcp_resource_templates(mcp: FastMCP) -> list[Any]:
    """Return resource templates across FastMCP API variants."""
    if hasattr(mcp, "list_resource_templates"):
        return await mcp.list_resource_templates()
    if hasattr(mcp, "_list_resource_templates"):
        return await mcp._list_resource_templates()
    raise AttributeError(
        "FastMCP instance does not expose list_resource_templates or _list_resource_templates"
    )


def _apply_tool_profile(mcp: FastMCP, profile: str) -> None:
    """Hide tools that do not belong to the configured LLM tool surface."""
    import asyncio

    # Discover registered tool names using the public list_tools() API.
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                tools = pool.submit(asyncio.run, _list_mcp_tools(mcp)).result()
        else:
            tools = loop.run_until_complete(_list_mcp_tools(mcp))
    except RuntimeError:
        tools = asyncio.run(_list_mcp_tools(mcp))

    tool_names = [t.name for t in tools]

    allowed_tools = set(AGENT_PROFILE_TOOLS)
    if profile in {"advanced", "debug", "all"}:
        allowed_tools.update(ADVANCED_PROFILE_TOOLS)
    if profile in {"debug", "all"}:
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
