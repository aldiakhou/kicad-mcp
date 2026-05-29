"""
Tests for MCP startup and stdout-safe behavior.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import runpy
import subprocess

import pytest

import kicad_mcp.config as config
import kicad_mcp.server as server_module
from kicad_mcp.server import (
    ADVANCED_PROFILE_TOOLS,
    AGENT_PROFILE_TOOLS,
    DEBUG_PROFILE_TOOLS,
    create_server,
    get_tool_profile,
    get_transport_config,
)
from kicad_mcp.tools.drc_impl.cli_drc import run_drc_via_cli
from kicad_mcp.utils.kicad_api_detection import check_for_cli_api
from kicad_mcp.utils.kicad_cli import KiCadCLIError, get_kicad_cli_path
import kicad_mcp.utils.kicad_utils as kicad_utils
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

    def run(
        self,
        *,
        transport: str = "stdio",
        host: str | None = None,
        port: int | None = None,
        path: str | None = None,
    ) -> None:
        self.run_kwargs = {"transport": transport, "host": host, "port": port, "path": path}


class FakeKwargsRunnableServer:
    """FastMCP-like server double that accepts transport kwargs via **kwargs."""

    def __init__(self) -> None:
        self.run_kwargs: dict[str, object] | None = None

    def run(self, *, transport: str = "stdio", **transport_kwargs: object) -> None:
        self.run_kwargs = {"transport": transport, **transport_kwargs}


@pytest.mark.asyncio
async def test_create_server_registers_smoke_resources_and_tools():
    """The default server should expose only the agent-first intent tool surface."""
    server = create_server()

    tools = await server.get_tools()
    resource_templates = await server.get_resource_templates()

    assert set(tools) == AGENT_PROFILE_TOOLS
    assert "create_kicad_project" in tools
    assert "discover_projects" in tools
    assert "get_project_structure" in tools
    assert "schematic_apply_design_intent" in tools
    assert "schematic_preview_design_intent" in tools
    assert "schematic_apply_expanded_spec" in tools
    assert "schematic_start_design_intent_job" in tools
    assert "schematic_get_job_status" in tools
    assert "schematic_get_job_result" in tools
    assert "schematic_cancel_job" in tools
    assert "schematic_add_support_circuits" in tools
    assert "schematic_apply_no_connect_rules" in tools
    assert "schematic_build_from_spec_v2" in tools
    assert "export_schematic_preview" in tools
    assert "export_schematic_svg" in tools
    assert "schematic_apply_functional_layout" in tools
    assert "schematic_delete_item" in tools
    assert "schematic_snap_to_grid" in tools
    assert "schematic_connect_pin_to_net" in tools
    assert "schematic_connect_pins" in tools
    assert "schematic_connect_pin_to_ground" in tools
    assert "schematic_connect_pin_to_power" in tools
    assert "validate_project_boundaries" in tools
    assert "generate_validation_report" in tools
    assert "run_erc_check" in tools
    assert "resolve_symbol" in tools
    assert "resolve_footprint" in tools
    assert "schematic_add_wire" not in tools
    assert "schematic_get_pin_map" not in tools
    assert "schematic_build_from_spec" not in tools
    assert "pcb_add_track" not in tools
    assert "kicad://netlist/{schematic_path}" in resource_templates
    assert "kicad://drc/{project_path}" in resource_templates
    assert server_module._server_instance is server

    server_module.shutdown_server()
    assert server_module._server_instance is None


@pytest.mark.asyncio
async def test_agent_profile_tools_have_callable_handlers():
    """Server registration should expose executable tool handlers, not routing metadata."""
    server = create_server()
    tools = await server.get_tools()

    missing_callables = [name for name, tool in tools.items() if not callable(getattr(tool, "fn", None))]

    assert missing_callables == []

    server_module.shutdown_server()


@pytest.mark.asyncio
async def test_create_server_advanced_profile_exposes_manual_schematic_tools(monkeypatch):
    """Advanced profile adds manual schematic tools without raw geometry/debug aliases."""
    monkeypatch.setenv("KICAD_MCP_TOOL_PROFILE", "advanced")
    server = create_server()
    tools = await server.get_tools()

    assert get_tool_profile() == "advanced"
    assert set(tools) == AGENT_PROFILE_TOOLS | ADVANCED_PROFILE_TOOLS
    assert "schematic_add_symbol" in tools
    assert "schematic_snap_to_grid" in tools
    assert "list_symbol_libraries" in tools
    assert "schematic_add_wire" not in tools
    assert "schematic_get_pin_map" not in tools
    assert "schematic_build_from_spec" not in tools
    assert "pcb_add_track" not in tools

    server_module.shutdown_server()


@pytest.mark.asyncio
async def test_create_server_debug_profile_exposes_raw_schematic_tools(monkeypatch):
    """Debug profile adds raw schematic geometry and compatibility tools."""
    monkeypatch.setenv("KICAD_MCP_TOOL_PROFILE", "debug")
    server = create_server()
    tools = await server.get_tools()

    assert get_tool_profile() == "debug"
    assert set(tools) == AGENT_PROFILE_TOOLS | ADVANCED_PROFILE_TOOLS | DEBUG_PROFILE_TOOLS
    assert "schematic_add_wire" in tools
    assert "schematic_get_pin_map" in tools
    assert "schematic_build_from_spec" in tools
    assert "list_symbol_libraries" in tools
    assert "pcb_add_track" not in tools

    server_module.shutdown_server()


@pytest.mark.asyncio
async def test_create_server_all_profile_exposes_full_legacy_surface(monkeypatch):
    """All profile keeps every registered tool available for broad regression coverage."""
    monkeypatch.setenv("KICAD_MCP_TOOL_PROFILE", "all")
    server = create_server()
    tools = await server.get_tools()

    assert get_tool_profile() == "all"
    assert "schematic_add_wire" in tools
    assert "schematic_get_pin_map" in tools
    assert "schematic_build_from_spec" in tools
    assert "list_symbol_libraries" in tools
    assert "pcb_add_track" in tools
    assert "extract_schematic_netlist" in tools

    server_module.shutdown_server()


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


def test_transport_config_supports_sse_http_endpoint(monkeypatch):
    """SSE transport should be selectable for tunneled or remote HTTP use."""
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


def test_run_server_passes_sse_transport_arguments_via_var_kwargs():
    """Run wrapper should forward SSE options when FastMCP.run uses **transport_kwargs."""
    fake_server = FakeKwargsRunnableServer()

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


def test_extract_netlist_ignores_embedded_library_symbols(tmp_path: Path):
    schematic_path = tmp_path / "embedded.kicad_sch"
    schematic_path.write_text(
        """
(kicad_sch
  (version 20231120)
  (generator "pytest")
  (lib_symbols
    (symbol "Device:R"
      (property "Reference" "R" (at 0 0 0))
      (property "Value" "R" (at 0 2.54 0))
      (symbol "R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27) (number "1"))
      )
    )
  )
  (symbol
    (lib_id "Device:R")
    (at 10 10 0)
    (uuid 11111111-1111-1111-1111-111111111111)
    (property "Reference" "R1" (at 10 8 0))
    (property "Value" "10k" (at 10 12 0))
  )
  (symbol
    (lib_id "Device:R")
    (at 20 10 0)
    (uuid 22222222-2222-2222-2222-222222222222)
    (property "Reference" "R2" (at 20 8 0))
    (property "Value" "1k" (at 20 12 0))
  )
)
""",
        encoding="utf-8",
    )

    result = extract_netlist(str(schematic_path))

    assert result["component_count"] == 2
    assert set(result["components"]) == {"R1", "R2"}


@pytest.mark.asyncio
async def test_extract_schematic_netlist_reports_missing_file_via_async_ctx(monkeypatch):
    """Async tool paths should await ctx.info instead of leaking un-awaited coroutines."""
    monkeypatch.setenv("KICAD_MCP_TOOL_PROFILE", "all")
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
        "kicad_mcp.utils.secure_subprocess.get_kicad_cli_path",
        lambda required=True: (_ for _ in ()).throw(KiCadCLIError("KiCad CLI not found for tests")),
    )

    result = await run_drc_via_cli("/tmp/test.kicad_pcb", FakeContext())
    captured = capsys.readouterr()

    assert captured.out == ""
    assert result["success"] is False
    assert result["error"] == "KiCad CLI not found for tests"


@pytest.mark.asyncio
async def test_run_drc_via_cli_uses_explicit_timeout(monkeypatch, tmp_path: Path):
    captured: dict[str, float] = {}
    board = tmp_path / "board.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")

    def fake_run(cmd, capture_output=True, text=True, timeout=None, **kwargs):
        captured["timeout"] = timeout
        output_path = Path(cmd[cmd.index("--output") + 1])
        output_path.write_text(json.dumps({"violations": []}), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("kicad_mcp.utils.secure_subprocess.get_kicad_cli_path", lambda required=True: "kicad-cli")
    monkeypatch.setattr("kicad_mcp.utils.secure_subprocess.subprocess.run", fake_run)

    result = await run_drc_via_cli(str(board), None, timeout_seconds=12.5)

    assert result["success"] is True
    assert result["timeout_seconds"] == 12.5
    assert captured["timeout"] == 12.5


@pytest.mark.asyncio
async def test_run_drc_via_cli_uses_env_timeout(monkeypatch, tmp_path: Path):
    captured: dict[str, float] = {}
    board = tmp_path / "board.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")

    def fake_run(cmd, capture_output=True, text=True, timeout=None, **kwargs):
        captured["timeout"] = timeout
        output_path = Path(cmd[cmd.index("--output") + 1])
        output_path.write_text(json.dumps({"violations": []}), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setenv("KICAD_DRC_TIMEOUT", "88")
    monkeypatch.setattr("kicad_mcp.utils.secure_subprocess.get_kicad_cli_path", lambda required=True: "kicad-cli")
    monkeypatch.setattr("kicad_mcp.utils.secure_subprocess.subprocess.run", fake_run)

    result = await run_drc_via_cli(str(board), None)

    assert result["success"] is True
    assert result["timeout_seconds"] == 88
    assert captured["timeout"] == 88


@pytest.mark.asyncio
async def test_run_drc_via_cli_reports_timeout(monkeypatch, tmp_path: Path):
    board = tmp_path / "board.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")

    def fake_run(cmd, capture_output=True, text=True, timeout=None, **kwargs):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr("kicad_mcp.utils.secure_subprocess.get_kicad_cli_path", lambda required=True: "kicad-cli")
    monkeypatch.setattr("kicad_mcp.utils.secure_subprocess.subprocess.run", fake_run)

    result = await run_drc_via_cli(str(board), None, timeout_seconds=1)

    assert result["success"] is False
    assert result["method"] == "cli"
    assert result["timeout_seconds"] == 1
    assert "KICAD_DRC_TIMEOUT" in result["error"]


def test_check_for_cli_api_uses_shared_cli_detector(monkeypatch):
    """CLI detection should delegate to the shared KiCad CLI utility."""
    monkeypatch.setattr("kicad_mcp.utils.kicad_api_detection.is_kicad_cli_available", lambda: True)
    assert check_for_cli_api() is True

    monkeypatch.setattr("kicad_mcp.utils.kicad_api_detection.is_kicad_cli_available", lambda: False)
    assert check_for_cli_api() is False


def test_open_kicad_project_supports_windows_executable(monkeypatch, tmp_path: Path):
    project_path = tmp_path / "demo.kicad_pro"
    project_path.write_text("{}", encoding="utf-8")
    kicad_dir = tmp_path / "KiCad"
    exe_path = kicad_dir / "bin" / "kicad.exe"
    exe_path.parent.mkdir(parents=True)
    exe_path.write_text("", encoding="utf-8")
    launched: dict[str, list[str]] = {}

    monkeypatch.setattr(kicad_utils.sys, "platform", "win32")
    monkeypatch.setattr(kicad_utils.config, "KICAD_APP_PATH", str(kicad_dir))
    monkeypatch.setattr(
        kicad_utils.subprocess,
        "Popen",
        lambda cmd: launched.setdefault("cmd", cmd),
    )

    result = kicad_utils.open_kicad_project(str(project_path))

    assert result["success"] is True
    assert result["method"] == "kicad_executable"
    assert launched["cmd"] == [str(exe_path), str(project_path)]


def test_open_kicad_project_windows_startfile_fallback(monkeypatch, tmp_path: Path):
    project_path = tmp_path / "demo.kicad_pro"
    project_path.write_text("{}", encoding="utf-8")
    opened: dict[str, str] = {}

    monkeypatch.setattr(kicad_utils.sys, "platform", "win32")
    monkeypatch.setattr(kicad_utils.config, "KICAD_APP_PATH", str(tmp_path / "missing"))
    monkeypatch.setattr(kicad_utils.os, "startfile", lambda path: opened.setdefault("path", path), raising=False)

    result = kicad_utils.open_kicad_project(str(project_path))

    assert result["success"] is True
    assert result["method"] == "windows_file_association"
    assert opened["path"] == str(project_path)


def test_config_honors_environment_overrides(monkeypatch):
    """Config paths should respect KICAD_USER_DIR and KICAD_APP_PATH overrides."""
    original_user_dir = config.KICAD_USER_DIR
    original_app_path = config.KICAD_APP_PATH

    monkeypatch.setenv("KICAD_USER_DIR", "~/CustomKiCadProjects")
    monkeypatch.setenv("KICAD_APP_PATH", "~/Applications/KiCadCustom.app")

    reloaded = importlib.reload(config)

    assert os.path.normpath(str(Path("~/CustomKiCadProjects").expanduser())) == os.path.normpath(
        reloaded.KICAD_USER_DIR
    )
    assert os.path.normpath(
        str(Path("~/Applications/KiCadCustom.app").expanduser())
    ) == os.path.normpath(reloaded.KICAD_APP_PATH)

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
    monkeypatch.setenv("KICAD_MCP_TOOL_PROFILE", "all")
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
