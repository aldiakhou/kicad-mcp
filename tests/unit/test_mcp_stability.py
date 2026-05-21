"""
Tests for MCP startup and stdout-safe behavior.
"""

from __future__ import annotations

import importlib
from pathlib import Path
import runpy

import pytest

import kicad_mcp.config as config
import kicad_mcp.server as server_module
from kicad_mcp.server import create_server, get_transport_config
from kicad_mcp.tools.drc_impl.cli_drc import run_drc_via_cli
from kicad_mcp.utils.kicad_api_detection import check_for_cli_api
from kicad_mcp.utils.kicad_cli import KiCadCLIError, get_kicad_cli_path
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


class FakeRunnableServer:
    """Minimal FastMCP-like server double for transport tests."""

    def __init__(self) -> None:
        self.run_kwargs: dict[str, object] | None = None

    def run(self, *, transport: str = "stdio", host: str | None = None, port: int | None = None, path: str | None = None) -> None:
        self.run_kwargs = {"transport": transport, "host": host, "port": port, "path": path}


@pytest.mark.asyncio
async def test_create_server_registers_smoke_resources_and_tools():
    """The server should initialize and expose core tools/resources."""
    server = create_server()

    tools = await server.get_tools()
    resource_templates = await server.get_resource_templates()

    assert "extract_schematic_netlist" in tools
    assert "run_drc_check" in tools
    assert "validate_schematic_syntax" in tools
    assert "schematic_move_symbol" in tools
    assert "export_schematic_svg" in tools
    assert "kicad://netlist/{schematic_path}" in resource_templates
    assert "kicad://drc/{project_path}" in resource_templates
    assert server_module._server_instance is server

    server_module.shutdown_server()
    assert server_module._server_instance is None


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


def test_transport_config_defaults_to_stdio(monkeypatch):
    """Default transport should preserve the historical stdio behavior."""
    monkeypatch.delenv("KICAD_MCP_TRANSPORT", raising=False)
    monkeypatch.delenv("KICAD_MCP_HOST", raising=False)
    monkeypatch.delenv("KICAD_MCP_PORT", raising=False)
    monkeypatch.delenv("KICAD_MCP_PATH", raising=False)

    assert get_transport_config() == {
        "transport": "stdio",
        "host": "127.0.0.1",
        "port": 8000,
        "path": "/mcp",
    }


def test_transport_config_supports_sse_for_chatgpt_desktop(monkeypatch):
    """SSE transport should be selectable for ChatGPT Desktop style local HTTP use."""
    monkeypatch.setenv("KICAD_MCP_TRANSPORT", "sse")
    monkeypatch.setenv("KICAD_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("KICAD_MCP_PORT", "8765")

    assert get_transport_config() == {
        "transport": "sse",
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/sse",
    }


def test_run_server_passes_sse_transport_arguments():
    """Run wrapper should pass transport arguments only when supported by FastMCP.run."""
    fake_server = FakeRunnableServer()

    server_module._run_server_with_config(
        fake_server,
        {"transport": "sse", "host": "127.0.0.1", "port": 8765, "path": "/sse"},
    )

    assert fake_server.run_kwargs == {
        "transport": "sse",
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/sse",
    }


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


def test_config_honors_environment_overrides(monkeypatch):
    """Config paths should respect KICAD_USER_DIR and KICAD_APP_PATH overrides."""
    original_user_dir = config.KICAD_USER_DIR
    original_app_path = config.KICAD_APP_PATH

    monkeypatch.setenv("KICAD_USER_DIR", "~/CustomKiCadProjects")
    monkeypatch.setenv("KICAD_APP_PATH", "~/Applications/KiCadCustom.app")

    reloaded = importlib.reload(config)

    assert str(Path("~/CustomKiCadProjects").expanduser()) == reloaded.KICAD_USER_DIR
    assert str(Path("~/Applications/KiCadCustom.app").expanduser()) == reloaded.KICAD_APP_PATH

    monkeypatch.setenv("KICAD_USER_DIR", original_user_dir)
    monkeypatch.setenv("KICAD_APP_PATH", original_app_path)
    importlib.reload(config)


def test_get_kicad_cli_path_can_return_none_when_not_required(monkeypatch):
    """Optional KiCad CLI lookups should return None instead of raising."""
    monkeypatch.setattr(
        "kicad_mcp.utils.kicad_cli.get_cli_manager",
        lambda: type("Manager", (), {"get_cli_path": staticmethod(lambda required=True: None)})(),
    )

    assert get_kicad_cli_path(required=False) is None


@pytest.mark.asyncio
async def test_find_component_connections_marks_results_as_inferred(monkeypatch):
    """Connection lookup should expose incomplete connectivity explicitly."""
    server = create_server()
    tools = await server.get_tools()
    fake_ctx = FakeContext()
    project_path = Path("/tmp/test-project.kicad_pro")
    project_path.write_text("{}")

    monkeypatch.setattr(
        "kicad_mcp.tools.netlist_tools.get_project_files",
        lambda path: {"schematic": "/tmp/demo.kicad_sch"},
    )
    monkeypatch.setattr(
        "kicad_mcp.tools.netlist_tools.extract_netlist",
        lambda path: {
            "components": {
                "R1": {"pins": [{"num": "1", "name": "A"}, {"num": "2", "name": "B"}]},
                "U1": {"pins": [{"num": "1", "name": "IN"}]},
            },
            "nets": {"NET1": [{"component": "R1", "pin": "1"}, {"component": "U1", "pin": "1"}]},
            "component_count": 2,
            "net_count": 1,
            "limitations": ["partial connectivity"],
            "netlist_quality": "partial",
        },
    )

    result = await tools["find_component_connections"].fn(str(project_path), "R1", fake_ctx)

    assert result["success"] is True
    assert result["connectivity_complete"] is False
    assert result["inferred_connection_count"] == 1
    assert fake_ctx.info_messages[-1].startswith("Inferred 1 possible connections")