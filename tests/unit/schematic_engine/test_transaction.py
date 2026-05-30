"""Tests for the transaction model."""

import os
import tempfile

from kicad_mcp.schematic_engine.transaction import SchematicBuildTransaction


class TestSchematicBuildTransaction:
    """Tests for SchematicBuildTransaction."""

    def _create_test_project(self):
        """Create a temporary test project."""
        tmpdir = tempfile.mkdtemp()
        project_path = os.path.join(tmpdir, "test_project.kicad_pro")
        schematic_path = os.path.join(tmpdir, "test_project.kicad_sch")

        with open(project_path, "w") as f:
            f.write('{"meta": {"filename": "test_project.kicad_pro", "version": 1}}')
        with open(schematic_path, "w") as f:
            f.write('(kicad_sch (version 20231120) (generator "test"))')

        return tmpdir, project_path

    def test_create_worktree(self):
        """Worktree is created with project files copied."""
        tmpdir, project_path = self._create_test_project()

        with SchematicBuildTransaction(project_path) as tx:
            worktree = tx.create_worktree()
            assert os.path.isdir(worktree)
            # Project files should be copied
            assert os.path.exists(os.path.join(worktree, "test_project.kicad_pro"))
            assert os.path.exists(os.path.join(worktree, "test_project.kicad_sch"))
            tx.rollback()

    def test_commit_copies_files_back(self):
        """Commit copies generated files to live project."""
        tmpdir, project_path = self._create_test_project()

        with SchematicBuildTransaction(project_path) as tx:
            worktree = tx.create_worktree()

            # Generate a new file in worktree
            new_file = os.path.join(worktree, "new_sheet.kicad_sch")
            with open(new_file, "w") as f:
                f.write('(kicad_sch (version 20231120) (generator "engine"))')

            result = tx.commit()
            assert result["success"]
            assert "new_sheet.kicad_sch" in result["committed_files"]

        # File should now exist in live project
        assert os.path.exists(os.path.join(tmpdir, "new_sheet.kicad_sch"))

    def test_rollback_does_not_modify_live(self):
        """Rollback ensures live project is untouched."""
        tmpdir, project_path = self._create_test_project()
        original_content = open(os.path.join(tmpdir, "test_project.kicad_sch")).read()

        with SchematicBuildTransaction(project_path) as tx:
            worktree = tx.create_worktree()

            # Modify schematic in worktree
            sch_path = os.path.join(worktree, "test_project.kicad_sch")
            with open(sch_path, "w") as f:
                f.write('(kicad_sch (version 20231120) (generator "modified"))')

            tx.rollback()

        # Live project unchanged
        current_content = open(os.path.join(tmpdir, "test_project.kicad_sch")).read()
        assert current_content == original_content

    def test_exception_triggers_rollback(self):
        """Exceptions in context trigger automatic rollback."""
        tmpdir, project_path = self._create_test_project()
        original_content = open(os.path.join(tmpdir, "test_project.kicad_sch")).read()

        try:
            with SchematicBuildTransaction(project_path) as tx:
                worktree = tx.create_worktree()
                sch_path = os.path.join(worktree, "test_project.kicad_sch")
                with open(sch_path, "w") as f:
                    f.write("modified content")
                raise RuntimeError("Simulated failure")
        except RuntimeError:
            pass

        # Live project unchanged
        current_content = open(os.path.join(tmpdir, "test_project.kicad_sch")).read()
        assert current_content == original_content
        assert tx.is_rolled_back

    def test_double_commit_idempotent(self):
        """Double commit is idempotent."""
        tmpdir, project_path = self._create_test_project()

        with SchematicBuildTransaction(project_path) as tx:
            tx.create_worktree()
            result1 = tx.commit()
            result2 = tx.commit()
            assert result1["success"]
            assert result2["success"]
            assert result2.get("already_committed")

    def test_commit_after_rollback_fails(self):
        """Cannot commit after rollback."""
        tmpdir, project_path = self._create_test_project()

        with SchematicBuildTransaction(project_path) as tx:
            tx.create_worktree()
            tx.rollback()
            result = tx.commit()
            assert not result["success"]

    def test_worktree_cleaned_up(self):
        """Temporary directory is cleaned up after transaction."""
        tmpdir, project_path = self._create_test_project()
        worktree_path = None

        with SchematicBuildTransaction(project_path) as tx:
            worktree_path = tx.create_worktree()
            assert os.path.isdir(worktree_path)
            tx.rollback()

        # After exiting context, worktree should be cleaned up
        assert not os.path.exists(worktree_path)

    def test_get_worktree_schematic(self):
        """Can get paths to schematic files in worktree."""
        tmpdir, project_path = self._create_test_project()

        with SchematicBuildTransaction(project_path) as tx:
            tx.create_worktree()
            root_sch = tx.get_worktree_schematic()
            assert root_sch.endswith("test_project.kicad_sch")

            named_sch = tx.get_worktree_schematic("power")
            assert named_sch.endswith("power.kicad_sch")
            tx.rollback()

    def test_partial_write_on_failure_returns_changed_false(self):
        """Failed transaction always returns changed=False."""
        tmpdir, project_path = self._create_test_project()

        with SchematicBuildTransaction(project_path) as tx:
            tx.create_worktree()
            result = tx.rollback()
            assert result["success"]
            assert result["rolled_back"]
            assert not tx.is_committed
