import json
from pathlib import Path
import shutil

import pytest

from kicad_mcp.utils.kicad_s_expr import KiCadSchematic
from kicad_mcp.utils.path_validator import PathValidationError
from kicad_mcp.utils.transactional_edit import (
    apply_transactional_schematic_edit,
    backup_project_files,
    get_file_diff_against_backup,
    restore_backup_manifest,
    transactional_file_lock,
    validate_local_path,
    validate_schematic_file_safely,
)

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "sample_schematic.kicad_sch"


def _copy_fixture(tmp_path: Path) -> Path:
    destination = tmp_path / "demo.kicad_sch"
    shutil.copy2(FIXTURE_PATH, destination)
    return destination


def test_validate_schematic_file_safely_accepts_fixture(tmp_path: Path):
    schematic_path = _copy_fixture(tmp_path)

    result = validate_schematic_file_safely(str(schematic_path))

    assert result["success"] is True
    assert result["symbol_count"] == 2


def test_apply_transactional_schematic_edit_creates_backup_and_diff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    schematic_path = _copy_fixture(tmp_path)
    monkeypatch.setattr(
        "kicad_mcp.utils.transactional_edit.validate_schematic_with_cli_export",
        lambda path: {"success": True, "skipped": True, "reason": "test"},
    )

    result = apply_transactional_schematic_edit(
        str(schematic_path),
        lambda schematic: {"symbol": schematic.move_symbol("R1", 130.0, 140.0)},
    )

    assert result["success"] is True
    assert result["backup_path"]
    moved_schematic = KiCadSchematic.from_file(str(schematic_path))
    moved_symbol = moved_schematic.get_symbol("R1")
    assert moved_symbol is not None
    assert moved_symbol["position"]["x"] == 130.0
    assert result["diff"]

    diff_result = get_file_diff_against_backup(str(schematic_path), result["backup_path"])
    assert diff_result["success"] is True
    assert "@@" in diff_result["diff"]


def test_transactional_file_lock_uses_private_temp_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import kicad_mcp.utils.path_validator as path_validator

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    schematic_path = project_dir / "demo.kicad_sch"
    schematic_path.write_text("(kicad_sch)", encoding="utf-8")
    monkeypatch.setattr(path_validator.tempfile, "gettempdir", lambda: str(tmp_path / "system_tmp"))

    with transactional_file_lock(str(schematic_path)):
        assert not (project_dir / ".demo.kicad_sch.lock").exists()

    private_lock_dir = Path(path_validator.get_application_temp_root()) / "locks"
    assert list(private_lock_dir.glob("*.lock"))
    assert not (project_dir / ".demo.kicad_sch.lock").exists()


def test_apply_transactional_schematic_edit_rolls_back_on_failed_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    schematic_path = _copy_fixture(tmp_path)
    original_text = schematic_path.read_text(encoding="utf-8")
    monkeypatch.setattr(
        "kicad_mcp.utils.transactional_edit.validate_schematic_with_cli_export",
        lambda path: {"success": False, "stderr": "forced failure"},
    )

    result = apply_transactional_schematic_edit(
        str(schematic_path),
        lambda schematic: {"symbol": schematic.move_symbol("R1", 160.0, 170.0)},
    )

    assert result["success"] is False
    assert result["rolled_back"] is True
    assert schematic_path.read_text(encoding="utf-8") == original_text


def test_apply_transactional_schematic_edit_rolls_back_on_failed_post_write_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    schematic_path = _copy_fixture(tmp_path)
    original_text = schematic_path.read_text(encoding="utf-8")
    monkeypatch.setattr(
        "kicad_mcp.utils.transactional_edit.validate_schematic_with_cli_export",
        lambda path: {"success": True, "skipped": True, "reason": "test"},
    )

    result = apply_transactional_schematic_edit(
        str(schematic_path),
        lambda schematic: {"symbol": schematic.move_symbol("R1", 160.0, 170.0)},
        post_write_validator=lambda path: {"success": False, "reason": "post-write failure"},
    )

    assert result["success"] is False
    assert result["rolled_back"] is True
    assert "post-write failure" in result["error"]
    assert schematic_path.read_text(encoding="utf-8") == original_text


def test_backup_project_files_and_restore_backup_manifest(tmp_path: Path):
    project_path = tmp_path / "demo.kicad_pro"
    schematic_path = tmp_path / "demo.kicad_sch"
    project_path.write_text("{}", encoding="utf-8")
    shutil.copy2(FIXTURE_PATH, schematic_path)

    backup_result = backup_project_files(str(project_path))
    assert backup_result["success"] is True

    schematic_path.write_text("(kicad_sch (version 20231120) (generator \"changed\"))", encoding="utf-8")
    restore_result = restore_backup_manifest(backup_result["backup_path"])

    assert restore_result["success"] is True
    restored = schematic_path.read_text(encoding="utf-8")
    assert "kicad_mcp_test" in restored


def test_restore_backup_manifest_rejects_tampered_paths(tmp_path: Path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    project_path = project_dir / "demo.kicad_pro"
    schematic_path = project_dir / "demo.kicad_sch"
    victim_path = outside_dir / "victim.kicad_sch"
    project_path.write_text("{}", encoding="utf-8")
    shutil.copy2(FIXTURE_PATH, schematic_path)
    victim_path.write_text("do not overwrite", encoding="utf-8")

    backup_result = backup_project_files(str(project_path))
    manifest_path = Path(backup_result["backup_path"]) / "backup_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["source"] = str(victim_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    restore_result = restore_backup_manifest(backup_result["backup_path"])

    assert restore_result["success"] is False
    assert "outside trusted directories" in restore_result["error"]
    assert victim_path.read_text(encoding="utf-8") == "do not overwrite"


def test_validate_local_path_rejects_file_outside_configured_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_schematic = outside / "demo.kicad_sch"
    outside_schematic.write_text("(kicad_sch)", encoding="utf-8")

    import kicad_mcp.utils.path_validator as path_validator

    monkeypatch.setattr(path_validator.os, "getcwd", lambda: str(trusted))
    monkeypatch.setattr(path_validator.tempfile, "gettempdir", lambda: str(trusted / "tmp"))
    monkeypatch.setattr(path_validator.config, "KICAD_USER_DIR", str(trusted / "user"))
    monkeypatch.setattr(path_validator.config, "ADDITIONAL_SEARCH_PATHS", [])
    monkeypatch.delenv(path_validator.TRUSTED_ROOTS_ENV_VAR, raising=False)

    with pytest.raises(PathValidationError, match="outside trusted directories"):
        validate_local_path(str(outside_schematic), "schematic", must_exist=True)

    monkeypatch.setenv(path_validator.TRUSTED_ROOTS_ENV_VAR, str(outside))
    assert validate_local_path(str(outside_schematic), "schematic", must_exist=True) == str(
        outside_schematic.resolve()
    )
