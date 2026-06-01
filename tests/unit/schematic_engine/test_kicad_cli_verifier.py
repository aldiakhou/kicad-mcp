"""Tests for KiCad CLI schematic verification report parsing."""

from __future__ import annotations

import json
from pathlib import Path

from kicad_mcp.schematic_engine.kicad_cli_verifier import KicadCliVerifier


def test_parse_erc_result_counts_sheet_nested_violations(tmp_path: Path):
    erc_path = tmp_path / "erc.json"
    erc_path.write_text(
        json.dumps({
            "sheets": [
                {
                    "violations": [
                        {"type": "pin_not_connected", "severity": "error"},
                        {"type": "label_dangling", "severity": "warning"},
                    ]
                }
            ]
        }),
        encoding="utf-8",
    )

    result = KicadCliVerifier()._parse_erc_result(str(erc_path))

    assert result["success"] is True
    assert result["total"] == 2
    assert result["errors"] == 1
    assert result["warnings"] == 1
    assert len(result["violations"]) == 2
