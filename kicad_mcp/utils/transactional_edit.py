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
from kicad_mcp.utils.kicad_s_expr import (
    KiCadSchematic,
    SExpressionError,
    compare_block_connectivity_snapshots,
    validate_schematic_text,
)
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

    # Include microseconds to avoid backup directory collisions during rapid successive edits.
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
        if os.path.isdir(output_path):
            shutil.rmtree(output_path, ignore_errors=True)
        else:
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


def export_schematic_svg_file(
    schematic_path: str, output_path: str | None = None
) -> dict[str, Any]:
    """Export a schematic SVG to disk."""
    validated_path = validate_local_path(schematic_path, "schematic", must_exist=True)
    schematic_dir = os.path.dirname(validated_path) or validated_path
    if output_path is None:
        output_path = os.path.join(schematic_dir, f"{Path(validated_path).stem}_schematic.svg")
    output_path = os.path.realpath(os.path.expanduser(output_path))

    output_dir_name = os.path.dirname(output_path)
    output_dir = output_dir_name if output_dir_name else schematic_dir
    validator = PathValidator(trusted_roots={schematic_dir, output_dir})
    runner = SecureSubprocessRunner(path_validator=validator)
    result = runner.run_kicad_command(
        ["sch", "export", "svg", validated_path, "-o", output_path],
        input_files=[validated_path],
        output_files=[output_path],
        working_dir=schematic_dir,
    )
    if result.returncode != 0:
        return {
            "success": False,
            "schematic_path": validated_path,
            "svg_path": output_path,
            "error": result.stderr or result.stdout or "KiCad CLI export failed",
        }
    return {
        "success": True,
        "schematic_path": validated_path,
        "svg_path": output_path,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def apply_transactional_schematic_edit(
    schematic_path: str,
    mutator: Callable[[KiCadSchematic], dict[str, Any]],
    *,
    run_cli_validation: bool = True,
    post_write_validator: Callable[[str], dict[str, Any]] | None = None,
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

        post_write_validation = (
            post_write_validator(validated_path)
            if post_write_validator is not None
            else {"success": True, "skipped": True, "reason": "Post-write validation disabled"}
        )
        if not post_write_validation.get("success", False):
            raise TransactionalEditError(
                cast(str, post_write_validation.get("error") or post_write_validation.get("reason"))
            )

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
                "post_write": post_write_validation,
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


def validate_block_connectivity_snapshots(
    schematic_path: str, before_snapshots: list[dict[str, Any]]
) -> dict[str, Any]:
    """Validate block connectivity snapshots after a write."""
    if not before_snapshots:
        return {
            "success": True,
            "skipped": True,
            "reason": "No block moves planned",
            "block_connectivity": "preserved",
        }
    schematic = KiCadSchematic.from_file(schematic_path)
    comparisons = []
    failures = []
    for before_snapshot in before_snapshots:
        after_snapshot = schematic.block_connectivity_snapshot(
            symbol_refs=before_snapshot["internal_symbols"]
        )
        comparison = compare_block_connectivity_snapshots(before_snapshot, after_snapshot)
        comparisons.append(
            {
                "block_id": before_snapshot["block_id"],
                "before": before_snapshot,
                "after": after_snapshot,
                "reason": comparison["reason"],
                "success": comparison["preserved"],
            }
        )
        if not comparison["preserved"]:
            failures.append(f"{before_snapshot['block_id']}: {comparison['reason']}")
    return {
        "success": not failures,
        "reason": "block connectivity preserved" if not failures else "; ".join(failures),
        "block_connectivity": "preserved" if not failures else "changed",
        "blocks": comparisons,
    }


def apply_transactional_schematic_cleanup(
    schematic_path: str,
    *,
    layout_style: str = "left_to_right",
    spacing_x: float = 35.0,
    spacing_y: float = 25.0,
    arrange_properties: bool = True,
    preserve_connectivity: bool = True,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Apply the high-level cleanup workflow transactionally."""
    validated_path = validate_local_path(schematic_path, "schematic", must_exist=True)
    original_text = Path(validated_path).read_text(encoding="utf-8")
    backup = create_file_backup(validated_path)
    try:
        before_validation = validate_schematic_text(original_text)
        schematic = KiCadSchematic.from_text(original_text)
        preview = schematic.preview_cleanup(
            layout_style=layout_style,
            spacing_x=spacing_x,
            spacing_y=spacing_y,
            arrange_properties=arrange_properties,
            preserve_connectivity=preserve_connectivity,
        )
        cleanup_plan = cast(dict[str, Any], preview["cleanup_plan"])
        if not preview["success"]:
            return {
                "success": False,
                "schematic_path": validated_path,
                "backup_path": backup["backup_path"],
                "error": preview.get("error", "Cleanup preview failed"),
                "cleanup_plan": cleanup_plan,
                "rolled_back": False,
            }

        before_snapshots = [
            schematic.block_connectivity_snapshot(symbol_refs=move["symbols"])
            for move in cast(list[dict[str, Any]], cleanup_plan["block_moves"])
        ]
        changed_objects = schematic.apply_cleanup(
            layout_style=layout_style,
            spacing_x=spacing_x,
            spacing_y=spacing_y,
            arrange_properties=arrange_properties,
            preserve_connectivity=preserve_connectivity,
        )
        updated_text = schematic.to_text()
        after_validation = validate_schematic_text(updated_text)
        Path(validated_path).write_text(updated_text, encoding="utf-8")

        cli_validation = validate_schematic_with_cli_export(validated_path)
        if not cli_validation["success"]:
            raise TransactionalEditError(cli_validation.get("stderr") or "CLI validation failed")

        connectivity_validation = validate_block_connectivity_snapshots(
            validated_path, before_snapshots
        )
        if not connectivity_validation["success"]:
            raise TransactionalEditError(cast(str, connectivity_validation["reason"]))

        export_result = export_schematic_svg_file(validated_path, output_path)
        if not export_result["success"]:
            raise TransactionalEditError(cast(str, export_result["error"]))

        diff_result = get_file_diff_against_backup(validated_path, backup["backup_path"])
        return {
            "success": True,
            "schematic_path": validated_path,
            "backup_path": backup["backup_path"],
            "cleanup_plan": cleanup_plan,
            "changed_objects": changed_objects,
            "validation": {
                "before": before_validation,
                "after": after_validation,
                "cli": cli_validation,
                "post_write": connectivity_validation,
                "syntax": "ok",
                "cli_export": "ok",
                "connectivity": "preserved",
            },
            "svg_preview": export_result["svg_path"],
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
