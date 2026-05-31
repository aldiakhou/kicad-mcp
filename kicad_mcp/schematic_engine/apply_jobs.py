"""Asynchronous schematic apply job manager."""

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

from kicad_mcp.schematic_engine.pipeline import apply_design_intent_netlist_first

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
_JOBS: dict[str, ApplyJob] = {}
_LOCK = threading.RLock()
_EXECUTOR: ThreadPoolExecutor | None = None


@dataclass
class ApplyJob:
    """Mutable state for one schematic apply job."""

    job_id: str
    project_path: str
    intent: dict[str, Any]
    status: str = "queued"
    stage: str = "queued"
    progress: dict[str, Any] = field(
        default_factory=lambda: {"step": 0, "step_count": 12, "message": "Queued"}
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


def start_apply_job(project_path: str, intent: dict[str, Any]) -> dict[str, Any]:
    """Queue a design-intent apply job and return a monitor handle."""
    resolved_project = str(Path(project_path).expanduser().resolve())
    job_id = f"apply-{uuid.uuid4().hex[:12]}"
    job = ApplyJob(job_id=job_id, project_path=resolved_project, intent=dict(intent or {}))
    job.record_path = str(_job_record_path(resolved_project, job_id))
    with _LOCK:
        _JOBS[job_id] = job
        _write_job_record_locked(job)
        job.future = _executor().submit(_run_apply_job, job_id)
    return _public_status(job, include_result=False) | {
        "success": True,
        "tool": "schematic_start_design_intent_job",
        "recommended_next_tool": "schematic_get_job_status",
        "result_tool": "schematic_get_job_result",
        "cancel_tool": "schematic_cancel_job",
    }


def get_job_status(job_id: str) -> dict[str, Any]:
    """Return current status and progress for a queued/running/completed job."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return _unknown_job(job_id)
        return _public_status(job, include_result=False) | {
            "success": True,
            "tool": "schematic_get_job_status",
            "recommended_next_tool": _next_tool_for_status(job),
        }


def get_job_result(job_id: str) -> dict[str, Any]:
    """Return the final apply result without blocking for job completion."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return _unknown_job(job_id)
        if job.status not in TERMINAL_STATUSES:
            return _public_status(job, include_result=False) | {
                "success": False,
                "tool": "schematic_get_job_result",
                "stage": "job_not_finished",
                "error": "Job is not finished yet; poll schematic_get_job_status.",
                "recommended_next_tool": "schematic_get_job_status",
            }
        return _public_status(job, include_result=True) | {
            "success": job.status == "succeeded",
            "tool": "schematic_get_job_result",
        }


def cancel_job(job_id: str) -> dict[str, Any]:
    """Request cancellation for a queued or running schematic apply job."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return _unknown_job(job_id)
        if job.status in TERMINAL_STATUSES:
            return _public_status(job, include_result=False) | {
                "success": False,
                "tool": "schematic_cancel_job",
                "error": f"Job is already {job.status}.",
            }

        job.cancel_requested = True
        job.updated_at = time.time()
        cancelled_future = bool(job.future and job.future.cancel())
        if cancelled_future:
            job.status = "cancelled"
            job.stage = "cancelled:queued"
            job.finished_at = time.time()
            job.result = {
                "success": False,
                "changed": False,
                "rolled_back": True,
                "stage": "cancelled:queued",
                "error": "Job cancelled before it started.",
            }
        elif job.status == "queued":
            job.status = "cancelling"
            job.stage = "cancel_requested"
            job.progress = {
                "step": 0,
                "step_count": 12,
                "message": "Cancellation requested before job started",
            }
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
            "tool": "schematic_cancel_job",
            "cancel_requested": True,
            "note": "Cancellation is cooperative; a running KiCad CLI command may finish before rollback.",
            "recommended_next_tool": "schematic_get_job_status",
        }


def _run_apply_job(job_id: str) -> None:
    with _LOCK:
        job = _JOBS[job_id]
        if job.cancel_requested:
            job.status = "cancelled"
            job.stage = "cancelled:queued"
            job.finished_at = time.time()
            job.result = {
                "success": False,
                "changed": False,
                "rolled_back": True,
                "stage": "cancelled:queued",
                "error": "Job cancelled before it started.",
            }
            _write_job_record_locked(job)
            return
        job.status = "running"
        job.stage = "starting"
        job.started_at = time.time()
        job.updated_at = job.started_at
        job.progress = {"step": 0, "step_count": 12, "message": "Starting apply job"}
        _write_job_record_locked(job)

    def cancel_check() -> bool:
        with _LOCK:
            current = _JOBS[job_id]
            return current.cancel_requested

    def progress_callback(stage: str, progress: dict[str, Any]) -> None:
        with _LOCK:
            current = _JOBS[job_id]
            if current.status not in TERMINAL_STATUSES:
                current.stage = stage
                current.progress = dict(progress)
                current.updated_at = time.time()
                _write_job_record_locked(current)

    try:
        result = apply_design_intent_netlist_first(
            project_path=job.project_path,
            intent=job.intent,
            export_svg=False,
            job_id=job.job_id,
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        result = {
            "success": False,
            "changed": False,
            "rolled_back": True,
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


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        try:
            workers = max(1, int(os.getenv("KICAD_MCP_APPLY_WORKERS", "1")))
        except ValueError:
            workers = 1
        _EXECUTOR = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kicad-mcp-apply")
    return _EXECUTOR


def _job_record_path(project_path: str, job_id: str) -> Path:
    project = Path(project_path)
    project_dir = project.parent if project.suffix else project
    return project_dir / ".kicad_mcp" / "jobs" / f"{job_id}.json"


def _write_job_record_locked(job: ApplyJob) -> None:
    if not job.record_path:
        return
    path = Path(job.record_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_public_status(job, include_result=True), indent=2, default=str),
        encoding="utf-8",
    )


def _public_status(job: ApplyJob, *, include_result: bool) -> dict[str, Any]:
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
        "error": f"Unknown schematic apply job: {job_id}",
    }


def _next_tool_for_status(job: ApplyJob) -> str:
    if job.status in TERMINAL_STATUSES:
        return "schematic_get_job_result"
    return "schematic_get_job_status"
