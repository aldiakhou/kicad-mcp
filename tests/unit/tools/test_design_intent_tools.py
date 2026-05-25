import json
from pathlib import Path

import pytest

from kicad_mcp.server import create_server
from kicad_mcp.tools import creation_tools


def _tool_intent() -> dict:
    return {
        "parts": [
            {
                "ref": "U1",
                "value": "MCU",
                "footprint": "Package_QFP:LQFP-48_7x7mm_P0.5mm",
                "pins": [
                    {"number": "1", "name": "VDD", "type": "power_in"},
                    {"number": "2", "name": "GND", "type": "power_in"},
                    {"number": "3", "name": "PB6", "type": "bidirectional"},
                    {"number": "4", "name": "PB7", "type": "bidirectional"},
                ],
            },
            {
                "ref": "U2",
                "value": "SENSOR",
                "footprint": "Package_LGA:LGA-4_2x2mm_P0.65mm",
                "pins": [
                    {"number": "1", "name": "SCL", "type": "bidirectional"},
                    {"number": "2", "name": "SDA", "type": "bidirectional"},
                    {"number": "3", "name": "VDD", "type": "power_in"},
                    {"number": "4", "name": "GND", "type": "power_in"},
                ],
            },
        ],
        "pin_rules": [
            {"ref": "U1", "match": {"pin": "VDD"}, "net": "+3V3"},
            {"ref": "U1", "match": {"pin": "GND"}, "net": "GND"},
            {"ref": "U2", "match": {"pin": "VDD"}, "net": "+3V3"},
            {"ref": "U2", "match": {"pin": "GND"}, "net": "GND"},
        ],
        "interfaces": [
            {
                "type": "i2c",
                "name": "SENSOR_I2C",
                "controller": {"ref": "U1", "scl": "PB6", "sda": "PB7"},
                "devices": [{"ref": "U2", "scl": "SCL", "sda": "SDA"}],
                "pullups": {"rail": "+3V3", "value": "4.7k"},
            }
        ],
    }


@pytest.mark.asyncio
async def test_schematic_preview_design_intent_returns_compact_expanded_summary(tmp_path: Path):
    server = create_server()
    tools = await server.get_tools()

    result = tools["schematic_preview_design_intent"].fn(str(tmp_path), _tool_intent())

    assert result["success"] is True
    assert result["tool"] == "schematic_preview_design_intent"
    assert result["stage"] == "preview"
    assert result["changed"] is False
    assert result["summary"]["generated_part_count"] == 2
    assert "expanded_spec" not in result
    assert "diff" not in result
    assert Path(result["expanded_spec_path"]).exists()
    assert Path(result["visual_expanded_spec_path"]).exists()
    visual_spec = json.loads(Path(result["visual_expanded_spec_path"]).read_text(encoding="utf-8"))
    assert visual_spec["layout_hints"]["label_strategy"] == "external_stubs"
    assert visual_spec["parts"][0]["x"] is not None


@pytest.mark.asyncio
async def test_schematic_apply_design_intent_dry_run_can_include_expanded_spec(tmp_path: Path):
    server = create_server()
    tools = await server.get_tools()

    result = tools["schematic_apply_design_intent"].fn(
        str(tmp_path),
        _tool_intent(),
        "update",
        True,
        False,
        "compact",
        True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["recommended_next_tool"] == "schematic_apply_expanded_spec"
    assert "expanded_spec" in result
    assert result["expanded_spec"]["nets"]["SENSOR_I2C_SCL"]
    assert Path(result["visual_expanded_spec_path"]).exists()


@pytest.mark.asyncio
async def test_schematic_apply_design_intent_reports_compile_errors_before_build(tmp_path: Path):
    server = create_server()
    tools = await server.get_tools()

    result = tools["schematic_apply_design_intent"].fn(
        str(tmp_path),
        {"parts": _tool_intent()["parts"], "pin_rules": [{"ref": "U1", "match": {"pin": "NOPE"}, "net": "X"}]},
        "update",
        False,
        False,
        "compact",
        False,
    )

    assert result["success"] is False
    assert result["stage"] == "compile_failed"
    assert result["recoverable"] is True
    assert result["errors"][0]["error"] == "selector matched zero pins"


@pytest.mark.asyncio
async def test_schematic_design_intent_schema_returns_executable_examples(tmp_path: Path):
    server = create_server()
    tools = await server.get_tools()

    schema = tools["schematic_design_intent_schema"].fn("all")

    assert schema["success"] is True
    assert "intent" in schema["schemas"]
    assert "interfaces" in schema["schemas"]["intent"]["accepted_top_level_shape"]
    assert isinstance(schema["schemas"]["intent"]["accepted_top_level_shape"]["interfaces"], dict)
    assert "support_circuits.decoupling" in schema["schemas"]
    assert schema["schemas"]["rails"]["alternate_example"] == [
        {"name": "+3V3", "pins": [["U1", "VDD"]]}
    ]
    assert (
        schema["schemas"]["support_circuits.led_indicator"]["generated_nets_summary"]
        == "Resistor connects rail to LED anode net; LED cathode connects to ground."
    )

    custom_parts = [
        {
            "ref": "U1",
            "value": "MCU",
            "footprint": "Package_QFP:LQFP-48_7x7mm_P0.5mm",
            "pins": [
                {"number": "1", "name": "VDD", "type": "power_in"},
                {"number": "2", "name": "VSS", "type": "power_in"},
                {"number": "3", "name": "PB6", "type": "bidirectional"},
                {"number": "4", "name": "PB7", "type": "bidirectional"},
                {"number": "5", "name": "PA5", "type": "bidirectional"},
                {"number": "6", "name": "PA6", "type": "bidirectional"},
                {"number": "7", "name": "PA7", "type": "bidirectional"},
                {"number": "8", "name": "PA13", "type": "bidirectional"},
                {"number": "9", "name": "PA14", "type": "bidirectional"},
                {"number": "10", "name": "NRST", "type": "input"},
                {"number": "11", "name": "PA0", "type": "bidirectional"},
                {"number": "12", "name": "BOOT0", "type": "input"},
            ],
        },
        {
            "ref": "U2",
            "value": "IMU",
            "footprint": "Package_LGA:LGA-8_2.0x2.5mm_P0.65mm",
            "pins": [
                {"number": "1", "name": "SCL", "type": "bidirectional"},
                {"number": "2", "name": "SDA", "type": "bidirectional"},
                {"number": "3", "name": "INT", "type": "output"},
                {"number": "4", "name": "VDD", "type": "power_in"},
                {"number": "5", "name": "GND", "type": "power_in"},
            ],
        },
        {
            "ref": "U3",
            "value": "FLASH",
            "footprint": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
            "pins": [
                {"number": "1", "name": "~{CS}", "type": "input"},
                {"number": "2", "name": "SO", "type": "output"},
                {"number": "4", "name": "GND", "type": "power_in"},
                {"number": "5", "name": "SI", "type": "input"},
                {"number": "6", "name": "SCK", "type": "input"},
                {"number": "8", "name": "VCC", "type": "power_in"},
            ],
        },
    ]
    examples = schema["schemas"]
    intent = {
        "parts": custom_parts,
        "rails": examples["rails"]["alternate_example"],
        "pin_rules": examples["pin_rules"]["example"],
        "interfaces": (
            examples["interfaces.i2c"]["example"]
            + examples["interfaces.spi"]["example"]
            + examples["interfaces.swd"]["example"]
        ),
        "support_circuits": [
            examples["support_circuits.decoupling"]["example"][0],
            examples["support_circuits.pullup"]["example"][0],
            examples["support_circuits.pulldown"]["example"][0],
            examples["support_circuits.crystal"]["example"][0],
            examples["support_circuits.reset_button"]["example"][0],
            examples["support_circuits.led_indicator"]["example"][0],
            examples["support_circuits.ferrite_filter"]["example"][0],
            examples["support_circuits.power_flag"]["example"][0],
            examples["support_circuits.connector_header"]["example"][0],
        ],
        "bulk_connections": examples["bulk_connections"]["example"],
        "no_connect_rules": examples["no_connect_rules"]["example"],
    }

    preview = tools["schematic_preview_design_intent"].fn(str(tmp_path), intent)

    assert preview["success"] is True
    assert preview["summary"]["generated_part_count"] >= 12
    assert preview["summary"]["net_count"] >= 10


def test_schematic_apply_design_intent_strict_fails_on_bad_quality_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        creation_tools,
        "compile_design_intent",
        lambda project_path, intent, strict=False: {
            "success": True,
            "expanded_spec": {"parts": [], "nets": {}, "no_connects": [], "layout_hints": {}},
            "summary": {},
            "generated_refs": {},
            "warnings": [],
            "errors": [],
            "expanded_spec_path": str(tmp_path / "expanded.json"),
        },
    )
    monkeypatch.setattr(
        creation_tools,
        "build_schematic_from_spec_v2",
        lambda *args, **kwargs: {
            "success": True,
            "validation": {"post_write": {"missing": []}},
        },
    )
    monkeypatch.setattr(
        creation_tools,
        "build_quality_report",
        lambda *args, **kwargs: {
            "success": True,
            "native_netlist": {"success": True},
            "erc": {"total_violations": 1},
            "quality_gate": {"passed": False},
        },
    )

    result = creation_tools._schematic_design_intent_response(
        str(tmp_path),
        {},
        mode="update",
        dry_run=False,
        strict=True,
        detail="compact",
        include_expanded_spec=False,
        tool_name="schematic_apply_design_intent",
    )

    assert result["success"] is False
    assert result["stage"] == "verification_failed"
    assert result["recoverable"] is True
    assert result["errors"][0]["error"] == "strict mode verification failed"


def test_schematic_apply_design_intent_non_strict_reports_but_allows_bad_quality_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        creation_tools,
        "compile_design_intent",
        lambda project_path, intent, strict=False: {
            "success": True,
            "expanded_spec": {"parts": [], "nets": {}, "no_connects": [], "layout_hints": {}},
            "summary": {},
            "generated_refs": {},
            "warnings": [],
            "errors": [],
            "expanded_spec_path": str(tmp_path / "expanded.json"),
        },
    )
    monkeypatch.setattr(
        creation_tools,
        "build_schematic_from_spec_v2",
        lambda *args, **kwargs: {
            "success": True,
            "validation": {"post_write": {"missing": []}},
        },
    )
    monkeypatch.setattr(
        creation_tools,
        "build_quality_report",
        lambda *args, **kwargs: {
            "success": True,
            "native_netlist": {"success": True},
            "erc": {"total_violations": 1},
            "quality_gate": {"passed": False},
        },
    )

    result = creation_tools._schematic_design_intent_response(
        str(tmp_path),
        {},
        mode="update",
        dry_run=False,
        strict=False,
        detail="compact",
        include_expanded_spec=False,
        tool_name="schematic_apply_design_intent",
    )

    assert result["success"] is True
    assert result["verification"]["erc_total_violations"] == 1
    assert result["verification"]["quality_gate_passed"] is False


def test_schematic_apply_design_intent_respects_visual_layout_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        creation_tools,
        "compile_design_intent",
        lambda project_path, intent, strict=False: {
            "success": True,
            "expanded_spec": {"parts": [], "nets": {}, "no_connects": []},
            "summary": {},
            "generated_refs": {},
            "warnings": [],
            "errors": [],
            "expanded_spec_path": str(tmp_path / "expanded.json"),
        },
    )

    def fake_build(project_path, spec, **kwargs):
        captured["spec"] = spec
        captured["kwargs"] = kwargs
        return {"success": True, "native_netlist": {"success": None, "skipped": True}}

    monkeypatch.setattr(creation_tools, "build_schematic_from_spec_v2", fake_build)

    result = creation_tools._schematic_design_intent_response(
        str(tmp_path),
        {},
        mode="update",
        dry_run=False,
        strict=False,
        detail="compact",
        include_expanded_spec=False,
        tool_name="schematic_apply_design_intent",
        visual_layout=False,
        quick_apply=True,
    )

    assert result["success"] is True
    assert captured["kwargs"]["apply_default_visual_layout"] is False
    assert captured["spec"]["layout_hints"]["visual_layout"]["enabled"] is False


def test_schematic_apply_design_intent_quick_apply_skips_expensive_post_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        creation_tools,
        "compile_design_intent",
        lambda project_path, intent, strict=False: {
            "success": True,
            "expanded_spec": {"parts": [], "nets": {}, "no_connects": [], "layout_hints": {}},
            "summary": {},
            "generated_refs": {},
            "warnings": [],
            "errors": [],
            "expanded_spec_path": str(tmp_path / "expanded.json"),
        },
    )
    monkeypatch.setattr(
        creation_tools,
        "build_schematic_from_spec_v2",
        lambda *args, **kwargs: {
            "success": True,
            "native_netlist": {"success": None, "skipped": True},
        },
    )
    monkeypatch.setattr(
        creation_tools,
        "build_quality_report",
        lambda *args, **kwargs: pytest.fail("quality report should be skipped"),
    )

    result = creation_tools._schematic_design_intent_response(
        str(tmp_path),
        {},
        mode="update",
        dry_run=False,
        strict=False,
        detail="compact",
        include_expanded_spec=False,
        tool_name="schematic_apply_design_intent",
        quick_apply=True,
        include_preview=True,
        run_quality_report=True,
        run_native_validation=True,
    )

    assert result["success"] is True
    assert result["post_steps"] == {
        "include_preview": False,
        "run_quality_report": False,
        "run_native_validation": False,
    }
    assert result["verification"]["quality_report_skipped"] is True
    assert "schematic_preview" not in result


def test_schematic_apply_expanded_spec_reuses_saved_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    expanded_path = tmp_path / ".kicad_mcp" / "design_intent.expanded_spec.json"
    expanded_path.parent.mkdir()
    expanded_path.write_text(
        json.dumps({"parts": [], "nets": {}, "no_connects": [], "layout_hints": {}}),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_build(project_path, spec, **kwargs):
        captured["project_path"] = project_path
        captured["spec"] = spec
        captured["kwargs"] = kwargs
        return {"success": True, "native_netlist": {"success": None, "skipped": True}}

    monkeypatch.setattr(creation_tools, "build_schematic_from_spec_v2", fake_build)

    result = creation_tools._schematic_apply_expanded_spec_response(
        str(tmp_path),
        expanded_spec_path=".kicad_mcp/design_intent.expanded_spec.json",
        spec=None,
        mode="update",
        strict=False,
        detail="compact",
        quick_apply=True,
        include_preview=True,
        run_quality_report=True,
        run_native_validation=True,
        visual_layout=False,
    )

    assert result["success"] is True
    assert result["tool"] == "schematic_apply_expanded_spec"
    assert captured["project_path"] == str(tmp_path)
    assert captured["kwargs"]["run_native_validation"] is False
    assert captured["kwargs"]["apply_default_visual_layout"] is False
    assert captured["spec"]["layout_hints"]["visual_layout"]["enabled"] is False


def test_large_design_preview_recommends_staged_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        creation_tools,
        "compile_design_intent",
        lambda project_path, intent, strict=False: {
            "success": True,
            "expanded_spec": {
                "parts": [{"ref": f"R{i}", "lib_id": "Device:R"} for i in range(26)],
                "nets": {"N": [["R1", "1"]] * 76},
                "no_connects": [],
                "layout_hints": {},
            },
            "summary": {"total_part_count": 26, "connection_count": 76},
            "generated_refs": {},
            "warnings": [],
            "errors": [],
            "expanded_spec_path": str(tmp_path / "expanded.json"),
        },
    )

    result = creation_tools._schematic_design_intent_response(
        str(tmp_path),
        {},
        mode="update",
        dry_run=True,
        strict=False,
        detail="compact",
        include_expanded_spec=False,
        tool_name="schematic_preview_design_intent",
    )

    assert result["recommended_next_tool"] == "schematic_build_from_spec_v2"
    assert result["recommended_workflow"] == "large_design_staged_apply"
    assert "26 parts / 76 connections" in result["recommendation_reason"]


def test_design_intent_job_status_and_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        creation_tools,
        "_schematic_design_intent_response",
        lambda *args, **kwargs: {"success": True, "stage": "schematic_built"},
    )

    start = creation_tools._start_design_intent_job(
        str(tmp_path),
        {},
        mode="update",
        strict=False,
        detail="compact",
        include_expanded_spec=False,
        visual_layout=True,
        visual_style="readable",
        quick_apply=True,
        include_preview=False,
        run_quality_report=False,
        run_native_validation=False,
    )
    creation_tools._DESIGN_INTENT_JOBS[start["job_id"]]["future"].result(timeout=2)
    status = creation_tools._get_design_intent_job_status(start["job_id"])
    result = creation_tools._get_design_intent_job_result(start["job_id"])

    assert status["success"] is True
    assert status["status"] == "completed"
    assert result["success"] is True
    assert result["result"]["success"] is True
