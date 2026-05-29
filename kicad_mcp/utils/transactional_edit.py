"""
Transactional editing helpers for safe KiCad schematic modifications.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import unified_diff
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, cast

from kicad_mcp.utils.file_utils import get_project_files
from kicad_mcp.utils.kicad_cli import get_kicad_cli_path
from kicad_mcp.utils.kicad_s_expr import (
    KiCadSchematic,
    SExpressionError,
    compare_block_connectivity_snapshots,
    validate_schematic_text,
)
from kicad_mcp.utils.path_validator import (
    PathValidationError,
    PathValidator,
    get_application_temp_root,
    get_configured_trusted_roots,
    get_configured_validator,
    validate_configured_directory,
    validate_configured_kicad_file,
)
from kicad_mcp.utils.secure_subprocess import SecureSubprocessRunner

BACKUP_DIR_NAME = ".kicad_mcp_backups"
BACKUP_METADATA_NAME = "backup_manifest.json"


class TransactionalEditError(RuntimeError):
    """Raised when a transactional schematic edit fails."""


@contextmanager
def transactional_file_lock(file_path: str):
    """Serialize transactional writers for one target file across processes.

    The lock marker lives in KiCad MCP's private temp area, not in the project
    directory. It is intentionally reused rather than unlinked on every unlock:
    removing lock files is racy when another process already has the old lock
    file open or is waiting on it.
    """
    lock_path = _transactional_lock_path(file_path)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            if os.path.getsize(lock_path) == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _transactional_lock_path(file_path: str | Path) -> Path:
    target_key = os.path.normcase(os.path.realpath(os.path.expanduser(str(file_path))))
    digest = hashlib.sha256(target_key.encode("utf-8", "surrogatepass")).hexdigest()
    lock_dir = Path(get_application_temp_root()) / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(lock_dir, 0o700)
    except OSError:
        pass
    return lock_dir / f"{digest}.lock"


def atomic_write_text(file_path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write text to a same-directory temp file and atomically replace the target."""
    target = Path(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        _fsync_directory(target.parent)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _atomic_copy2(source: str, destination: str) -> None:
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".restore.tmp",
        dir=str(target.parent),
    )
    os.close(fd)
    try:
        shutil.copy2(source, temp_name)
        os.replace(temp_name, destination)
        _fsync_directory(target.parent)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sanitize_message(message: Any) -> str:
    """Redact host-specific absolute paths from user-facing diagnostics."""
    text = "" if message is None else str(message)
    replacements = []
    for label, path in (
        ("<workspace>", os.getcwd()),
        ("<home>", str(Path.home())),
        ("<temp>", tempfile.gettempdir()),
    ):
        replacements.append((path, label))
    for root in get_configured_trusted_roots():
        replacements.append((root, "<trusted_root>"))

    sanitized = text
    for raw_path, label in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        if not raw_path:
            continue
        normalized = os.path.realpath(os.path.expanduser(raw_path))
        variants = {normalized, normalized.replace("\\", "/"), normalized.replace("/", "\\")}
        for variant in variants:
            sanitized = sanitized.replace(variant, label)
    home = str(Path.home())
    if home:
        sanitized = sanitized.replace(home.replace("\\", "/"), "~")
        sanitized = sanitized.replace(home.replace("/", "\\"), "~")
    return re.sub(r"\s+$", "", sanitized)


def validate_local_path(file_path: str, file_type: str, must_exist: bool = True) -> str:
    """Validate a KiCad file path against configured trusted filesystem roots."""
    expanded = os.path.realpath(os.path.expanduser(file_path))
    return validate_configured_kicad_file(expanded, file_type, must_exist=must_exist)


def validate_local_directory(dir_path: str, must_exist: bool = True) -> str:
    """Validate a directory path against configured trusted filesystem roots."""
    expanded = os.path.realpath(os.path.expanduser(dir_path))
    return validate_configured_directory(expanded, must_exist=must_exist)


def validate_schematic_file_safely(schematic_path: str) -> dict[str, Any]:
    """Validate schematic syntax and basic structure."""
    validated_path = validate_local_path(schematic_path, "schematic", must_exist=True)
    try:
        validation = validate_schematic_text(Path(validated_path).read_text(encoding="utf-8"))
    except (OSError, SExpressionError) as exc:
        return {"success": False, "schematic_path": validated_path, "error": _sanitize_message(str(exc))}
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
    configured_validator = get_configured_validator()

    # Include microseconds to avoid backup directory collisions during rapid successive edits.
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_dir = os.path.join(backup_root, BACKUP_DIR_NAME, f"{backup_type}_{timestamp}")
    os.makedirs(backup_dir, exist_ok=False)

    files = []
    for source_path in source_paths:
        source = configured_validator.validate_path(source_path, must_exist=True)
        if not os.path.isfile(source):
            raise PathValidationError(f"Backup source is not a file: {source_path}")
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
    atomic_write_text(manifest_path, json.dumps(manifest, indent=2))
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
    manifest_path = PathValidator(trusted_roots={validated_backup}).validate_path(
        os.path.join(validated_backup, BACKUP_METADATA_NAME),
        must_exist=True,
    )
    if not os.path.isfile(manifest_path):
        raise FileNotFoundError(f"Backup manifest not found: {manifest_path}")
    return cast(dict[str, Any], json.loads(Path(manifest_path).read_text(encoding="utf-8")))


def _validated_manifest_entries(manifest: dict[str, Any], validated_backup: str) -> list[tuple[str, str]]:
    """Return validated (backup_file, destination_file) entries from a manifest."""
    raw_entries = manifest.get("files")
    if not isinstance(raw_entries, list):
        raise PathValidationError("Backup manifest files must be a list")

    backup_validator = PathValidator(trusted_roots={validated_backup})
    source_root = _manifest_source_root(manifest, validated_backup)
    source_validator = PathValidator(trusted_roots={source_root})
    entries: list[tuple[str, str]] = []
    for index, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            raise PathValidationError(f"Backup manifest entry {index} must be an object")
        if not isinstance(entry.get("source"), str) or not isinstance(entry.get("backup"), str):
            raise PathValidationError(f"Backup manifest entry {index} has invalid paths")
        destination = source_validator.validate_path(entry["source"], must_exist=False)
        if os.path.exists(destination) and not os.path.isfile(destination):
            raise PathValidationError(f"Backup destination is not a file: {entry['source']}")
        source = backup_validator.validate_path(entry["backup"], must_exist=True)
        if not os.path.isfile(source):
            raise PathValidationError(f"Backup entry is not a file: {entry['backup']}")
        entries.append((source, destination))
    return entries


def _manifest_source_root(manifest: dict[str, Any], validated_backup: str) -> str:
    backup_dir = Path(validated_backup)
    if backup_dir.parent.name == BACKUP_DIR_NAME:
        return str(backup_dir.parent.parent)
    target_path = manifest.get("target_path")
    if isinstance(target_path, str) and target_path.strip():
        target = get_configured_validator().validate_path(target_path, must_exist=False)
        return os.path.dirname(target) or target
    raise PathValidationError("Backup manifest does not identify a trusted source root")


def restore_backup_manifest(backup_path: str) -> dict[str, Any]:
    """Restore files from a backup manifest."""
    try:
        validated_backup = validate_local_directory(backup_path, must_exist=True)
        manifest = load_backup_manifest(validated_backup)
        entries = _validated_manifest_entries(manifest, validated_backup)
    except (FileNotFoundError, json.JSONDecodeError, PathValidationError) as exc:
        return {"success": False, "backup_path": backup_path, "error": _sanitize_message(str(exc))}

    restored_files = []
    for source, destination in entries:
        _atomic_copy2(source, destination)
        restored_files.append(destination)

    return {
        "success": True,
        "backup_path": backup_path,
        "restored_files": restored_files,
        "type": manifest.get("type"),
    }


def get_backup_file_for_source(file_path: str, backup_path: str) -> str:
    """Resolve a backed-up file path for a specific source file."""
    validated_backup = validate_local_directory(backup_path, must_exist=True)
    manifest = load_backup_manifest(validated_backup)
    source_root = _manifest_source_root(manifest, validated_backup)
    source = PathValidator(trusted_roots={source_root}).validate_path(file_path, must_exist=True)
    for backup_file, entry_source in _validated_manifest_entries(manifest, validated_backup):
        if entry_source == source:
            return backup_file
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
            "stdout": _sanitize_message(result.stdout),
            "stderr": _sanitize_message(result.stderr),
        }

    return {
        "success": True,
        "skipped": False,
        "stdout": _sanitize_message(result.stdout),
        "stderr": _sanitize_message(result.stderr),
    }


def export_schematic_svg_file(
    schematic_path: str, output_path: str | None = None
) -> dict[str, Any]:
    """Export a schematic SVG to disk."""
    validated_path = validate_local_path(schematic_path, "schematic", must_exist=True)
    schematic_dir = os.path.dirname(validated_path) or validated_path
    schematic_stem = Path(validated_path).stem
    if output_path is None:
        requested_path = os.path.join(schematic_dir, f"{schematic_stem}_schematic.svg")
    else:
        requested_path = os.path.realpath(os.path.expanduser(output_path))

    requested = Path(requested_path)
    requested_is_svg_file = requested.suffix.lower() == ".svg"
    legacy_svg_directory = requested_is_svg_file and requested.is_dir()
    if legacy_svg_directory:
        final_svg_path = requested / f"{schematic_stem}.svg"
        output_dir = str(requested)
        temp_dir_context = None
    elif requested_is_svg_file:
        final_svg_path = requested
        output_dir = str(requested.parent if str(requested.parent) else Path(schematic_dir))
        os.makedirs(output_dir, exist_ok=True)
        temp_dir_context = tempfile.TemporaryDirectory(
            prefix=".kicad_mcp_svg_", dir=output_dir
        )
    else:
        output_dir = str(requested)
        os.makedirs(output_dir, exist_ok=True)
        final_svg_path = Path(output_dir) / f"{schematic_stem}.svg"
        temp_dir_context = None

    cli_output_dir = temp_dir_context.name if temp_dir_context is not None else output_dir
    generated_svg_path = Path(cli_output_dir) / f"{schematic_stem}.svg"
    validator = PathValidator(trusted_roots={schematic_dir, output_dir})
    runner = SecureSubprocessRunner(path_validator=validator)
    try:
        result = runner.run_kicad_command(
            ["sch", "export", "svg", validated_path, "-o", cli_output_dir],
            input_files=[validated_path],
            output_files=[str(generated_svg_path)],
            working_dir=schematic_dir,
        )
        if result.returncode != 0:
            return {
                "success": False,
                "schematic_path": validated_path,
                "svg_path": str(final_svg_path),
                "error": _sanitize_message(
                    result.stderr or result.stdout or "KiCad CLI export failed"
                ),
            }

        if not generated_svg_path.is_file():
            return {
                "success": False,
                "schematic_path": validated_path,
                "svg_path": str(final_svg_path),
                "error": _sanitize_message(
                    f"KiCad CLI did not create expected SVG: {generated_svg_path}"
                ),
            }

        if generated_svg_path != final_svg_path:
            os.makedirs(final_svg_path.parent, exist_ok=True)
            os.replace(generated_svg_path, final_svg_path)

        return {
            "success": True,
            "schematic_path": validated_path,
            "svg_path": str(final_svg_path),
            "stdout": _sanitize_message(result.stdout),
            "stderr": _sanitize_message(result.stderr),
        }
    finally:
        if temp_dir_context is not None:
            temp_dir_context.cleanup()


def apply_transactional_schematic_edit(
    schematic_path: str,
    mutator: Callable[[KiCadSchematic], dict[str, Any]],
    *,
    run_cli_validation: bool = True,
    post_write_validator: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply a schematic edit transactionally with backup and rollback."""
    validated_path = validate_local_path(schematic_path, "schematic", must_exist=True)
    with transactional_file_lock(validated_path):
        original_text = Path(validated_path).read_text(encoding="utf-8")
        backup = create_file_backup(validated_path)

        try:
            before_validation = validate_schematic_text(original_text)
            schematic = KiCadSchematic.from_text(original_text)
            change_result = mutator(schematic)
            updated_text = schematic.to_text()
            after_validation = validate_schematic_text(updated_text)
            atomic_write_text(validated_path, updated_text)
            _invalidate_schematic_validation_cache(validated_path)

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
            _invalidate_schematic_validation_cache(validated_path)
            return {
                "success": False,
                "schematic_path": validated_path,
                "backup_path": backup["backup_path"],
                "error": _sanitize_message(str(exc)),
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
    with transactional_file_lock(validated_path):
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
                    "error": _sanitize_message(preview.get("error", "Cleanup preview failed")),
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
            atomic_write_text(validated_path, updated_text)
            _invalidate_schematic_validation_cache(validated_path)

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
            _invalidate_schematic_validation_cache(validated_path)
            return {
                "success": False,
                "schematic_path": validated_path,
                "backup_path": backup["backup_path"],
                "error": _sanitize_message(str(exc)),
                "rolled_back": restore_result.get("success", False),
                "restore_result": restore_result,
            }


def _invalidate_schematic_validation_cache(schematic_path: str) -> None:
    try:
        from kicad_mcp.utils.kicad_cli_batch import invalidate_schematic_validation_cache

        invalidate_schematic_validation_cache(schematic_path)
    except Exception:
        pass
