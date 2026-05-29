from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_mcp.server import create_server
from kicad_mcp.tools import bom_tools


@pytest.mark.asyncio
async def test_export_bom_csv_does_not_retry_internal_parser_when_already_attempted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project_path = tmp_path / "demo.kicad_pro"
    schematic_path = tmp_path / "demo.kicad_sch"
    project_path.write_text("{}", encoding="utf-8")
    schematic_path.write_text("(kicad_sch)", encoding="utf-8")
    calls = {"internal": 0, "cli": 0}

    monkeypatch.setattr(
        bom_tools,
        "get_kicad_app_context",
        lambda ctx: SimpleNamespace(kicad_modules_available=True),
    )
    monkeypatch.setattr(
        bom_tools,
        "get_project_files",
        lambda project: {"schematic": str(schematic_path)},
    )

    async def fail_internal(*_args, **_kwargs):
        calls["internal"] += 1
        return {"success": False, "error": "internal failed"}

    async def fail_cli(*_args, **_kwargs):
        calls["cli"] += 1
        return {"success": False, "error": "cli failed"}

    monkeypatch.setattr(bom_tools, "export_bom_with_python", fail_internal)
    monkeypatch.setattr(bom_tools, "export_bom_with_cli", fail_cli)

    tools = await create_server().get_tools()
    result = await tools["export_bom_csv"].fn(str(project_path), None)

    assert result["success"] is False
    assert result["error"] == "cli failed"
    assert calls == {"internal": 1, "cli": 1}


@pytest.mark.asyncio
async def test_export_bom_csv_uses_internal_fallback_after_cli_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project_path = tmp_path / "demo.kicad_pro"
    schematic_path = tmp_path / "demo.kicad_sch"
    project_path.write_text("{}", encoding="utf-8")
    schematic_path.write_text("(kicad_sch)", encoding="utf-8")
    calls = {"internal": 0, "cli": 0}

    monkeypatch.setattr(
        bom_tools,
        "get_kicad_app_context",
        lambda ctx: SimpleNamespace(kicad_modules_available=False),
    )
    monkeypatch.setattr(
        bom_tools,
        "get_project_files",
        lambda project: {"schematic": str(schematic_path)},
    )

    async def succeed_internal(*_args, **_kwargs):
        calls["internal"] += 1
        return {"success": True, "method": "internal_schematic_parser"}

    async def fail_cli(*_args, **_kwargs):
        calls["cli"] += 1
        return {"success": False, "error": "cli failed"}

    monkeypatch.setattr(bom_tools, "export_bom_with_python", succeed_internal)
    monkeypatch.setattr(bom_tools, "export_bom_with_cli", fail_cli)

    tools = await create_server().get_tools()
    result = await tools["export_bom_csv"].fn(str(project_path), None)

    assert result["success"] is True
    assert result["method"] == "internal_schematic_parser"
    assert calls == {"internal": 1, "cli": 1}
