from __future__ import annotations

import threading
import time

import pytest

import kicad_mcp.pcb_engine.jobs as pcb_jobs


@pytest.fixture(autouse=True)
def reset_pcb_jobs():
    with pcb_jobs._LOCK:
        pcb_jobs._JOBS.clear()
    if pcb_jobs._EXECUTOR is not None:
        pcb_jobs._EXECUTOR.shutdown(wait=True, cancel_futures=True)
        pcb_jobs._EXECUTOR = None
    yield
    if pcb_jobs._EXECUTOR is not None:
        pcb_jobs._EXECUTOR.shutdown(wait=True, cancel_futures=True)
        pcb_jobs._EXECUTOR = None
    with pcb_jobs._LOCK:
        pcb_jobs._JOBS.clear()


def _wait_terminal(job_id: str, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        last = pcb_jobs.get_layout_job_status(job_id)
        if last.get("status") in pcb_jobs.TERMINAL_STATUSES:
            return last
        time.sleep(0.01)
    raise AssertionError(f"job did not finish: {last}")


def test_start_layout_job_reports_progress_and_result(tmp_path, monkeypatch):
    def fake_apply(project_path, intent, is_cancelled, progress):
        progress("sync_and_place", 3, "Syncing PCB")
        return {
            "success": True,
            "changed": True,
            "stage": "pcb_layout_committed",
            "project_path": project_path,
            "pcb_path": str(tmp_path / "demo.kicad_pcb"),
            "intent": intent,
            "cancelled": is_cancelled(),
            "progress": {"step": 7, "step_count": 7, "message": "Done"},
        }

    monkeypatch.setattr(pcb_jobs, "_apply_layout_intent", fake_apply)

    started = pcb_jobs.start_layout_job(
        str(tmp_path / "demo.kicad_pro"),
        {"board": {"width_mm": 40, "height_mm": 30}},
    )
    assert started["success"] is True

    final_status = _wait_terminal(started["job_id"])
    assert final_status["status"] == "succeeded"

    result = pcb_jobs.get_layout_job_result(started["job_id"])
    assert result["success"] is True
    assert result["result"]["stage"] == "pcb_layout_committed"
    assert result["record_path"].endswith(f"{started['job_id']}.json")


def test_cancel_running_layout_job_is_cooperative(tmp_path, monkeypatch):
    running = threading.Event()

    def fake_apply(project_path, intent, is_cancelled, progress):
        running.set()
        progress("sync_and_place", 3, "Syncing PCB")
        while not is_cancelled():
            time.sleep(0.01)
        return {
            "success": False,
            "changed": False,
            "stage": "cancelled:after_sync_and_place",
            "error": "PCB layout job cancelled",
        }

    monkeypatch.setattr(pcb_jobs, "_apply_layout_intent", fake_apply)

    started = pcb_jobs.start_layout_job(str(tmp_path / "demo.kicad_pro"), {})
    assert running.wait(timeout=1.0)

    cancelled = pcb_jobs.cancel_layout_job(started["job_id"])
    assert cancelled["success"] is True
    assert cancelled["cancel_requested"] is True

    final_status = _wait_terminal(started["job_id"])
    assert final_status["status"] == "cancelled"

    result = pcb_jobs.get_layout_job_result(started["job_id"])
    assert result["success"] is False
    assert result["result"]["stage"].startswith("cancelled:")
