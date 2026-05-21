"""
Transactional editing helpers for safe KiCad schematic modifications.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from difflib import unified_diff
import json
import os
from pathlib import Path
import shutil
from typing import Any, cast

from kicad_mcp.utils.file_utils import get_project_files
from kicad_mcp.utils.kicad_cli import get_kicad_cli_path
from kicad_mcp.utils.kicad_s_expr import KiCadSchematic, SExpressionError, validate_schematic_text
from kicad_mcp.utils.path_validator import PathValidationError, PathValidator
from kicad_mcp.utils.secure_subprocess import SecureSubprocessRunner

BACKUP_DIR_NAME = ".kicad_mcp_backups"
BACKUP_METADATA_NAME = "backup_manifest.json"


class TransactionalEditError(RuntimeError):
    """Raised when a transactional schematic edit fails."""


def validate_local_path(file_path: str, file_type: str, must_exist: bool = True) -> str:
    """Validate a KiCad file path using the file's own directory as the trusted root."""
    expanded = os.path.realpath(os.path.expanduser(file_path))
    trusted_root = expanded if os.path.isdir(expanded) else os.path.dirname(expanded)
    validator = PathValidator(trusted_roots={trusted_root})
    return validator.validate_kicad_file(expanded, file_type, must_exist=must_exist)


def validate_local_directory(dir_path: str, must_exist: bool = True) -> str:
    """Validate a directory path using itself as a trusted root."""
    expanded = os.path.realpath(os.path.expanduser(dir_path))
    validator = PathValidator(trusted_roots={expanded})
    return validator.validate_directory(expanded, must_exist=must_exist)


def validate_schematic_file_safely(schematic_path: str) -> dict[str, Any]:
    """Validate schematic syntax and basic structure."""
    validated_path = validate_local_path(schematic_path, "schematic", must_exist=True)
    try:
        validation = validate_schematic_text(Path(validated_path).read_text(encoding="utf-8"))
    except (OSError, SExpressionError) as exc:
        return {"success": False, "schematic_path": validated_path, "error": str(exc)}
    validation["success"] = True
    validation["schematic_path"] = validated_path
    return validation


def create_backup_manifest(
    source_paths: list[str],
    backup_root: str,
    backup_type: str,
    target_path: str,
) -> dict[str, Any]:
    """Create a timestamped backup directory with a manifest."""
    validate_local_directory(backup_root, must_exist=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_dir = os.path.join(backup_root, BACKUP_DIR_NAME, f"{backup_type}_{timestamp}")
    os.makedirs(backup_dir, exist_ok=False)

    files = []
    for source_path in source_paths:
        source = os.path.realpath(os.path.expanduser(source_path))
        backup_name = os.path.basename(source)
        destination = os.path.join(backup_dir, backup_name)
        shutil.copy2(source, destination)
        files.append({"source": source, "backup": destination})

    manifest = {
        "type": backup_type,
        "target_path": os.path.realpath(os.path.expanduser(target_path)),
        "created_at": timestamp,
        "files": files,
    }
    manifest_path = os.path.join(backup_dir, BACKUP_METADATA_NAME)
    Path(manifest_path).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest["backup_path"] = backup_dir
    return manifest


def create_file_backup(file_path: str) -> dict[str, Any]:
    """Create a backup for a single KiCad file."""
    source = os.path.realpath(os.path.expanduser(file_path))
    backup_root = os.path.dirname(source) or source
    return create_backup_manifest([source], backup_root, "file", source)


def backup_project_files(project_path: str) -> dict[str, Any]:
    """Create a project backup containing the project and related KiCad files."""
    validated_project = validate_local_path(project_path, "project", must_exist=True)
    files = get_project_files(validated_project)
    source_paths = sorted(set(files.values()))
    manifest = create_backup_manifest(
        source_paths=source_paths,
        backup_root=os.path.dirname(validated_project),
        backup_type="project",
        target_path=validated_project,
    )
    return {
        "success": True,
        "project_path": validated_project,
        "backup_path": manifest["backup_path"],
        "files": [entry["source"] for entry in manifest["files"]],
    }


def load_backup_manifest(backup_path: str) -> dict[str, Any]:
    """Load a backup manifest from a backup directory."""
    validated_backup = validate_local_directory(backup_path, must_exist=True)
    manifest_path = os.path.join(validated_backup, BACKUP_METADATA_NAME)
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(f"Backup manifest not found: {manifest_path}")
    return cast(dict[str, Any], json.loads(Path(manifest_path).read_text(encoding="utf-8")))


def restore_backup_manifest(backup_path: str) -> dict[str, Any]:
    """Restore files from a backup manifest."""
    try:
        manifest = load_backup_manifest(backup_path)
    except (FileNotFoundError, json.JSONDecodeError, PathValidationError) as exc:
        return {"success": False, "backup_path": backup_path, "error": str(exc)}

    restored_files = []
    for entry in manifest["files"]:
        destination = entry["source"]
        source = entry["backup"]
        shutil.copy2(source, destination)
        restored_files.append(destination)

    return {
        "success": True,
        "backup_path": backup_path,
        "restored_files": restored_files,
        "type": manifest.get("type"),
    }


def get_backup_file_for_source(file_path: str, backup_path: str) -> str:
    """Resolve a backed-up file path for a specific source file."""
    manifest = load_backup_manifest(backup_path)
    source = os.path.realpath(os.path.expanduser(file_path))
    for entry in manifest["files"]:
        if entry["source"] == source:
            return cast(str, entry["backup"])
    raise FileNotFoundError(f"No backup entry for file: {file_path}")


def get_file_diff_against_backup(file_path: str, backup_path: str) -> dict[str, Any]:
    """Return a unified diff between a current file and its backup."""
    source = os.path.realpath(os.path.expanduser(file_path))
    backup_file = get_backup_file_for_source(source, backup_path)
    current_lines = Path(source).read_text(encoding="utf-8").splitlines(keepends=True)
    backup_lines = Path(backup_file).read_text(encoding="utf-8").splitlines(keepends=True)
    diff = "".join(
        unified_diff(
            backup_lines,
            current_lines,
            fromfile=backup_file,
            tofile=source,
        )
    )
    return {
        "success": True,
        "file_path": source,
        "backup_path": backup_path,
        "diff": diff,
    }


def validate_schematic_with_cli_export(schematic_path: str) -> dict[str, Any]:
    """Validate a schematic by attempting a CLI SVG export when KiCad CLI is available."""
    cli_path = get_kicad_cli_path(required=False)
    if cli_path is None:
        return {
            "success": True,
            "skipped": True,
            "reason": "KiCad CLI is not available",
        }

    validated_path = validate_local_path(schematic_path, "schematic", must_exist=True)
    schematic_dir = os.path.dirname(validated_path) or validated_path
    output_path = os.path.join(schematic_dir, f".{Path(validated_path).stem}_validation.svg")
    validator = PathValidator(trusted_roots={schematic_dir})
    runner = SecureSubprocessRunner(path_validator=validator)

    result = runner.run_kicad_command(
        ["sch", "export", "svg", validated_path, "-o", output_path],
        input_files=[validated_path],
        output_files=[output_path],
        working_dir=schematic_dir,
    )
    if os.path.exists(output_path):
        os.unlink(output_path)

    if result.returncode != 0:
        return {
            "success": False,
            "skipped": False,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    return {
        "success": True,
        "skipped": False,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def apply_transactional_schematic_edit(
    schematic_path: str,
    mutator: Callable[[KiCadSchematic], dict[str, Any]],
    *,
    run_cli_validation: bool = True,
) -> dict[str, Any]:
    """Apply a schematic edit transactionally with backup and rollback."""
    validated_path = validate_local_path(schematic_path, "schematic", must_exist=True)
    original_text = Path(validated_path).read_text(encoding="utf-8")
    backup = create_file_backup(validated_path)

    try:
        before_validation = validate_schematic_text(original_text)
        schematic = KiCadSchematic.from_text(original_text)
        change_result = mutator(schematic)
        updated_text = schematic.to_text()
        after_validation = validate_schematic_text(updated_text)
        Path(validated_path).write_text(updated_text, encoding="utf-8")

        cli_validation = (
            validate_schematic_with_cli_export(validated_path)
            if run_cli_validation
            else {"success": True, "skipped": True, "reason": "CLI validation disabled"}
        )
        if not cli_validation["success"]:
            raise TransactionalEditError(cli_validation.get("stderr") or "CLI validation failed")

        diff_result = get_file_diff_against_backup(validated_path, backup["backup_path"])
        return {
            "success": True,
            "schematic_path": validated_path,
            "backup_path": backup["backup_path"],
            "changed_objects": change_result,
            "validation": {
                "before": before_validation,
                "after": after_validation,
                "cli": cli_validation,
            },
            "rolled_back": False,
            "diff": diff_result["diff"],
        }
    except Exception as exc:
        restore_result = restore_backup_manifest(backup["backup_path"])
        return {
            "success": False,
            "schematic_path": validated_path,
            "backup_path": backup["backup_path"],
            "error": str(exc),
            "rolled_back": restore_result.get("success", False),
            "restore_result": restore_result,
        }
