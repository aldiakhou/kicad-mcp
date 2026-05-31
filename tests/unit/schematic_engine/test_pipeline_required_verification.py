"""Tests for required netlist-first apply verification."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from kicad_mcp.schematic_engine.models import NormalizedNetlist
from kicad_mcp.schematic_engine.pipeline import apply_design_intent_netlist_first


def _write_project(tmp_path: Path) -> Path:
    project_path = tmp_path / "demo.kicad_pro"
    schematic_path = tmp_path / "demo.kicad_sch"
    project_path.write_text("{}", encoding="utf-8")
    schematic_path.write_text("original schematic", encoding="utf-8")
    return project_path


def _patch_pipeline(monkeypatch, tmp_path: Path, verifier):
    canonical = SimpleNamespace(
        parts=[SimpleNamespace(ref="R1")],
        endpoints=[SimpleNamespace(ref="R1", pin="1", net="NET1")],
        no_connects=[],
    )
    compile_result = SimpleNamespace(
        success=True,
        expected_netlist=NormalizedNetlist(nets={}),
        expected_netlist_path=str(tmp_path / "expected_netlist.json"),
        net_count=0,
    )
    lint_result = SimpleNamespace(blocking_count=0, warning_count=0, issues=[])

    class FakeCompiler:
        def __init__(self, artifact_dir=None):
            self.artifact_dir = artifact_dir

        def compile(self, canonical):
            return compile_result

    class FakeWriter:
        def __init__(self, output_dir, project_name):
            self.output_dir = Path(output_dir)
            self.project_name = project_name

        def write(self, canonical, sheet_plan):
            (self.output_dir / f"{self.project_name}.kicad_sch").write_text(
                "generated schematic",
                encoding="utf-8",
            )
            (self.output_dir / f"{self.project_name}.kicad_pro").write_text(
                "{}",
                encoding="utf-8",
            )
            return {"success": True}

    monkeypatch.setattr(
        "kicad_mcp.schematic_engine.pipeline.normalize_design_intent",
        lambda project_path, intent: canonical,
    )
    monkeypatch.setattr("kicad_mcp.schematic_engine.pipeline.SkidlCompiler", FakeCompiler)
    monkeypatch.setattr(
        "kicad_mcp.schematic_engine.pipeline.plan_sheets",
        lambda canonical, style: SimpleNamespace(sheets={"root": ["R1"]}),
    )
    monkeypatch.setattr(
        "kicad_mcp.schematic_engine.pipeline.visual_lint",
        lambda canonical, sheet_plan: lint_result,
    )
    monkeypatch.setattr("kicad_mcp.schematic_engine.pipeline.SchematicWriter", FakeWriter)
    monkeypatch.setattr("kicad_mcp.schematic_engine.pipeline.KicadCliVerifier", verifier)


def test_apply_rolls_back_on_kicad_cli_failure(tmp_path: Path, monkeypatch):
    project_path = _write_project(tmp_path)

    class FailingVerifier:
        def __init__(self, output_dir=None):
            self.output_dir = output_dir

        def verify(self, *args, **kwargs):
            raise RuntimeError("kicad-cli failed")

    _patch_pipeline(monkeypatch, tmp_path, FailingVerifier)

    result = apply_design_intent_netlist_first(str(project_path), {"parts": []})

    assert result["success"] is False
    assert result["changed"] is False
    assert result["rolled_back"] is True
    assert result["stage"] == "kicad_cli_verification_failed"
    assert "kicad-cli failed" in result["error"]
    assert (tmp_path / "demo.kicad_sch").read_text(encoding="utf-8") == "original schematic"


def test_apply_success_requires_kicad_netlist_output(tmp_path: Path, monkeypatch):
    project_path = _write_project(tmp_path)

    class MissingNetlistVerifier:
        def __init__(self, output_dir=None):
            self.output_dir = output_dir

        def verify(self, *args, **kwargs):
            return SimpleNamespace(
                erc_errors=0,
                erc_warnings=0,
                erc_total=0,
                erc_path=None,
                svg_dir=None,
                netlist_path=None,
            )

    _patch_pipeline(monkeypatch, tmp_path, MissingNetlistVerifier)

    result = apply_design_intent_netlist_first(str(project_path), {"parts": []})

    assert result["success"] is False
    assert result["changed"] is False
    assert result["rolled_back"] is True
    assert result["stage"] == "netlist_mismatch"
    assert "netlist export did not produce output" in result["error"]
    assert (tmp_path / "demo.kicad_sch").read_text(encoding="utf-8") == "original schematic"
