"""
Tests for MCP startup and stdout-safe behavior.
"""

from __future__ import annotations

from pathlib import Path
import runpy

import pytest

from kicad_mcp.server import create_server
from kicad_mcp.tools.drc_impl.cli_drc import run_drc_via_cli
from kicad_mcp.utils.kicad_api_detection import check_for_cli_api
from kicad_mcp.utils.kicad_cli import KiCadCLIError
from kicad_mcp.utils.netlist_parser import extract_netlist


class FakeContext:
    """Minimal async context double for tool tests."""

    def __init__(self) -> None:
        self.info_messages: list[str] = []
        self.progress_updates: list[tuple[int, int]] = []

    async def info(self, message: str) -> None:
        self.info_messages.append(message)

    async def report_progress(self, current: int, total: int) -> None:
        self.progress_updates.append((current, total))


@pytest.mark.asyncio
async def test_create_server_registers_smoke_resources_and_tools():
    """The server should initialize and expose core tools/resources."""
    server = create_server()

    tools = await server.get_tools()
    resource_templates = await server.get_resource_templates()

    assert "extract_schematic_netlist" in tools
    assert "run_drc_check" in tools
    assert "kicad://netlist/{schematic_path}" in resource_templates
    assert "kicad://drc/{project_path}" in resource_templates


def test_main_entrypoint_calls_blocking_server_main(monkeypatch):
    """Running main.py should call the blocking server entrypoint directly."""
    import kicad_mcp.server
    import kicad_mcp.utils.env

    called: list[bool] = []

    monkeypatch.setattr(kicad_mcp.server, "main", lambda: called.append(True))
    monkeypatch.setattr(kicad_mcp.utils.env, "load_dotenv", lambda: False)

    main_path = Path(__file__).resolve().parents[2] / "main.py"
    runpy.run_path(str(main_path), run_name="__main__")

    assert called == [True]


def test_extract_netlist_does_not_write_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Netlist extraction should keep stdout clean for MCP stdio transport."""
    schematic_path = tmp_path / "minimal.kicad_sch"
    schematic_path.write_text('(kicad_sch (version 20231120) (generator "pytest"))')

    result = extract_netlist(str(schematic_path))
    captured = capsys.readouterr()

    assert captured.out == ""
    assert result["netlist_quality"] == "partial"
    assert result["limitations"]


@pytest.mark.asyncio
async def test_extract_schematic_netlist_reports_missing_file_via_async_ctx():
    """Async tool paths should await ctx.info instead of leaking un-awaited coroutines."""
    server = create_server()
    tools = await server.get_tools()
    fake_ctx = FakeContext()
    missing_path = "/tmp/does-not-exist.kicad_sch"

    result = await tools["extract_schematic_netlist"].fn(missing_path, fake_ctx)

    assert result == {"success": False, "error": f"Schematic file not found: {missing_path}"}
    assert fake_ctx.info_messages == [f"Schematic file not found: {missing_path}"]


@pytest.mark.asyncio
async def test_run_drc_via_cli_returns_structured_error_without_stdout(
    monkeypatch, capsys: pytest.CaptureFixture[str]
):
    """Missing KiCad CLI should return a structured error and keep stdout clean."""
    monkeypatch.setattr(
        "kicad_mcp.tools.drc_impl.cli_drc.get_kicad_cli_path",
        lambda: (_ for _ in ()).throw(KiCadCLIError("KiCad CLI not found for tests")),
    )

    result = await run_drc_via_cli("/tmp/test.kicad_pcb", FakeContext())
    captured = capsys.readouterr()

    assert captured.out == ""
    assert result["success"] is False
    assert result["error"] == "KiCad CLI not found for tests"


def test_check_for_cli_api_uses_shared_cli_detector(monkeypatch):
    """CLI detection should delegate to the shared KiCad CLI utility."""
    monkeypatch.setattr("kicad_mcp.utils.kicad_api_detection.is_kicad_cli_available", lambda: True)
    assert check_for_cli_api() is True

    monkeypatch.setattr("kicad_mcp.utils.kicad_api_detection.is_kicad_cli_available", lambda: False)
    assert check_for_cli_api() is False
