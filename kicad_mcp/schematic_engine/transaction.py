"""Transaction model for atomic schematic generation.

All generation happens in a temporary worktree. The live project is not
modified until verification passes. Any exception rolls back.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any

logger = logging.getLogger(__name__)


class SchematicBuildTransaction:
    """Atomic transaction for schematic generation.

    Usage:
        with SchematicBuildTransaction(project_path) as tx:
            temp_project = tx.create_worktree()
            # ... generate schematic in temp_project ...
            if success:
                tx.commit()
            else:
                tx.rollback()

    Rules:
    - All generation happens in a temp worktree.
    - Live project is not modified until verification passes.
    - Commit is atomic: copy generated files using atomic writes.
    - Any exception rolls back.
    - Failed job returns changed=False.
    """

    def __init__(self, project_path: str, job_id: str | None = None):
        """Initialize transaction.

        Args:
            project_path: Path to the live KiCad project file (.kicad_pro).
            job_id: Optional job ID for tracking.
        """
        self.project_path = os.path.abspath(project_path)
        self.project_dir = os.path.dirname(self.project_path)
        self.project_name = Path(self.project_path).stem
        self.job_id = job_id

        self._temp_dir: str | None = None
        self._worktree_path: str | None = None
        self._committed = False
        self._rolled_back = False
        self._started_at = time.monotonic()

    def __enter__(self) -> SchematicBuildTransaction:
        """Enter transaction context."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit transaction context, rolling back on exception."""
        if exc_type is not None and not self._committed:
            logger.warning(
                "Transaction rolling back due to exception: %s: %s",
                exc_type.__name__,
                exc_val,
            )
            self.rollback()
        self.cleanup()

    @property
    def worktree_path(self) -> str | None:
        """Path to the temporary worktree project directory."""
        return self._worktree_path

    @property
    def worktree_project_path(self) -> str | None:
        """Path to the temporary worktree .kicad_pro file."""
        if self._worktree_path:
            return os.path.join(self._worktree_path, f"{self.project_name}.kicad_pro")
        return None

    @property
    def is_committed(self) -> bool:
        """Whether the transaction was committed."""
        return self._committed

    @property
    def is_rolled_back(self) -> bool:
        """Whether the transaction was rolled back."""
        return self._rolled_back

    def create_worktree(self) -> str:
        """Create a temporary project copy for generation.

        Returns:
            Path to the temp project directory.
        """
        self._temp_dir = tempfile.mkdtemp(prefix=".tmp_kicad_mcp_build_")
        self._worktree_path = self._temp_dir

        # Copy existing project files to temp directory
        if os.path.isdir(self.project_dir):
            for item in os.listdir(self.project_dir):
                # Only copy KiCad project files, not temp/backup dirs
                if item.startswith((".tmp_", ".kicad_mcp")):
                    continue
                src = os.path.join(self.project_dir, item)
                dst = os.path.join(self._worktree_path, item)
                if os.path.isfile(src):
                    shutil.copy2(src, dst)
                elif os.path.isdir(src) and not item.startswith("."):
                    shutil.copytree(src, dst, dirs_exist_ok=True)

        logger.debug("Created worktree at %s", self._worktree_path)
        return self._worktree_path

    def commit(self) -> dict[str, Any]:
        """Commit generated schematic files to the live project.

        Copies all .kicad_sch and .kicad_pro files from the worktree to the
        live project using a two-phase atomic approach:
        1. Write ALL files to temp locations (phase 1 - preparatory)
        2. Rename ALL temp files to final locations (phase 2 - atomic)

        If any file fails in phase 1, no files are committed.
        If any rename fails in phase 2, attempt to rollback already-renamed files.

        Returns:
            Dict with commit details.
        """
        if self._committed:
            return {"success": True, "already_committed": True}
        if self._rolled_back:
            return {"success": False, "error": "Transaction already rolled back"}
        if not self._worktree_path:
            return {"success": False, "error": "No worktree created"}

        committed_files: list[str] = []
        pending_renames: list[tuple[str, str]] = []  # (tmp_path, final_path)
        backup_originals: list[tuple[str, str | None]] = []  # (final_path, backup_path | None)

        try:
            # Phase 1: Copy all files to temp locations
            for root, _dirs, files in os.walk(self._worktree_path):
                for filename in files:
                    if filename.endswith((".kicad_sch", ".kicad_pro")):
                        src = os.path.join(root, filename)
                        rel_path = os.path.relpath(src, self._worktree_path)
                        dst = os.path.join(self.project_dir, rel_path)

                        dst_dir = os.path.dirname(dst)
                        os.makedirs(dst_dir, exist_ok=True)

                        tmp_dst = dst + ".tmp_commit"
                        shutil.copy2(src, tmp_dst)
                        pending_renames.append((tmp_dst, dst))

            # Phase 2: Atomically rename all temp files to final destinations
            for tmp_path, final_path in pending_renames:
                # Backup existing file if present (for rollback)
                backup_path = None
                if os.path.exists(final_path):
                    backup_path = final_path + ".bak_commit"
                    shutil.copy2(final_path, backup_path)
                backup_originals.append((final_path, backup_path))

                os.replace(tmp_path, final_path)
                rel_path = os.path.relpath(final_path, self.project_dir)
                committed_files.append(rel_path)

            # Phase 3: Clean up backups on success
            for _final_path, backup_path in backup_originals:
                if backup_path and os.path.exists(backup_path):
                    os.unlink(backup_path)

            self._committed = True
            elapsed = time.monotonic() - self._started_at
            logger.info(
                "Transaction committed: %d files in %.1fs",
                len(committed_files),
                elapsed,
            )
            return {
                "success": True,
                "committed_files": committed_files,
                "elapsed_seconds": elapsed,
            }
        except Exception as e:
            logger.error("Transaction commit failed: %s", e)

            # Rollback: restore backups for any files that were already renamed
            for final_path, backup_path in backup_originals:
                if backup_path and os.path.exists(backup_path):
                    try:
                        os.replace(backup_path, final_path)
                    except Exception as rb_err:
                        logger.error(
                            "Failed to rollback %s: %s", final_path, rb_err
                        )

            # Clean up any remaining temp files
            for tmp_path, _final_path in pending_renames:
                if os.path.exists(tmp_path):
                    with contextlib.suppress(Exception):
                        os.unlink(tmp_path)

            self._rolled_back = True
            return {
                "success": False,
                "error": f"Commit failed (rolled back): {e}",
                "committed_files": committed_files,
            }

    def rollback(self) -> dict[str, Any]:
        """Roll back the transaction (discard temp worktree).

        Returns:
            Dict with rollback details.
        """
        if self._rolled_back:
            return {"success": True, "already_rolled_back": True}
        if self._committed:
            return {"success": False, "error": "Cannot rollback committed transaction"}

        self._rolled_back = True
        elapsed = time.monotonic() - self._started_at
        logger.info("Transaction rolled back after %.1fs", elapsed)
        return {
            "success": True,
            "rolled_back": True,
            "elapsed_seconds": elapsed,
        }

    def cleanup(self) -> None:
        """Remove temporary worktree directory."""
        if self._temp_dir and os.path.isdir(self._temp_dir):
            try:
                shutil.rmtree(self._temp_dir)
                logger.debug("Cleaned up worktree: %s", self._temp_dir)
            except Exception as e:
                logger.warning("Failed to clean up worktree %s: %s", self._temp_dir, e)
            self._temp_dir = None
            self._worktree_path = None

    def get_worktree_schematic(self, name: str | None = None) -> str:
        """Get path to a schematic file in the worktree.

        Args:
            name: Sheet name (without extension). If None, returns root schematic.

        Returns:
            Full path to the .kicad_sch file.
        """
        if not self._worktree_path:
            raise RuntimeError("No worktree created")
        if name:
            return os.path.join(self._worktree_path, f"{name}.kicad_sch")
        return os.path.join(self._worktree_path, f"{self.project_name}.kicad_sch")

    def list_generated_schematics(self) -> list[str]:
        """List all .kicad_sch files in the worktree."""
        if not self._worktree_path:
            return []
        result = []
        for root, _dirs, files in os.walk(self._worktree_path):
            for f in files:
                if f.endswith(".kicad_sch"):
                    result.append(os.path.join(root, f))
        return result
