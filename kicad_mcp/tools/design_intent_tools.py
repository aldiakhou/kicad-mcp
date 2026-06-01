"""Public MCP tool registration for the simplified design-intent workflow."""

from pathlib import Path
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
    def schematic_export_candidate_to_project(
        project_path: str,
        candidate_schematic_path: str | None = None,
        job_id: str | None = None,
        run_erc: bool = True,
        force: bool = False,
    ) -> dict[str, Any]:
        """Promote a saved candidate schematic artifact into the live project."""
        resolved_project = ct._resolve_project_alias(project_path, None, None)
        return _promote_candidate_schematic(
            resolved_project,
            candidate_schematic_path=candidate_schematic_path,
            job_id=job_id,
            run_erc=run_erc,
            force=force,
        )

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


def _promote_candidate_schematic(
    project_path: str,
    *,
    candidate_schematic_path: str | None,
    job_id: str | None,
    run_erc: bool,
    force: bool,
) -> dict[str, Any]:
    try:
        validated_project = ct.validate_local_path(project_path, "project", must_exist=True)
        project = Path(validated_project)
        project_dir = project.parent
        project_root_name = f"{project.stem}.kicad_sch"
        source_dir = _candidate_source_dir(project_dir, candidate_schematic_path, job_id)
        if source_dir is None:
            return {
                "success": False,
                "tool": "schematic_export_candidate_to_project",
                "project_path": validated_project,
                "error": "candidate_schematic_path or job_id is required",
            }
        if not source_dir.exists():
            return {
                "success": False,
                "tool": "schematic_export_candidate_to_project",
                "project_path": validated_project,
                "candidate_dir": str(source_dir),
                "error": "candidate schematic directory does not exist",
            }
        candidate_files = sorted(source_dir.glob("*.kicad_sch"))
        if not candidate_files:
            return {
                "success": False,
                "tool": "schematic_export_candidate_to_project",
                "project_path": validated_project,
                "candidate_dir": str(source_dir),
                "error": "candidate directory contains no .kicad_sch files",
            }
        root_candidate = source_dir / project_root_name
        if not root_candidate.exists():
            return {
                "success": False,
                "tool": "schematic_export_candidate_to_project",
                "project_path": validated_project,
                "candidate_dir": str(source_dir),
                "error": f"candidate root schematic not found: {project_root_name}",
                "available_schematics": [path.name for path in candidate_files],
            }

        backups: list[dict[str, Any]] = []
        promoted: list[str] = []
        try:
            for source in candidate_files:
                destination = project_dir / source.name
                if destination.exists():
                    backups.append(ct.create_file_backup(str(destination)))
                ct.atomic_write_text(destination, source.read_text(encoding="utf-8"))
                promoted.append(str(destination))

            validation = ct._generated_schematic_report(str(project_dir / project_root_name), run_erc=run_erc)
            if not validation.get("success") and not force:
                for backup in reversed(backups):
                    ct.restore_backup_manifest(backup["backup_path"])
                return {
                    "success": False,
                    "tool": "schematic_export_candidate_to_project",
                    "project_path": validated_project,
                    "candidate_dir": str(source_dir),
                    "changed": False,
                    "rolled_back": True,
                    "error": "candidate promotion failed validation",
                    "validation": validation,
                }
        except Exception as exc:
            for backup in reversed(backups):
                ct.restore_backup_manifest(backup["backup_path"])
            return {
                "success": False,
                "tool": "schematic_export_candidate_to_project",
                "project_path": validated_project,
                "candidate_dir": str(source_dir),
                "changed": False,
                "rolled_back": True,
                "error": str(exc),
            }

        return {
            "success": True,
            "tool": "schematic_export_candidate_to_project",
            "project_path": validated_project,
            "candidate_dir": str(source_dir),
            "changed": True,
            "promoted_schematics": promoted,
            "backup_paths": [backup["backup_path"] for backup in backups],
            "validation": validation,
            "recommended_next_tool": "pcb_preview_layout_intent",
        }
    except Exception as exc:
        return {
            "success": False,
            "tool": "schematic_export_candidate_to_project",
            "project_path": project_path,
            "error": str(exc),
        }


def _candidate_source_dir(
    project_dir: Path,
    candidate_schematic_path: str | None,
    job_id: str | None,
) -> Path | None:
    if candidate_schematic_path:
        candidate_path = Path(candidate_schematic_path).expanduser().resolve()
        return candidate_path if candidate_path.is_dir() else candidate_path.parent
    if job_id:
        return project_dir / ".kicad_mcp" / "engine_artifacts" / job_id / "failed_schematics"
    return None
