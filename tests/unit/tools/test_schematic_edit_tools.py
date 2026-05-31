from pathlib import Path
import shutil
import subprocess

from fastmcp.utilities.types import Image
import pytest

from kicad_mcp.server import create_server

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "sample_schematic.kicad_sch"


def _copy_fixture(tmp_path: Path) -> Path:
    schematic_path = tmp_path / "tool_demo.kicad_sch"
    shutil.copy2(FIXTURE_PATH, schematic_path)
    return schematic_path


def _assert_svg_preview(preview: dict, path: Path) -> None:
    assert preview == {
        "kind": "svg",
        "path": str(path),
        "mime_type": "image/svg+xml",
        "file_size": path.stat().st_size,
    }


def _contains_image(value) -> bool:
    if isinstance(value, Image):
        return True
    if isinstance(value, dict):
        return any(_contains_image(item) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_contains_image(item) for item in value)
    return False


@pytest.mark.asyncio
async def test_export_schematic_svg_uses_secure_cli_runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    schematic_path = _copy_fixture(tmp_path)
    output_path = tmp_path / "preview.svg"
    server = create_server()
    tools = await server.get_tools()

    def fake_export(schematic_path: str, requested_output: str | None = None):
        assert requested_output == str(output_path)
        output_path.write_text("<svg/>", encoding="utf-8")
        return {
            "success": True,
            "schematic_path": schematic_path,
            "svg_path": str(output_path),
            "stdout": "",
            "stderr": "",
        }

    monkeypatch.setattr(
        "kicad_mcp.tools.export_tools.validate_schematic_with_cli_export",
        lambda path: {"success": True, "skipped": False, "stdout": "", "stderr": ""},
    )
    monkeypatch.setattr(
        "kicad_mcp.tools.export_tools.export_schematic_svg_file",
        fake_export,
    )

    result = await tools["export_schematic_svg"].fn(str(schematic_path), str(output_path), None)

    assert result["success"] is True
    assert result["svg_path"] == str(output_path)
    _assert_svg_preview(result["preview"], output_path)
    assert not _contains_image(result)


@pytest.mark.asyncio
async def test_export_schematic_svg_resolves_cli_directory_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    schematic_path = _copy_fixture(tmp_path)
    output_path = tmp_path / "preview.svg"
    server = create_server()
    tools = await server.get_tools()

    def fake_run_kicad_command(
        self,
        command_args,
        input_files=None,
        output_files=None,
        working_dir=None,
        timeout=None,
        capture_output=True,
    ):
        output_dir = Path(command_args[-1])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / f"{schematic_path.stem}.svg").write_text("<svg/>", encoding="utf-8")
        return subprocess.CompletedProcess(command_args, 0, "", "")

    monkeypatch.setattr(
        "kicad_mcp.tools.export_tools.validate_schematic_with_cli_export",
        lambda path: {"success": True, "skipped": False, "stdout": "", "stderr": ""},
    )
    monkeypatch.setattr(
        "kicad_mcp.utils.transactional_edit.SecureSubprocessRunner.run_kicad_command",
        fake_run_kicad_command,
    )

    result = await tools["export_schematic_svg"].fn(str(schematic_path), str(output_path), None)

    assert result["success"] is True
    assert result["svg_path"] == str(output_path)
    assert output_path.is_file()
    _assert_svg_preview(result["preview"], output_path)


@pytest.mark.asyncio
async def test_export_schematic_svg_reads_legacy_svg_named_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    schematic_path = _copy_fixture(tmp_path)
    legacy_output_dir = tmp_path / "legacy.svg"
    legacy_output_dir.mkdir()
    server = create_server()
    tools = await server.get_tools()

    def fake_run_kicad_command(
        self,
        command_args,
        input_files=None,
        output_files=None,
        working_dir=None,
        timeout=None,
        capture_output=True,
    ):
        output_dir = Path(command_args[-1])
        (output_dir / f"{schematic_path.stem}.svg").write_text("<svg/>", encoding="utf-8")
        return subprocess.CompletedProcess(command_args, 0, "", "")

    monkeypatch.setattr(
        "kicad_mcp.tools.export_tools.validate_schematic_with_cli_export",
        lambda path: {"success": True, "skipped": False, "stdout": "", "stderr": ""},
    )
    monkeypatch.setattr(
        "kicad_mcp.utils.transactional_edit.SecureSubprocessRunner.run_kicad_command",
        fake_run_kicad_command,
    )

    result = await tools["export_schematic_svg"].fn(
        str(schematic_path), str(legacy_output_dir), None
    )

    expected_svg = legacy_output_dir / f"{schematic_path.stem}.svg"
    assert result["success"] is True
    assert result["svg_path"] == str(expected_svg)
    assert expected_svg.is_file()
    _assert_svg_preview(result["preview"], expected_svg)
