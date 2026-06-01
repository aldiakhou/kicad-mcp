"""Asynchronous PCB layout job manager."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import threading
import time
from typing import Any
import uuid

from kicad_mcp.pcb_engine.autorouter import autoroute_pcb
from kicad_mcp.pcb_engine.intent import normalize_pcb_layout_intent
from kicad_mcp.tools import creation_tools as ct
from kicad_mcp.utils.kicad_pcb_s_expr import KiCadPcb

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
PCB_LAYOUT_STEP_COUNT = 8
_JOBS: dict[str, PcbLayoutJob] = {}
_LOCK = threading.RLock()
_EXECUTOR: ThreadPoolExecutor | None = None


@dataclass
class PcbLayoutJob:
    """Mutable state for one PCB layout job."""

    job_id: str
    project_path: str
    intent: dict[str, Any]
    status: str = "queued"
    stage: str = "queued"
    progress: dict[str, Any] = field(
        default_factory=lambda: {"step": 0, "step_count": PCB_LAYOUT_STEP_COUNT, "message": "Queued"}
    )
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancel_requested: bool = False
    result: dict[str, Any] | None = None
    error: str | None = None
    record_path: str | None = None
    future: Future[Any] | None = field(default=None, repr=False)


def preview_layout_intent(project_path: str, intent: dict[str, Any] | None) -> dict[str, Any]:
    """Preview PCB layout readiness without writing board files."""
    try:
        normalized = normalize_pcb_layout_intent(intent)
        validated_project = ct.validate_local_path(project_path, "project", must_exist=True)
        files = ct.get_project_files(validated_project)
        issues: list[dict[str, Any]] = []
        if "schematic" not in files:
            issues.append(
                {
                    "category": "blocking_generation_issue",
                    "type": "missing_schematic",
                    "message": "No schematic file found for PCB sync.",
                }
            )
            native = {"success": False, "component_count": 0, "net_count": 0}
        else:
            native = ct._native_netlist_for_tool(files["schematic"])
            if not native.get("success"):
                issues.append(
                    {
                        "category": "blocking_connectivity_issue",
                        "type": "native_netlist_failed",
                        "message": native.get("error", "Native schematic netlist export failed"),
                    }
                )
        component_count = int(native.get("component_count", 0) or 0)
        footprint_count = sum(
            1
            for component in native.get("components", {}).values()
            if isinstance(component, dict) and component.get("footprint")
        )
        if component_count > 0 and footprint_count == 0:
            issues.append(
                {
                    "category": "blocking_generation_issue",
                    "type": "missing_footprints",
                    "message": "Schematic components do not have footprint properties.",
                }
            )
        return {
            "success": True,
            "tool": "pcb_preview_layout_intent",
            "stage": "preview",
            "changed": False,
            "ready_to_start": not issues,
            "blocking_issue_count": len(issues),
            "issues": issues,
            "project_path": validated_project,
            "pcb_path": files.get("pcb"),
            "schematic_path": files.get("schematic"),
            "summary": {
                "component_count": component_count,
                "footprint_count": footprint_count,
                "net_count": native.get("net_count", 0),
                "board": normalized["board"],
                "placement": normalized["placement"],
                "routing": normalized["routing"],
                "validation": normalized["validation"],
            },
            "recommended_apply_tool": "pcb_start_layout_job",
            "recommended_status_tool": "pcb_get_layout_job_status",
        }
    except Exception as exc:
        return {
            "success": False,
            "tool": "pcb_preview_layout_intent",
            "stage": "preview_failed",
            "changed": False,
            "project_path": project_path,
            "error": str(exc),
        }


def start_layout_job(project_path: str, intent: dict[str, Any] | None) -> dict[str, Any]:
    """Queue a PCB layout job and return a monitor handle."""
    resolved_project = str(Path(project_path).expanduser().resolve())
    job_id = f"pcb-{uuid.uuid4().hex[:12]}"
    job = PcbLayoutJob(job_id=job_id, project_path=resolved_project, intent=dict(intent or {}))
    job.record_path = str(_job_record_path(resolved_project, job_id))
    with _LOCK:
        _JOBS[job_id] = job
        _write_job_record_locked(job)
        job.future = _executor().submit(_run_layout_job, job_id)
    return _public_status(job, include_result=False) | {
        "success": True,
        "tool": "pcb_start_layout_job",
        "recommended_next_tool": "pcb_get_layout_job_status",
        "result_tool": "pcb_get_layout_job_result",
        "cancel_tool": "pcb_cancel_layout_job",
    }


def get_layout_job_status(job_id: str) -> dict[str, Any]:
    """Return current status and progress for a PCB layout job."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return _unknown_job(job_id)
        return _public_status(job, include_result=False) | {
            "success": True,
            "tool": "pcb_get_layout_job_status",
            "recommended_next_tool": _next_tool_for_status(job),
        }


def get_layout_job_result(job_id: str) -> dict[str, Any]:
    """Return the final PCB layout job result without blocking."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return _unknown_job(job_id)
        if job.status not in TERMINAL_STATUSES:
            return _public_status(job, include_result=False) | {
                "success": False,
                "tool": "pcb_get_layout_job_result",
                "stage": "job_not_finished",
                "error": "Job is not finished yet; poll pcb_get_layout_job_status.",
                "recommended_next_tool": "pcb_get_layout_job_status",
            }
        return _public_status(job, include_result=True) | {
            "success": job.status == "succeeded",
            "tool": "pcb_get_layout_job_result",
        }


def cancel_layout_job(job_id: str) -> dict[str, Any]:
    """Request cooperative cancellation for a queued or running PCB layout job."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return _unknown_job(job_id)
        if job.status in TERMINAL_STATUSES:
            return _public_status(job, include_result=False) | {
                "success": False,
                "tool": "pcb_cancel_layout_job",
                "error": f"Job is already {job.status}.",
            }
        job.cancel_requested = True
        job.updated_at = time.time()
        cancelled_future = bool(job.future and job.future.cancel())
        if cancelled_future:
            job.status = "cancelled"
            job.stage = "cancelled:queued"
            job.finished_at = time.time()
            job.result = _cancelled_result(job, "queued")
        else:
            previous_stage = job.stage
            job.status = "cancelling"
            job.stage = "cancel_requested"
            job.progress = {
                **job.progress,
                "message": f"Cancellation requested; current stage is {previous_stage}",
            }
        _write_job_record_locked(job)
        return _public_status(job, include_result=False) | {
            "success": True,
            "tool": "pcb_cancel_layout_job",
            "cancel_requested": True,
            "note": "Cancellation is cooperative; running KiCad CLI validation may finish before rollback.",
            "recommended_next_tool": "pcb_get_layout_job_status",
        }


def validate_layout(project_path: str, *, run_drc: bool = False, require_clean_drc: bool = False) -> dict[str, Any]:
    """Validate current PCB sync, placement, ratsnest, and optional DRC status."""
    try:
        validated_project = ct.validate_local_path(project_path, "project", must_exist=True)
        files = ct.get_project_files(validated_project)
        if "pcb" not in files:
            return {
                "success": False,
                "tool": "pcb_validate_layout",
                "project_path": validated_project,
                "stage": "missing_pcb",
                "error": "PCB file not found.",
            }
        pcb = KiCadPcb.from_file(files["pcb"])
        quality = ct._pcb_quality_report(validated_project, files["pcb"], pcb)
        drc = {"success": True, "skipped": True, "reason": "run_drc=False"}
        if run_drc:
            drc = ct._run_pcb_drc_sync(files["pcb"])
        blocking: list[str] = []
        if not quality.get("placement_valid"):
            blocking.append("placement warnings remain")
        if run_drc and require_clean_drc and drc.get("total_violations", 0) > 0:
            blocking.append(f"DRC has {drc.get('total_violations')} violation(s)")
        return {
            "success": not blocking,
            "tool": "pcb_validate_layout",
            "project_path": validated_project,
            "pcb_path": files["pcb"],
            "stage": "layout_valid" if not blocking else "layout_blocked",
            "quality": quality,
            "drc": drc,
            "blocking_issues": blocking,
            "recommended_next_tool": "pcb_export_fabrication_package"
            if not blocking
            else "pcb_start_layout_job",
        }
    except Exception as exc:
        return {
            "success": False,
            "tool": "pcb_validate_layout",
            "project_path": project_path,
            "stage": "validation_failed",
            "error": str(exc),
        }


def _run_layout_job(job_id: str) -> None:
    with _LOCK:
        job = _JOBS[job_id]
        if job.cancel_requested:
            _finish_cancelled_locked(job, "queued")
            return
        job.status = "running"
        job.stage = "starting"
        job.started_at = time.time()
        job.updated_at = job.started_at
        job.progress = {
            "step": 0,
            "step_count": PCB_LAYOUT_STEP_COUNT,
            "message": "Starting PCB layout job",
        }
        _write_job_record_locked(job)

    def is_cancelled() -> bool:
        with _LOCK:
            return _JOBS[job_id].cancel_requested

    def progress(stage: str, step: int, message: str) -> None:
        with _LOCK:
            current = _JOBS[job_id]
            if current.status not in TERMINAL_STATUSES:
                current.stage = stage
                current.progress = {
                    "step": step,
                    "step_count": PCB_LAYOUT_STEP_COUNT,
                    "message": message,
                }
                current.updated_at = time.time()
                _write_job_record_locked(current)

    try:
        result = _apply_layout_intent(job.project_path, job.intent, is_cancelled, progress)
    except Exception as exc:
        result = {
            "success": False,
            "changed": False,
            "stage": "job_exception",
            "error": str(exc),
        }

    with _LOCK:
        current = _JOBS[job_id]
        current.result = result
        current.stage = str(result.get("stage") or current.stage)
        current.progress = dict(result.get("progress") or current.progress)
        current.finished_at = time.time()
        current.updated_at = current.finished_at
        current.error = result.get("error")
        if current.stage.startswith("cancelled:") or current.cancel_requested and not result.get("success"):
            current.status = "cancelled"
        elif result.get("success"):
            current.status = "succeeded"
        else:
            current.status = "failed"
        _write_job_record_locked(current)


def _apply_layout_intent(
    project_path: str,
    intent: dict[str, Any],
    is_cancelled: Any,
    progress: Any,
) -> dict[str, Any]:
    normalized = normalize_pcb_layout_intent(intent)
    progress("normalizing", 1, "Normalizing PCB layout intent")
    if is_cancelled():
        return _cancelled_payload(project_path, "after_normalize")

    progress("preview", 2, "Checking schematic and native netlist readiness")
    preview = preview_layout_intent(project_path, intent)
    if not preview.get("success") or not preview.get("ready_to_start"):
        return {
            "success": False,
            "changed": False,
            "project_path": project_path,
            "stage": "preview_failed",
            "preview": preview,
            "error": preview.get("error") or "PCB layout intent is not ready to start.",
        }
    if is_cancelled():
        return _cancelled_payload(project_path, "after_preview")

    progress("sync_and_place", 3, "Syncing PCB footprints from schematic and applying placement")
    completed = ct._complete_pcb_from_schematic(
        project_path,
        normalized["board"]["width_mm"],
        normalized["board"]["height_mm"],
        normalized["placement"]["style"],
        preserve_existing_placement=normalized["placement"]["preserve_existing_placement"],
        place_pcb=True,
        placement_rules=normalized["placement"]["rules"],
    )
    if not completed.get("success"):
        completed["tool"] = "pcb_start_layout_job"
        return completed
    if is_cancelled():
        return _cancelled_payload(project_path, "after_sync_and_place")

    files = ct.get_project_files(ct.validate_local_path(project_path, "project", must_exist=True))
    routing = {
        "success": True,
        "skipped": True,
        "reason": (
            "routing.mode=report_only; ratsnest/topology report is returned below"
            if normalized["routing"]["mode"] == "report_only"
            else "routing.mode is not auto"
        ),
    }
    if normalized["routing"]["mode"] == "auto":
        progress("autoroute", 4, "Routing assigned PCB ratsnest connections")

        def route_mutation(pcb_model: KiCadPcb) -> dict[str, Any]:
            route_result = autoroute_pcb(
                pcb_model,
                normalized["board"]["width_mm"],
                normalized["board"]["height_mm"],
                layer=normalized["routing"]["layer"],
                track_width_mm=normalized["routing"]["track_width_mm"],
                clearance_mm=normalized["routing"]["clearance_mm"],
                grid_mm=normalized["routing"]["grid_mm"],
                max_connections=normalized["routing"]["max_connections"],
                cancel_check=is_cancelled,
            )
            if route_result.get("cancelled"):
                raise RuntimeError("PCB autoroute cancelled")
            return route_result

        routing = ct._apply_transactional_pcb_edit(
            files["pcb"],
            route_mutation,
            run_cli_validation=True,
        )
        if not routing.get("success"):
            if is_cancelled():
                return _cancelled_payload(project_path, "during_autoroute")
            return {
                "success": False,
                "changed": True,
                "project_path": project_path,
                "pcb_path": files["pcb"],
                "stage": "autoroute_failed",
                "error": routing.get("error", "PCB autoroute failed"),
                "sync": completed.get("sync"),
                "placement": completed.get("placement"),
                "routing": routing,
            }
    if is_cancelled():
        return _cancelled_payload(project_path, "after_autoroute")

    progress("quality_report", 5, "Building PCB quality and ratsnest report")
    pcb = KiCadPcb.from_file(files["pcb"])
    quality = ct._pcb_quality_report(project_path, files["pcb"], pcb)
    ratsnest = ct._build_ratsnest(project_path, files["pcb"], pcb)
    if is_cancelled():
        return _cancelled_payload(project_path, "after_quality_report")

    drc = {"success": True, "skipped": True, "reason": "validation.run_drc=False"}
    progress("drc", 6, "Running PCB DRC" if normalized["validation"]["run_drc"] else "Skipping PCB DRC")
    if normalized["validation"]["run_drc"]:
        drc = ct._run_pcb_drc_sync(files["pcb"])
        if normalized["validation"]["require_clean_drc"] and drc.get("total_violations", 0) > 0:
            return {
                "success": False,
                "changed": True,
                "project_path": project_path,
                "pcb_path": files["pcb"],
                "stage": "drc_failed",
                "error": f"PCB DRC has {drc.get('total_violations')} violation(s)",
                "drc": drc,
                "quality": quality,
            }
    if is_cancelled():
        return _cancelled_payload(project_path, "after_drc")

    progress("done", PCB_LAYOUT_STEP_COUNT, "PCB layout job completed")
    status = dict(completed.get("status", {}))
    status["routing_complete"] = bool(quality.get("routing_complete", False))
    status["routing_status"] = quality.get("routing_status", "unknown_needs_drc")
    return {
        "success": True,
        "changed": True,
        "tool": "pcb_start_layout_job",
        "project_path": project_path,
        "pcb_path": files["pcb"],
        "stage": "pcb_layout_committed",
        "status": status,
        "sync": completed.get("sync"),
        "placement": completed.get("placement"),
        "ratsnest": {
            "net_count": ratsnest.get("net_count", 0),
            "connection_count": ratsnest.get("connection_count", 0),
            "routed_connection_count": ratsnest.get("routed_connection_count", 0),
            "expected_connection_count": ratsnest.get("expected_connection_count", 0),
        },
        "routing": routing,
        "quality": quality,
        "drc": drc,
        "intent": normalized,
        "progress": {
            "step": PCB_LAYOUT_STEP_COUNT,
            "step_count": PCB_LAYOUT_STEP_COUNT,
            "message": "PCB layout job completed",
        },
        "recommended_next_tool": "pcb_validate_layout",
    }


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        try:
            workers = max(1, int(os.getenv("KICAD_MCP_PCB_WORKERS", "1")))
        except ValueError:
            workers = 1
        _EXECUTOR = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kicad-mcp-pcb")
    return _EXECUTOR


def _job_record_path(project_path: str, job_id: str) -> Path:
    project = Path(project_path)
    project_dir = project.parent if project.suffix else project
    return project_dir / ".kicad_mcp" / "jobs" / f"{job_id}.json"


def _write_job_record_locked(job: PcbLayoutJob) -> None:
    if not job.record_path:
        return
    path = Path(job.record_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_public_status(job, include_result=True), indent=2, default=str),
        encoding="utf-8",
    )


def _public_status(job: PcbLayoutJob, *, include_result: bool) -> dict[str, Any]:
    now = time.time()
    data: dict[str, Any] = {
        "job_id": job.job_id,
        "project_path": job.project_path,
        "status": job.status,
        "stage": job.stage,
        "progress": job.progress,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "elapsed_seconds": round((job.finished_at or now) - (job.started_at or job.created_at), 3),
        "cancel_requested": job.cancel_requested,
        "record_path": job.record_path,
    }
    if job.error:
        data["error"] = job.error
    if include_result and job.result is not None:
        data["result"] = job.result
    return data


def _unknown_job(job_id: str) -> dict[str, Any]:
    return {
        "success": False,
        "job_id": job_id,
        "stage": "job_not_found",
        "error": f"Unknown PCB layout job: {job_id}",
    }


def _next_tool_for_status(job: PcbLayoutJob) -> str:
    if job.status in TERMINAL_STATUSES:
        return "pcb_get_layout_job_result"
    return "pcb_get_layout_job_status"


def _finish_cancelled_locked(job: PcbLayoutJob, stage: str) -> None:
    job.status = "cancelled"
    job.stage = f"cancelled:{stage}"
    job.finished_at = time.time()
    job.updated_at = job.finished_at
    job.result = _cancelled_result(job, stage)
    _write_job_record_locked(job)


def _cancelled_result(job: PcbLayoutJob, stage: str) -> dict[str, Any]:
    return _cancelled_payload(job.project_path, stage)


def _cancelled_payload(project_path: str, stage: str) -> dict[str, Any]:
    return {
        "success": False,
        "changed": False,
        "project_path": project_path,
        "stage": f"cancelled:{stage}",
        "error": f"PCB layout job cancelled at stage: {stage}",
    }
