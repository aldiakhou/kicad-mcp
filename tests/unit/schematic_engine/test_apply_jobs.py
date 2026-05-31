from __future__ import annotations

import threading
import time

import pytest

import kicad_mcp.schematic_engine.apply_jobs as apply_jobs


@pytest.fixture(autouse=True)
def reset_apply_jobs():
    with apply_jobs._LOCK:
        apply_jobs._JOBS.clear()
    if apply_jobs._EXECUTOR is not None:
        apply_jobs._EXECUTOR.shutdown(wait=True, cancel_futures=True)
        apply_jobs._EXECUTOR = None
    yield
    if apply_jobs._EXECUTOR is not None:
        apply_jobs._EXECUTOR.shutdown(wait=True, cancel_futures=True)
        apply_jobs._EXECUTOR = None
    with apply_jobs._LOCK:
        apply_jobs._JOBS.clear()


def _wait_terminal(job_id: str, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        last = apply_jobs.get_job_status(job_id)
        if last.get("status") in apply_jobs.TERMINAL_STATUSES:
            return last
        time.sleep(0.01)
    raise AssertionError(f"job did not finish: {last}")


def test_start_apply_job_reports_progress_and_result(tmp_path, monkeypatch):
    def fake_apply(
        *,
        project_path,
        intent,
        export_svg,
        job_id,
        cancel_check,
        progress_callback,
    ):
        progress_callback(
            "planning_sheets",
            {"step": 5, "step_count": 12, "message": "Planning schematic sheets"},
        )
        return {
            "success": True,
            "changed": True,
            "stage": "schematic_committed",
            "progress": {"step": 12, "step_count": 12, "message": "Done"},
            "job_id": job_id,
            "project_path": project_path,
            "export_svg": export_svg,
            "part_count": len(intent.get("parts", [])),
            "cancelled": cancel_check(),
        }

    monkeypatch.setattr(apply_jobs, "apply_design_intent_netlist_first", fake_apply)

    started = apply_jobs.start_apply_job(
        str(tmp_path / "demo.kicad_pro"),
        {"parts": [{"ref": "R1", "lib_id": "Device:R", "value": "10k"}]},
    )
    assert started["success"] is True
    assert started["status"] in {"queued", "running"}

    final_status = _wait_terminal(started["job_id"])
    assert final_status["status"] == "succeeded"

    result = apply_jobs.get_job_result(started["job_id"])
    assert result["success"] is True
    assert result["result"]["success"] is True
    assert result["result"]["stage"] == "schematic_committed"
    assert result["result"]["export_svg"] is False
    assert result["record_path"].endswith(f"{started['job_id']}.json")


def test_cancel_running_apply_job_is_cooperative(tmp_path, monkeypatch):
    running = threading.Event()

    def fake_apply(
        *,
        project_path,
        intent,
        export_svg,
        job_id,
        cancel_check,
        progress_callback,
    ):
        running.set()
        progress_callback(
            "kicad_cli_verification",
            {"step": 8, "step_count": 12, "message": "Running KiCad CLI verification"},
        )
        while not cancel_check():
            time.sleep(0.01)
        return {
            "success": False,
            "changed": False,
            "rolled_back": True,
            "stage": "cancelled:after_cli_verification",
            "error": "Job cancelled at stage: after_cli_verification",
        }

    monkeypatch.setattr(apply_jobs, "apply_design_intent_netlist_first", fake_apply)

    started = apply_jobs.start_apply_job(str(tmp_path / "demo.kicad_pro"), {"parts": []})
    assert running.wait(timeout=1.0)

    cancelled = apply_jobs.cancel_job(started["job_id"])
    assert cancelled["success"] is True
    assert cancelled["cancel_requested"] is True

    final_status = _wait_terminal(started["job_id"])
    assert final_status["status"] == "cancelled"

    result = apply_jobs.get_job_result(started["job_id"])
    assert result["success"] is False
    assert result["result"]["rolled_back"] is True
    assert result["result"]["stage"].startswith("cancelled:")
