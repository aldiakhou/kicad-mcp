"""Tests for the required SKiDL compiler path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_mcp.schematic_engine.expected_netlist import compare_netlists
from kicad_mcp.schematic_engine.models import (
    CanonicalCircuit,
    CircuitEndpoint,
    CircuitPart,
    NetlistEntry,
    NormalizedNetlist,
)
from kicad_mcp.schematic_engine.skidl_compiler import (
    SkidlCompiler,
    SkidlCompileResult,
    _custom_pin_alias_lookup,
    _pin_lookup_key,
    _resolved_selector_netlist,
)


def _make_canonical() -> CanonicalCircuit:
    return CanonicalCircuit(
        project_path="/tmp/test.kicad_pro",
        parts=[CircuitPart(ref="R1", lib_id="Device:R", value="10k")],
        endpoints=[
            CircuitEndpoint(ref="R1", pin="1", net="+3V3"),
            CircuitEndpoint(ref="R1", pin="2", net="GND"),
        ],
        no_connects=[],
        blocks={},
        rails={"+3V3", "GND"},
    )


def test_compiler_fails_without_skidl(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("kicad_mcp.schematic_engine.skidl_compiler._SKIDL_AVAILABLE", False)
    compiler = SkidlCompiler(artifact_dir=str(tmp_path))

    with pytest.raises(RuntimeError, match="SKiDL is required"):
        compiler.compile(_make_canonical())


def test_compiler_routes_to_skidl_when_available(monkeypatch, tmp_path: Path):
    expected = SkidlCompileResult(success=True, part_count=1)
    called = {}

    def fake_compile(self, canonical):
        called["canonical"] = canonical
        return expected

    monkeypatch.setattr("kicad_mcp.schematic_engine.skidl_compiler._SKIDL_AVAILABLE", True)
    monkeypatch.setattr(SkidlCompiler, "_compile_with_skidl", fake_compile)

    compiler = SkidlCompiler(artifact_dir=str(tmp_path))
    result = compiler.compile(_make_canonical())

    assert result is expected
    assert called["canonical"].parts[0].ref == "R1"


def test_expected_netlist_json_saved(tmp_path: Path):
    canonical = _make_canonical()
    netlist = NormalizedNetlist(
        nets={
            "+3V3": {NetlistEntry(ref="R1", pin="1")},
            "GND": {NetlistEntry(ref="R1", pin="2")},
        }
    )

    compiler = SkidlCompiler(artifact_dir=str(tmp_path))
    path = compiler._save_expected_netlist(canonical, netlist)

    assert path is not None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert "+3V3" in data["nets"]
    assert "GND" in data["nets"]
    assert data["metadata"]["part_count"] == 1


def test_duplicate_same_function_pins_are_not_collapsed_in_expected_netlist():
    pins = [
        {"number": "A4", "name": "VBUS"},
        {"number": "B4", "name": "VBUS"},
        {"number": "A6", "name": "D+"},
        {"number": "B6", "name": "D+"},
        {"number": "A7", "name": "D-"},
        {"number": "B7", "name": "D-"},
    ]
    part = CircuitPart(
        ref="J1",
        lib_id="kicad_mcp_custom:J1_USB_C",
        value="USB-C",
        properties={
            "KICAD_MCP_CUSTOM_PINS": json.dumps(pins),
        },
    )
    canonical = CanonicalCircuit(
        project_path="/tmp/test.kicad_pro",
        parts=[part],
        endpoints=[
            CircuitEndpoint(ref="J1", pin="VBUS", net="VBUS"),
            CircuitEndpoint(ref="J1", pin="D+", net="USB_D_P"),
            CircuitEndpoint(ref="J1", pin="D-", net="USB_D_N"),
        ],
        no_connects=[],
        blocks={},
        rails={"VBUS", "GND"},
    )

    aliases = _custom_pin_alias_lookup(part)
    netlist = _resolved_selector_netlist(canonical, {"J1": aliases})

    assert netlist.nets["VBUS"] == {
        NetlistEntry("J1", "A4"),
        NetlistEntry("J1", "B4"),
    }
    assert netlist.nets["USB_D_P"] == {
        NetlistEntry("J1", "A6"),
        NetlistEntry("J1", "B6"),
    }
    assert netlist.nets["USB_D_N"] == {
        NetlistEntry("J1", "A7"),
        NetlistEntry("J1", "B7"),
    }
    assert _pin_lookup_key("D+") != _pin_lookup_key("D-")

    actual = NormalizedNetlist(nets={
        "VBUS": {NetlistEntry("J1", "A4")},
        "USB_D_P": {NetlistEntry("J1", "A6"), NetlistEntry("J1", "B6")},
        "USB_D_N": {NetlistEntry("J1", "A7"), NetlistEntry("J1", "B7")},
    })
    compare = compare_netlists(netlist, actual)

    assert compare.success is False
    assert {"net": "VBUS", "ref": "J1", "pin": "B4"} in compare.missing_endpoints
