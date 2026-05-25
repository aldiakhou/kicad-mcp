from pathlib import Path

from kicad_mcp.utils.kicad_cli_batch import (
    clear_schematic_validation_cache,
    invalidate_schematic_validation_cache,
    validate_schematic_batch,
)


def test_validate_schematic_batch_reuses_same_file_revision(monkeypatch, tmp_path: Path):
    clear_schematic_validation_cache()
    schematic = tmp_path / "demo.kicad_sch"
    schematic.write_text("(kicad_sch)", encoding="utf-8")
    calls = {"netlist": 0, "erc": 0}

    def fake_netlist(path: str, timeout_seconds: float | None = None):
        calls["netlist"] += 1
        return {"success": True, "schematic_path": path, "nets": {}, "components": {}}

    def fake_erc(path: str, timeout_seconds: float | None = None):
        calls["erc"] += 1
        return {"success": True, "schematic_path": path, "total_violations": 0}

    monkeypatch.setattr("kicad_mcp.utils.kicad_cli_batch.export_native_netlist", fake_netlist)
    monkeypatch.setattr("kicad_mcp.utils.kicad_cli_batch.run_erc_via_cli", fake_erc)

    first = validate_schematic_batch(str(schematic), need_netlist=True, need_erc=True)
    second = validate_schematic_batch(str(schematic), need_netlist=True, need_erc=True)

    assert first is second
    assert calls == {"netlist": 1, "erc": 1}

    invalidate_schematic_validation_cache(str(schematic))
    validate_schematic_batch(str(schematic), need_netlist=True, need_erc=True)

    assert calls == {"netlist": 2, "erc": 2}
