"""Public MCP tool registration for the simplified design-intent workflow."""

from typing import Any

from fastmcp import FastMCP

from kicad_mcp.schematic_engine.apply_jobs import (
    cancel_job,
    get_job_result,
    get_job_status,
    start_apply_job,
)
from kicad_mcp.tools import creation_tools as ct
from kicad_mcp.utils.design_intent_compiler import design_intent_schema


def register_design_intent_tools(mcp: FastMCP) -> None:
    """Register the public design-intent tool surface."""

    @mcp.tool()
    def schematic_preview_design_intent(
        project_path: str,
        intent: dict[str, Any],
    ) -> dict[str, Any]:
        """Preview schematic generation readiness from high-level design intent."""
        resolved_project = ct._resolve_project_alias(project_path, None, None)
        return ct._preview_design_intent_netlist_first(
            resolved_project,
            intent or {},
            visual_style="professional_blocks",
        )

    @mcp.tool()
    def schematic_apply_design_intent(
        project_path: str,
        intent: dict[str, Any],
    ) -> dict[str, Any]:
        """Blocking compatibility apply; agents should use schematic_start_design_intent_job."""
        resolved_project = ct._resolve_project_alias(project_path, None, None)
        return ct._apply_via_netlist_first_engine(
            resolved_project,
            intent or {},
        )

    @mcp.tool()
    def schematic_start_design_intent_job(
        project_path: str,
        intent: dict[str, Any],
    ) -> dict[str, Any]:
        """Start an asynchronous, cancellable schematic apply job."""
        resolved_project = ct._resolve_project_alias(project_path, None, None)
        return start_apply_job(resolved_project, intent or {})

    @mcp.tool()
    def schematic_get_job_status(job_id: str) -> dict[str, Any]:
        """Poll progress for an asynchronous schematic apply job."""
        return get_job_status(job_id)

    @mcp.tool()
    def schematic_get_job_result(job_id: str) -> dict[str, Any]:
        """Fetch the final result for a completed schematic apply job."""
        return get_job_result(job_id)

    @mcp.tool()
    def schematic_cancel_job(job_id: str) -> dict[str, Any]:
        """Request cooperative cancellation for a queued or running schematic apply job."""
        return cancel_job(job_id)

    @mcp.tool()
    def schematic_engine_status() -> dict[str, Any]:
        """Report readiness of the required schematic-generation runtime."""
        kicad_cli_available = False
        try:
            cli_path = ct.get_kicad_cli_path(required=False)
            kicad_cli_available = cli_path is not None
        except Exception:
            pass

        skidl_available = False
        try:
            from kicad_mcp.schematic_engine.skidl_compiler import _SKIDL_AVAILABLE

            skidl_available = _SKIDL_AVAILABLE
        except Exception:
            pass

        kiutils_available = False
        kicad_skip_available = False
        try:
            from kicad_mcp.schematic_engine.schematic_writer import (
                _KICAD_SKIP_AVAILABLE,
                _KIUTILS_AVAILABLE,
            )

            kiutils_available = _KIUTILS_AVAILABLE
            kicad_skip_available = _KICAD_SKIP_AVAILABLE
        except Exception:
            pass

        ready = (
            kicad_cli_available and skidl_available and kiutils_available and kicad_skip_available
        )
        return {
            "engine": "skidl_kiutils_kicad_cli",
            "skidl": "installed" if skidl_available else "missing",
            "kiutils": "installed" if kiutils_available else "missing",
            "kicad_skip": "installed" if kicad_skip_available else "missing",
            "kicad_cli_available": kicad_cli_available,
            "ready": ready,
        }

    @mcp.tool()
    def schematic_design_intent_schema(section: str = "all") -> dict[str, Any]:
        """Return compact schema examples for asynchronous design-intent apply jobs."""
        return design_intent_schema(section)
