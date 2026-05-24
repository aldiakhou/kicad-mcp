"""Spec-driven schematic building helpers."""

from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
from typing import Any, cast

from kicad_mcp.utils.file_utils import get_project_files
from kicad_mcp.utils.kicad_s_expr import KiCadSchematic, SExprAtom, SExprList
from kicad_mcp.utils.library_resolver import resolve_footprint, resolve_symbol
from kicad_mcp.utils.native_netlist import export_native_netlist, run_erc_via_cli
from kicad_mcp.utils.preview_metadata import svg_preview_metadata
from kicad_mcp.utils.schematic_intent import (
    apply_connection_plan_v2,
    normalize_connections,
)
from kicad_mcp.utils.schematic_pins import (
    SCHEMATIC_GRID_MM,
    add_no_connect_to_pin,
    attach_net_to_pin,
    get_symbol_pin_map_from_schematic,
)
from kicad_mcp.utils.transactional_edit import export_schematic_svg_file

UNACCEPTABLE_ERC_TYPES = {
    "endpoint_off_grid",
    "label_dangling",
    "isolated_pin_label",
    "ground_pin_not_ground",
}


def card_reader_v1_spec() -> dict[str, Any]:
    """Return the built-in clean Card Reader schematic specification."""
    symbols = [
        _sym("J1", "Connector:USB_C_Receptacle_USB2.0_14P", "USB_C_Receptacle_USB2.0_14P", 35, 65, footprint="Connector_USB:USB_C_Receptacle_XKB_U262-16XN-4BVC11"),
        _sym("R1", "Device:R", "5.1k", 75, 78, 90, "Resistor_SMD:R_0603_1608Metric"),
        _sym("R2", "Device:R", "5.1k", 75, 92, 90, "Resistor_SMD:R_0603_1608Metric"),
        _sym("D1", "Diode:SMF5V0A", "SMF5V0A", 95, 45, 180, "Diode_SMD:D_SMF"),
        _sym("U1", "Regulator_Linear:AMS1117-3.3", "AMS1117-3.3", 125, 65, footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2"),
        _sym("C1", "Device:C", "10u", 105, 92, footprint="Capacitor_SMD:C_1206_3216Metric"),
        _sym("C2", "Device:C", "22u", 145, 92, footprint="Capacitor_SMD:C_1206_3216Metric"),
        _sym("C3", "Device:C", "0.1u", 165, 92, footprint="Capacitor_SMD:C_0805_2012Metric"),
        _sym("U2", "RF_Module:ESP32-S3-WROOM-1", "ESP32-S3-WROOM-1", 210, 115, footprint="RF_Module:ESP32-S3-WROOM-1"),
        _sym("C4", "Device:C", "0.1u", 210, 158, footprint="Capacitor_SMD:C_0805_2012Metric"),
        _sym("R4", "Device:R", "10k", 175, 158, footprint="Resistor_SMD:R_0603_1608Metric"),
        _sym("SW1", "Switch:SW_Push", "RESET", 165, 180, 0, "Button_Switch_SMD:SW_SPST_TL3342"),
        _sym("R7", "Device:R", "10k", 198, 180, footprint="Resistor_SMD:R_0603_1608Metric"),
        _sym("SW2", "Switch:SW_Push", "BOOT", 225, 180, 0, "Button_Switch_SMD:SW_SPST_TL3342"),
        _sym("J2", "Connector_Generic:Conn_01x04", "UART_Debug_Header", 210, 232, footprint="Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"),
        _sym("J3", "Connector_Generic:Conn_01x06", "PN532_I2C_Module", 290, 232, footprint="Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical"),
        _sym("R5", "Device:R", "4.7k", 280, 198, footprint="Resistor_SMD:R_0603_1608Metric"),
        _sym("R6", "Device:R", "4.7k", 296, 198, footprint="Resistor_SMD:R_0603_1608Metric"),
        _sym("U3", "Display_Character:NHD-0420H1Z", "NHD-0420H1Z", 350, 105, footprint="Display:NHD-0420H1Z"),
        _sym("RV1", "Device:R_Potentiometer", "10k", 325, 160, footprint="Potentiometer_THT:Potentiometer_Bourns_3296W_Vertical"),
        _sym("R3", "Device:R", "100", 318, 58, 90, "Resistor_SMD:R_0603_1608Metric"),
        _sym("#FLG01", "power:PWR_FLAG", "PWR_FLAG", 105, 30),
        _sym("#FLG03", "power:PWR_FLAG", "PWR_FLAG", 185, 30),
        _sym("#FLG04", "power:PWR_FLAG", "PWR_FLAG", 332, 180),
    ]
    connections = [
        *[_conn("J1", pin, "+5V", True) for pin in ("A4", "A9", "B4", "B9")],
        *[_conn("J1", pin, "GND", True) for pin in ("A1", "A12", "B1", "B12", "SH")],
        _conn("J1", "A5", "USB_CC1"),
        _conn("J1", "B5", "USB_CC2"),
        _conn("J1", "A6", "USB_D+"),
        _conn("J1", "B6", "USB_D+"),
        _conn("J1", "A7", "USB_D-"),
        _conn("J1", "B7", "USB_D-"),
        _conn("R1", "1", "USB_CC1"),
        _conn("R1", "2", "GND"),
        _conn("R2", "1", "USB_CC2"),
        _conn("R2", "2", "GND"),
        _conn("D1", "1", "+5V"),
        _conn("D1", "2", "GND"),
        _conn("U1", "VI", "+5V"),
        _conn("U1", "VO", "+3.3V"),
        _conn("U1", "GND", "GND"),
        *[_conn(ref, "1", high) for ref, high in (("C1", "+5V"), ("C2", "+3.3V"), ("C3", "+3.3V"), ("C4", "+3.3V"))],
        *[_conn(ref, "2", "GND") for ref in ("C1", "C2", "C3", "C4")],
        _conn("U2", "1", "GND", True),
        _conn("U2", "40", "GND", True),
        _conn("U2", "41", "GND", True),
        _conn("U2", "3V3", "+3.3V"),
        _conn("U2", "EN", "ESP_EN"),
        _conn("U2", "USB_D+", "USB_D+"),
        _conn("U2", "USB_D-", "USB_D-"),
        _conn("U2", "TXD0", "UART_TXD0"),
        _conn("U2", "RXD0", "UART_RXD0"),
        _conn("U2", "IO13", "NFC_SDA"),
        _conn("U2", "IO47", "NFC_SCL"),
        _conn("U2", "IO11", "NFC_IRQ"),
        _conn("U2", "IO12", "NFC_RST"),
        _conn("U2", "IO4", "LCD_RS"),
        _conn("U2", "IO6", "LCD_E"),
        _conn("U2", "IO7", "LCD_D4"),
        _conn("U2", "IO8", "LCD_D5"),
        _conn("U2", "IO9", "LCD_D6"),
        _conn("U2", "IO10", "LCD_D7"),
        _conn("U2", "IO14", "ESP_BOOT"),
        _conn("R4", "1", "ESP_EN"),
        _conn("R4", "2", "+3.3V"),
        _conn("SW1", "1", "ESP_EN"),
        _conn("SW1", "2", "GND"),
        _conn("R7", "1", "ESP_BOOT"),
        _conn("R7", "2", "+3.3V"),
        _conn("SW2", "1", "ESP_BOOT"),
        _conn("SW2", "2", "GND"),
        _conn("J2", "1", "+3.3V"),
        _conn("J2", "2", "GND"),
        _conn("J2", "3", "UART_TXD0"),
        _conn("J2", "4", "UART_RXD0"),
        _conn("J3", "1", "+3.3V"),
        _conn("J3", "2", "GND"),
        _conn("J3", "3", "NFC_SDA"),
        _conn("J3", "4", "NFC_SCL"),
        _conn("J3", "5", "NFC_IRQ"),
        _conn("J3", "6", "NFC_RST"),
        _conn("R5", "1", "NFC_SDA"),
        _conn("R5", "2", "+3.3V"),
        _conn("R6", "1", "NFC_SCL"),
        _conn("R6", "2", "+3.3V"),
        _conn("U3", "VSS", "GND"),
        _conn("U3", "VDD", "+3.3V"),
        _conn("U3", "VO", "LCD_VO"),
        _conn("U3", "R/W", "GND"),
        _conn("U3", "RS", "LCD_RS"),
        _conn("U3", "E", "LCD_E"),
        _conn("U3", "DB4", "LCD_D4"),
        _conn("U3", "DB5", "LCD_D5"),
        _conn("U3", "DB6", "LCD_D6"),
        _conn("U3", "DB7", "LCD_D7"),
        _conn("U3", "A", "LCD_BL_A"),
        _conn("U3", "K", "GND"),
        _conn("RV1", "1", "GND"),
        _conn("RV1", "2", "LCD_VO"),
        _conn("RV1", "3", "+3.3V"),
        _conn("R3", "1", "+3.3V"),
        _conn("R3", "2", "LCD_BL_A"),
        _conn("#FLG01", "1", "+5V"),
        _conn("#FLG03", "1", "GND"),
        _conn("#FLG04", "1", "LCD_VO"),
    ]
    no_connects = [
        *[{"ref": "U2", "pin": pin} for pin in ("IO5", "IO15", "IO16", "IO17", "IO18", "IO3", "IO46", "IO21", "IO48", "IO45", "IO0", "IO35", "IO36", "IO37", "IO38", "IO39", "IO40", "IO41", "IO42", "IO2", "IO1")],
        *[{"ref": "U3", "pin": pin} for pin in ("DB0", "DB1", "DB2", "DB3")],
    ]
    return {
        "name": "card_reader_v1",
        "paper": "A3",
        "symbols": symbols,
        "connections": connections,
        "no_connects": no_connects,
        "expected_nets": sorted({connection["net"] for connection in connections}),
        "accepted_erc_types": [],
    }


def preview_build_from_spec(project_path: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe build preview without writing files."""
    spec = normalize_build_spec_v2(spec) if _is_v2_spec(spec) else spec
    paper = spec.get("paper", "A4")
    page_width, page_height = KiCadSchematic.PAPER_SIZES_MM.get(paper, KiCadSchematic.PAPER_SIZES_MM["A4"])
    symbol_errors = []
    footprint_errors = []
    normalization_errors = spec.get("normalization_errors", [])
    for symbol in spec.get("symbols", []):
        if not symbol.get("custom_symbol_node"):
            try:
                resolve_symbol(symbol["lib_id"])
            except Exception as exc:
                symbol_errors.append({"reference": symbol.get("reference"), "lib_id": symbol.get("lib_id"), "error": str(exc)})
        if symbol.get("footprint"):
            try:
                resolve_footprint(symbol["footprint"])
            except Exception as exc:
                footprint_errors.append({"reference": symbol.get("reference"), "footprint": symbol.get("footprint"), "error": str(exc)})
    return {
        "success": not normalization_errors and not symbol_errors and not footprint_errors,
        "project_path": project_path,
        "spec_name": spec.get("name"),
        "page": {"paper": paper, "width_mm": page_width, "height_mm": page_height},
        "planned_symbol_count": len(spec.get("symbols", [])),
        "planned_connection_count": len(spec.get("connections", [])),
        "planned_no_connect_count": len(spec.get("no_connects", [])),
        "planned_nets": sorted({connection["net"] for connection in spec.get("connections", [])}),
        "erc_sensitive_pins": _erc_sensitive_pins(spec),
        "normalization_errors": normalization_errors,
        "normalization_warnings": spec.get("normalization_warnings", []),
        "symbol_errors": symbol_errors,
        "footprint_errors": footprint_errors,
    }


def build_schematic_from_spec(
    project_path: str,
    spec: dict[str, Any],
    mode: str = "replace",
    run_erc: bool = True,
    *,
    allow_destructive_replace: bool = True,
    detail: str = "full",
    include_diff: bool = True,
    include_preview: bool = True,
    include_full_native_netlist: bool = True,
    run_quality_report: bool = True,
) -> dict[str, Any]:
    """Build a schematic from a structured spec."""
    spec = normalize_build_spec_v2(spec) if _is_v2_spec(spec) else spec
    if mode not in {"append", "update", "replace"}:
        return {
            "success": False,
            "project_path": project_path,
            "error": "mode must be one of: append, update, replace",
        }
    if spec.get("normalization_errors"):
        return {
            "success": False,
            "project_path": project_path,
            "error": "Spec contains schema errors",
            "normalization_errors": spec["normalization_errors"],
            "normalization_warnings": spec.get("normalization_warnings", []),
        }
    files = get_project_files(project_path)
    if "schematic" not in files:
        return {"success": False, "project_path": project_path, "error": "Schematic file not found"}
    schematic_path = files["schematic"]
    if (
        mode == "replace"
        and not allow_destructive_replace
        and _schematic_has_user_content(schematic_path)
    ):
        return {
            "success": False,
            "project_path": project_path,
            "schematic_path": schematic_path,
            "error": "mode='replace' would overwrite a non-empty schematic; pass allow_destructive_replace=True or use mode='update'",
            "recoverable": True,
            "recommended_next_tool": "schematic_build_from_spec_v2",
            "recommended_next_arguments": {"project_path": project_path, "mode": "update"},
        }
    preview = preview_build_from_spec(project_path, spec)
    if not preview["success"]:
        return {**preview, "success": False, "error": "Spec contains unresolved symbols or footprints"}
    built_summary: dict[str, Any] = {}

    def mutate(schematic: KiCadSchematic) -> dict[str, Any]:
        built = (
            _build_in_memory_schematic(schematic_path, spec)
            if mode == "replace"
            else _apply_spec_to_existing_schematic(schematic, schematic_path, spec, mode)
        )
        schematic.root = built.root
        built_summary.update(
            {
                "symbols": built.list_symbols(),
                "labels": built.list_labels(),
                "wires": built.list_wires(),
                "no_connects": built.list_no_connects(),
            }
        )
        return {
            "spec_name": spec.get("name"),
            "symbol_count": len(built_summary["symbols"]),
            "connection_count": len(spec.get("connections", [])),
            "no_connect_count": len(built_summary["no_connects"]),
        }

    from kicad_mcp.utils.transactional_edit import apply_transactional_schematic_edit

    result = apply_transactional_schematic_edit(
        schematic_path,
        mutate,
        run_cli_validation=True,
        post_write_validator=lambda path: validate_connection_plan_membership(path, spec.get("connections", [])),
    )
    if not result.get("success"):
        return result
    native = export_native_netlist(schematic_path)
    result["tool"] = (
        "schematic_build_from_spec_v2"
        if spec.get("source_format") == "v2"
        else "schematic_build_from_spec"
    )
    result["stage"] = "schematic_built"
    result["mode"] = mode
    result["changed"] = True
    result["warnings"] = spec.get("normalization_warnings", [])
    result["symbol_count"] = len(built_summary["symbols"])
    result["connection_count"] = len(spec.get("connections", []))
    result["no_connect_count"] = len(built_summary["no_connects"])
    result["native_netlist"] = native if include_full_native_netlist else _compact_native_netlist(native)
    if run_quality_report:
        quality = schematic_quality_report(schematic_path, run_erc=run_erc)
        result["quality_report"] = quality if detail == "full" else _compact_quality_report(quality)
    if include_preview:
        svg_result = export_schematic_svg_file(schematic_path, None)
        if svg_result.get("success"):
            result["schematic_preview"] = svg_preview_metadata(svg_result["svg_path"])
        else:
            result["schematic_preview_error"] = svg_result.get("error")
    if detail == "full":
        result["preview"] = preview
    if not include_diff:
        result.pop("diff", None)
    if detail == "compact":
        result.pop("validation", None)
    return result


def apply_connection_plan(
    schematic_path: str,
    connections: list[dict[str, Any]],
    no_connects: list[dict[str, Any]] | None = None,
    run_native_netlist: bool = True,
    rollback_on_failed_membership: bool = True,
    fail_on_erc_violations: bool = False,
) -> dict[str, Any]:
    """Apply a batch connection plan transactionally through the v2 intent engine."""
    # Preserve existing tests and callers that monkeypatch this module's native
    # netlist helper by rebinding the v2 engine at call time.
    import kicad_mcp.utils.schematic_intent as schematic_intent

    schematic_intent.export_native_netlist = export_native_netlist
    return apply_connection_plan_v2(
        schematic_path,
        connections,
        no_connects,
        verify_native_netlist=run_native_netlist,
        run_erc=True,
        auto_snap=True,
        rollback_on_failure=rollback_on_failed_membership,
        fail_on_erc_violations=fail_on_erc_violations,
    )


def preview_build_from_spec_v2(project_path: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Preview an agent-friendly v2 circuit specification."""
    return preview_build_from_spec(project_path, normalize_build_spec_v2(spec))


def build_schematic_from_spec_v2(
    project_path: str,
    spec: dict[str, Any],
    mode: str = "update",
    run_erc: bool = True,
    *,
    allow_destructive_replace: bool = False,
    detail: str = "compact",
    include_diff: bool = False,
    include_preview: bool = False,
    include_full_native_netlist: bool = False,
    run_quality_report: bool = False,
) -> dict[str, Any]:
    """Build a schematic from the v2 parts/nets/no_connects spec format."""
    return build_schematic_from_spec(
        project_path,
        normalize_build_spec_v2(spec),
        mode=mode,
        run_erc=run_erc,
        allow_destructive_replace=allow_destructive_replace,
        detail=detail,
        include_diff=include_diff,
        include_preview=include_preview,
        include_full_native_netlist=include_full_native_netlist,
        run_quality_report=run_quality_report,
    )


def cleanup_schematic_visuals(
    schematic_path: str,
    *,
    preserve_connectivity: bool = True,
    arrange_labels: bool = True,
    arrange_generated_parts: bool = True,
    run_quality_report: bool = True,
) -> dict[str, Any]:
    """Internal visual cleanup hook for future post-build readability passes."""
    quality = schematic_quality_report(schematic_path, run_erc=False) if run_quality_report else None
    return {
        "success": True,
        "schematic_path": schematic_path,
        "preserve_connectivity": preserve_connectivity,
        "arrange_labels": arrange_labels,
        "arrange_generated_parts": arrange_generated_parts,
        "changed": False,
        "quality_report": quality,
    }


def normalize_build_spec_v2(spec: dict[str, Any]) -> dict[str, Any]:
    """Translate the agent-friendly v2 spec format into the internal builder spec."""
    if not _is_v2_spec(spec):
        return spec
    parts = [*spec.get("parts", []), *spec.get("custom_parts", [])]
    layout_positions = _v2_layout_positions(spec)
    symbols = []
    errors = []
    warnings = []
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            errors.append({"path": f"parts[{index}]", "error": "part must be an object"})
            continue
        ref = part.get("ref") or part.get("reference")
        if not ref:
            errors.append({"path": f"parts[{index}]", "error": "part requires ref"})
            continue
        custom_pins = part.get("pins")
        lib_id = _normalize_v2_part_lib_id(part, f"parts[{index}]", errors, warnings)
        if not lib_id:
            continue
        x, y = layout_positions.get(str(ref), _default_v2_symbol_position(index))
        symbol: dict[str, Any] = {
            "reference": str(ref),
            "lib_id": lib_id,
            "value": str(part.get("value", ref)),
            "x": _snap(x),
            "y": _snap(y),
            "angle": float(part.get("angle", 0.0)),
            "footprint": part.get("footprint"),
            "properties": part.get("properties"),
        }
        if custom_pins is not None:
            symbol["custom_symbol_node"] = _custom_symbol_node(
                lib_id,
                str(part.get("value", ref)),
                part.get("footprint"),
                custom_pins,
                f"parts[{index}]",
                errors,
            )
        if symbol.get("custom_symbol_node") is None and custom_pins is not None:
            continue
        symbols.append(symbol)
    connections = []
    nets = spec.get("nets", {})
    if not isinstance(nets, dict):
        errors.append({"path": "nets", "error": "nets must be an object keyed by net name"})
        nets = {}
    for net_name, pins in nets.items():
        if not isinstance(pins, list):
            errors.append({"path": f"nets.{net_name}", "error": "net entries must be a list"})
            continue
        for item_index, item in enumerate(pins):
            endpoint = _normalize_v2_endpoint(
                item,
                f"nets.{net_name}[{item_index}]",
                errors,
                warnings,
            )
            if endpoint:
                connection = {
                    "type": "pin_to_net",
                    "ref": endpoint["ref"],
                    "pin": endpoint["pin"],
                    "net": str(net_name),
                    "allow_hidden_power": endpoint.get("allow_hidden_power", False),
                }
                layout_hints = spec.get("layout_hints", {})
                if isinstance(layout_hints, dict):
                    power_symbol_net = _known_power_symbol_net(str(net_name))
                    rail_like_net = str(net_name).startswith("+")
                    if layout_hints.get("label_strategy") and not rail_like_net and not power_symbol_net:
                        connection["label_placement"] = layout_hints["label_strategy"]
                    if layout_hints.get("connection_style") and (not rail_like_net or power_symbol_net):
                        connection["connection_style"] = layout_hints["connection_style"]
                    if layout_hints.get("label_clearance_mm") and not rail_like_net and not power_symbol_net:
                        connection["label_clearance_mm"] = layout_hints["label_clearance_mm"]
                connections.append(connection)
    no_connects = []
    for index, item in enumerate(spec.get("no_connects", [])):
        endpoint = _normalize_v2_endpoint(
            item,
            f"no_connects[{index}]",
            errors,
            warnings,
        )
        if endpoint:
            no_connects.append(endpoint)
    normalized_connections = normalize_connections(connections)
    errors.extend(normalized_connections["failed_connections"])
    return {
        "name": spec.get("name"),
        "paper": spec.get("paper", "A4"),
        "symbols": symbols,
        "connections": normalized_connections["connections"],
        "no_connects": no_connects,
        "expected_nets": sorted(nets.keys()),
        "accepted_erc_types": spec.get("accepted_erc_types", []),
        "layout_hints": spec.get("layout_hints", {}),
        "source_format": "v2",
        "normalization_errors": errors,
        "normalization_warnings": warnings,
    }


def _normalize_v2_part_lib_id(
    part: dict[str, Any],
    path: str,
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> str | None:
    lib_id = part.get("lib_id") or part.get("kicad_symbol") or part.get("kicad_symbol_id")
    symbol = part.get("symbol")
    if lib_id:
        if symbol and ":" not in str(symbol):
            warnings.append(
                {
                    "path": f"{path}.symbol",
                    "warning": "ignored symbol-unit-looking value because lib_id was provided",
                    "value": str(symbol),
                }
            )
        return str(lib_id)
    if symbol and ":" in str(symbol):
        return str(symbol)
    if part.get("pins") is not None:
        custom_name = str(part.get("value") or part.get("ref") or "CustomPart")
        return str(part.get("custom_lib_id") or f"Custom:{custom_name}")
    errors.append(
        {
            "path": path,
            "error": "part requires lib_id or symbol as a full KiCad library ID like 'Device:R'; unit names like 'R_1_1' are not valid",
            "symbol": symbol,
        }
    )
    return None


def _normalize_v2_endpoint(
    item: Any,
    path: str,
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> dict[str, Any] | None:
    allow_hidden = False
    if isinstance(item, dict):
        ref = item.get("ref") or item.get("reference")
        pin = item.get("pin") or item.get("pin_number") or item.get("pin_name")
        allow_hidden = bool(item.get("allow_hidden_power", False))
    elif isinstance(item, str):
        if "_" not in item:
            errors.append(
                {
                    "path": path,
                    "error": "string pin shorthand must be 'REF_PIN'; prefer ['REF', 'PIN'] or {'ref':'REF','pin':'PIN'}",
                    "value": item,
                }
            )
            return None
        ref, pin = item.rsplit("_", 1)
        warnings.append(
            {
                "path": path,
                "warning": "string pin shorthand is accepted but list/object endpoint format is preferred",
                "value": item,
            }
        )
    elif isinstance(item, list | tuple):
        ref = item[0] if len(item) > 0 else None
        pin = item[1] if len(item) > 1 else None
    else:
        errors.append({"path": path, "error": "endpoint must be object, [ref, pin], or 'REF_PIN'"})
        return None
    if not ref or not pin:
        errors.append({"path": path, "error": "endpoint requires ref and pin", "value": item})
        return None
    return {"ref": str(ref), "pin": str(pin), "allow_hidden_power": allow_hidden}


def _custom_symbol_node(
    lib_id: str,
    value: str,
    footprint: str | None,
    pins: Any,
    path: str,
    errors: list[dict[str, Any]],
) -> SExprList | None:
    if not isinstance(pins, list) or not pins:
        errors.append({"path": f"{path}.pins", "error": "custom part pins must be a non-empty list"})
        return None
    symbol_name = lib_id.split(":", 1)[-1]
    node = SExprList(
        [
            SExprAtom("symbol"),
            SExprAtom(lib_id, quoted=True),
            SExprList([SExprAtom("in_bom"), SExprAtom("yes")]),
            SExprList([SExprAtom("on_board"), SExprAtom("yes")]),
            _library_property("Reference", "U", 0.0, -5.08),
            _library_property("Value", value, 0.0, 5.08),
            _library_property("Footprint", footprint or "", 0.0, 7.62),
            SExprList([SExprAtom("symbol"), SExprAtom(f"{symbol_name}_0_1", quoted=True)]),
        ]
    )
    body = node.child_lists("symbol")[-1]
    for index, pin in enumerate(pins):
        if not isinstance(pin, dict):
            errors.append({"path": f"{path}.pins[{index}]", "error": "pin must be an object"})
            continue
        number = str(pin.get("number") or pin.get("pin") or "")
        name = str(pin.get("name") or number)
        if not number:
            errors.append({"path": f"{path}.pins[{index}]", "error": "pin requires number"})
            continue
        body.items.append(
            _library_pin(
                number,
                name,
                str(pin.get("type") or pin.get("pintype") or "passive"),
                index,
                len(pins),
                bool(pin.get("hidden", False)),
            )
        )
    if errors and any(str(error.get("path", "")).startswith(f"{path}.pins") for error in errors):
        return None
    return node


def _library_property(name: str, value: str, x: float, y: float) -> SExprList:
    return SExprList(
        [
            SExprAtom("property"),
            SExprAtom(name, quoted=True),
            SExprAtom(value, quoted=True),
            SExprList([SExprAtom("at"), SExprAtom(str(x)), SExprAtom(str(y)), SExprAtom("0")]),
        ]
    )


def _library_pin(
    number: str,
    name: str,
    pin_type: str,
    index: int,
    total: int,
    hidden: bool,
) -> SExprList:
    left_side = index < math.ceil(total / 2)
    local_index = index if left_side else index - math.ceil(total / 2)
    x = -7.62 if left_side else 7.62
    pin_pitch = 7.62 if total >= 8 else 5.08
    y = _snap((local_index - max(total / 4, 1)) * -pin_pitch)
    angle = 180 if left_side else 0
    items: list[Any] = [
        SExprAtom("pin"),
        SExprAtom(_normalize_pin_type(pin_type)),
        SExprAtom("line"),
        SExprList([SExprAtom("at"), SExprAtom(str(x)), SExprAtom(str(y)), SExprAtom(str(angle))]),
        SExprList([SExprAtom("length"), SExprAtom("2.54")]),
        SExprList([SExprAtom("name"), SExprAtom(name, quoted=True)]),
        SExprList([SExprAtom("number"), SExprAtom(number, quoted=True)]),
    ]
    if hidden:
        items.append(SExprAtom("hide"))
    return SExprList(items)


def _normalize_pin_type(pin_type: str) -> str:
    normalized = str(pin_type).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "bidirectional": "bidirectional",
        "bi_directional": "bidirectional",
        "input": "input",
        "output": "output",
        "passive": "passive",
        "power_in": "power_in",
        "power_input": "power_in",
        "power_out": "power_out",
        "power_output": "power_out",
        "open_collector": "open_collector",
        "open_emitter": "open_emitter",
        "no_connect": "no_connect",
        "tri_state": "tri_state",
        "unspecified": "unspecified",
    }
    return aliases.get(normalized, "passive")


def _known_power_symbol_net(net_name: str) -> bool:
    return net_name.upper() in {
        "GND",
        "AGND",
        "DGND",
        "+3V3",
        "+3.3V",
        "+5V",
        "VBUS",
        "VCC",
        "VDD",
    }


def add_no_connect_marker(
    schematic_path: str, reference: str, pin: str, allow_hidden_power: bool = False
) -> dict[str, Any]:
    """Add a no-connect marker to a resolved symbol pin."""
    from kicad_mcp.utils.transactional_edit import apply_transactional_schematic_edit

    return apply_transactional_schematic_edit(
        schematic_path,
        lambda schematic: {
            "no_connect": add_no_connect_to_pin(
                schematic, schematic_path, reference, pin, allow_hidden_power
            )
        },
        run_cli_validation=True,
    )


def validate_connection_plan_membership(
    schematic_path: str,
    connections: list[dict[str, Any]],
    rollback_on_failed_membership: bool = True,
) -> dict[str, Any]:
    """Validate that every planned connection appears in KiCad's native netlist."""
    native = export_native_netlist(schematic_path)
    if not native.get("success"):
        return {"success": False, "reason": native.get("error"), "native_netlist": native}
    missing = []
    optional_missing = []
    checked_count = 0
    for connection in connections:
        if str(connection["ref"]).startswith("#"):
            continue
        checked_count += 1
        check = _membership_from_native(
            native, connection["ref"], connection["pin"], connection["net"]
        )
        if not check:
            if connection.get("required", True):
                missing.append(connection)
            else:
                optional_missing.append(connection)
    success = not missing or not rollback_on_failed_membership
    return {
        "success": success,
        "reason": (
            "all required planned connections verified"
            if not missing
            else "missing required native netlist memberships"
        ),
        "missing": missing,
        "optional_missing": optional_missing,
        "checked_count": checked_count,
        "required_checked_count": sum(
            1
            for connection in connections
            if connection.get("required", True) and not str(connection["ref"]).startswith("#")
        ),
        "native_netlist": {
            "success": native.get("success"),
            "component_count": native.get("component_count"),
            "net_count": native.get("net_count"),
            "connectivity_complete": native.get("connectivity_complete"),
        },
    }


def validate_connection_plan_sanity(connections: list[dict[str, Any]]) -> dict[str, Any]:
    """Catch obvious pin/net intent mistakes before editing the schematic."""
    mismatches = []
    malformed = []
    seen: dict[tuple[str, str], str] = {}
    conflicts = []
    for index, connection in enumerate(connections):
        ref = connection.get("ref")
        pin = connection.get("pin")
        net = connection.get("net")
        if not ref or not pin or not net:
            malformed.append({"index": index, "connection": connection})
            continue
        key = (str(ref), str(pin))
        if key in seen and seen[key] != str(net):
            conflicts.append(
                {
                    "ref": ref,
                    "pin": pin,
                    "first_net": seen[key],
                    "second_net": net,
                }
            )
        seen[key] = str(net)
        mismatch = _power_ground_mismatch(str(ref), str(pin), str(net))
        if mismatch is not None and not connection.get("allow_power_ground_mismatch", False):
            mismatches.append({**mismatch, "connection": connection})
    return {
        "success": not malformed and not conflicts and not mismatches,
        "malformed": malformed,
        "conflicts": conflicts,
        "power_ground_mismatches": mismatches,
    }


def schematic_quality_report(project_or_schematic_path: str, run_erc: bool = True) -> dict[str, Any]:
    """Summarize schematic quality and common agent-authoring failure modes."""
    schematic_path = _schematic_path(project_or_schematic_path)
    schematic = KiCadSchematic.from_file(schematic_path)
    paper = _paper(schematic)
    width, height = KiCadSchematic.PAPER_SIZES_MM.get(paper, KiCadSchematic.PAPER_SIZES_MM["A4"])
    missing_footprints = [
        symbol["reference"]
        for symbol in schematic.list_symbols()
        if not str(symbol.get("reference", "")).startswith("#") and not symbol.get("footprint")
    ]
    off_grid = _off_grid_items(schematic)
    outside = [
        symbol["reference"]
        for symbol in schematic.list_symbols()
        if symbol["position"]["x"] < 0
        or symbol["position"]["y"] < 0
        or symbol["position"]["x"] > width
        or symbol["position"]["y"] > height
    ]
    erc = run_erc_via_cli(schematic_path) if run_erc else {"success": True, "skipped": True}
    categories = erc.get("violation_categories", {}) if erc.get("success") else {}
    native = export_native_netlist(schematic_path)
    dangling_labels = _dangling_labels(schematic, schematic_path)
    isolated_labels = _isolated_labels(schematic, native)
    power_ground_mismatches = _native_power_ground_mismatches(native)
    visual_quality = _visual_quality(schematic, schematic_path, native)
    return {
        "success": True,
        "schematic_path": schematic_path,
        "page": {"paper": paper, "width_mm": width, "height_mm": height},
        "symbol_count": len(schematic.list_symbols()),
        "wire_count": len(schematic.list_wires()),
        "label_count": len(schematic.list_labels()),
        "no_connect_count": len(schematic.list_no_connects()),
        "missing_footprints": missing_footprints,
        "missing_footprint_count": len(missing_footprints),
        "symbols_outside_page": outside,
        "outside_page_count": len(outside),
        "off_grid_items": off_grid,
        "off_grid_count": len(off_grid),
        "dangling_labels": dangling_labels,
        "dangling_label_count": len(dangling_labels),
        "isolated_labels": isolated_labels,
        "isolated_label_count": len(isolated_labels),
        "power_ground_mismatches": power_ground_mismatches,
        "power_ground_mismatch_count": len(power_ground_mismatches),
        "visual_quality": visual_quality,
        "quality_gate": {
            "passed": not off_grid
            and not dangling_labels
            and not isolated_labels
            and not power_ground_mismatches
            and not missing_footprints
            and not outside
            and visual_quality["blocking_count"] == 0,
            "blocking_counts": {
                "off_grid": len(off_grid),
                "dangling_labels": len(dangling_labels),
                "isolated_labels": len(isolated_labels),
                "power_ground_mismatches": len(power_ground_mismatches),
                "missing_footprints": len(missing_footprints),
                "outside_page": len(outside),
                "visual_quality": visual_quality["blocking_count"],
            },
        },
        "erc": {
            "success": erc.get("success"),
            "total_violations": erc.get("total_violations"),
            "violation_categories": categories,
            "unacceptable_categories": {
                key: categories[key] for key in sorted(UNACCEPTABLE_ERC_TYPES.intersection(categories))
            },
            "error": erc.get("error"),
        },
        "native_netlist": {
            "success": native.get("success"),
            "component_count": native.get("component_count"),
            "net_count": native.get("net_count"),
            "non_empty_nets": sum(
                1 for net in native.get("nets", {}).values() if net.get("nodes")
            )
            if native.get("success")
            else 0,
            "error": native.get("error"),
        },
    }


def _visual_quality(
    schematic: KiCadSchematic, schematic_path: str, native: dict[str, Any]
) -> dict[str, Any]:
    wires = schematic.list_wires()
    labels = schematic.list_labels()
    pin_points = _schematic_pin_points(schematic, schematic_path)
    tiny_stubs = _tiny_stubs(wires)
    duplicate_labels = _duplicate_nearby_labels(labels)
    label_overlaps = _overlapping_labels(labels)
    symbol_overlaps = _symbol_overlaps(schematic, schematic_path)
    labels_inside_symbols = _labels_inside_symbols(schematic, schematic_path)
    unreadable_labels = _unreadable_label_orientations(labels)
    dangling_labels = _dangling_labels(schematic, schematic_path)
    short_wires = [
        wire for wire in wires if _wire_length(wire) < 1.0 and wire not in tiny_stubs
    ]
    floating_wires = [
        wire for wire in wires if not _wire_touches_pin_or_label(wire, pin_points, labels)
    ]
    warnings = []
    blocking = []
    for ref, count in _tiny_stubs_by_symbol(tiny_stubs, schematic, schematic_path).items():
        if count > 4:
            warnings.append({"type": "many_tiny_stubs", "reference": ref, "count": count})
    for item in duplicate_labels:
        warnings.append({"type": "duplicate_label_near_pin", **item})
    for item in label_overlaps:
        warnings.append({"type": "label_overlap", **item})
    for item in labels_inside_symbols:
        blocking.append({"type": "label_inside_symbol", **item})
    for item in symbol_overlaps:
        blocking.append({"type": "symbol_overlap", **item})
    for item in unreadable_labels:
        warnings.append({"type": "unreadable_label_orientation", **item})
    for label in dangling_labels:
        blocking.append({"type": "label_not_attached", "label": label})
    for wire in short_wires:
        warnings.append({"type": "unusually_short_wire", "wire": wire})
    power_symbol_count = sum(
        1 for symbol in schematic.list_symbols() if str(symbol.get("lib_id", "")).startswith("power:")
    )
    ground_symbol_count = sum(
        1
        for symbol in schematic.list_symbols()
        if str(symbol.get("value", "")).upper() in {"GND", "VSS", "GNDA", "DGND"}
    )
    penalty = (
        len(tiny_stubs) * 1.5
        + len(duplicate_labels) * 5
        + len(label_overlaps) * 4
        + len(labels_inside_symbols) * 8
        + len(symbol_overlaps) * 20
        + len(unreadable_labels) * 3
        + len(short_wires) * 2
        + len(floating_wires) * 4
        + len(blocking) * 25
    )
    score = max(0.0, round(100.0 - penalty, 2))
    return {
        "tiny_stub_count": len(tiny_stubs),
        "overlapping_label_count": len(duplicate_labels),
        "duplicate_label_count": len(duplicate_labels),
        "label_overlap_count": len(label_overlaps),
        "symbol_overlap_count": len(symbol_overlaps),
        "label_inside_symbol_count": len(labels_inside_symbols),
        "long_wire_count": len(_long_wires(wires)),
        "crossing_wire_count": 0,
        "unreadable_label_orientation_count": len(unreadable_labels),
        "unusually_short_wire_count": len(short_wires),
        "floating_wire_count": len(floating_wires),
        "power_symbol_count": power_symbol_count,
        "ground_symbol_count": ground_symbol_count,
        "readability_score": score,
        "blocking_count": len(blocking),
        "warning_count": len(warnings),
        "warnings": warnings,
        "blocking": blocking,
        "native_netlist_available": bool(native.get("success")),
    }


def _build_in_memory_schematic(schematic_path: str, spec: dict[str, Any]) -> KiCadSchematic:
    schematic = KiCadSchematic.empty(paper=spec.get("paper", "A4"))
    for symbol in spec.get("symbols", []):
        _add_spec_symbol(schematic, symbol)
    for connection in spec.get("connections", []):
        attach_net_to_pin(
            schematic,
            schematic_path,
            connection["ref"],
            connection["pin"],
            connection["net"],
            connection.get("label_type", "global"),
            connection.get("stub_length_mm", 5.08),
            connection.get("allow_hidden_power", False),
            label_placement=connection.get("label_placement", "pin_anchor"),
            label_clearance_mm=connection.get("label_clearance_mm", 5.08),
            connection_style=connection.get("connection_style", "label"),
        )
    for marker in spec.get("no_connects", []):
        add_no_connect_to_pin(
            schematic,
            schematic_path,
            marker["ref"],
            marker["pin"],
            marker.get("allow_hidden_power", False),
        )
    return schematic


def _apply_spec_to_existing_schematic(
    schematic: KiCadSchematic,
    schematic_path: str,
    spec: dict[str, Any],
    mode: str,
) -> KiCadSchematic:
    existing_refs = {symbol["reference"] for symbol in schematic.list_symbols()}
    for symbol in spec.get("symbols", []):
        reference = symbol["reference"]
        if reference in existing_refs:
            if mode == "append":
                raise ValueError(f"Symbol reference already exists: {reference}")
            continue
        _add_spec_symbol(schematic, symbol)
        existing_refs.add(reference)
    for connection in spec.get("connections", []):
        if _pin_has_label(
            schematic,
            schematic_path,
            connection["ref"],
            connection["pin"],
            connection["net"],
        ):
            continue
        attach_net_to_pin(
            schematic,
            schematic_path,
            connection["ref"],
            connection["pin"],
            connection["net"],
            connection.get("label_type", "global"),
            connection.get("stub_length_mm", 5.08),
            connection.get("allow_hidden_power", False),
            label_placement=connection.get("label_placement", "pin_anchor"),
            label_clearance_mm=connection.get("label_clearance_mm", 5.08),
            connection_style=connection.get("connection_style", "label"),
        )
    for marker in spec.get("no_connects", []):
        if _pin_has_no_connect(schematic, schematic_path, marker["ref"], marker["pin"]):
            continue
        add_no_connect_to_pin(
            schematic,
            schematic_path,
            marker["ref"],
            marker["pin"],
            marker.get("allow_hidden_power", False),
        )
    return schematic


def _add_spec_symbol(schematic: KiCadSchematic, symbol: dict[str, Any]) -> None:
    if symbol.get("custom_symbol_node") is not None:
        lib_id = symbol["lib_id"]
        symbol_node = symbol["custom_symbol_node"]
    else:
        resolved_chain = _resolve_symbol_embed_chain(symbol["lib_id"])
        for parent_lib_id, parent_node in resolved_chain[:-1]:
            schematic.embed_lib_symbol(parent_lib_id, cast(Any, parent_node))
        lib_id, symbol_node = resolved_chain[-1]
    schematic.add_symbol(
        lib_id,
        symbol["reference"],
        symbol.get("value", symbol["reference"]),
        symbol["x"],
        symbol["y"],
        symbol.get("angle", 0.0),
        symbol.get("footprint"),
        symbol.get("properties"),
        cast(Any, symbol_node),
    )


def _pin_has_label(
    schematic: KiCadSchematic,
    schematic_path: str,
    reference: str,
    pin: str,
    net_name: str,
) -> bool:
    point = _pin_connection_point(schematic, schematic_path, reference, pin)
    if point is None:
        return False
    return any(
        label.get("text") == net_name
        and label.get("position", {}).get("x") == point["x"]
        and label.get("position", {}).get("y") == point["y"]
        for label in schematic.list_labels()
    )


def _pin_has_no_connect(
    schematic: KiCadSchematic,
    schematic_path: str,
    reference: str,
    pin: str,
) -> bool:
    point = _pin_connection_point(schematic, schematic_path, reference, pin)
    if point is None:
        return False
    return any(
        marker.get("position", {}).get("x") == point["x"]
        and marker.get("position", {}).get("y") == point["y"]
        for marker in schematic.list_no_connects()
    )


def _pin_connection_point(
    schematic: KiCadSchematic,
    schematic_path: str,
    reference: str,
    pin: str,
) -> dict[str, float] | None:
    pin_map = get_symbol_pin_map_from_schematic(schematic, schematic_path, reference)
    if not pin_map.get("success"):
        return None
    matches = [
        item
        for item in pin_map["pins"]
        if item["number"] == pin or item["name"] == pin or item["pinfunction"] == pin
    ]
    if len(matches) != 1:
        return None
    return cast(dict[str, float], matches[0]["connection_point"])


def _is_v2_spec(spec: dict[str, Any]) -> bool:
    return "parts" in spec or "custom_parts" in spec or "nets" in spec


def _v2_layout_positions(spec: dict[str, Any]) -> dict[str, tuple[float, float]]:
    positions: dict[str, tuple[float, float]] = {}
    parts = [*spec.get("parts", []), *spec.get("custom_parts", [])]
    for part in parts:
        if not isinstance(part, dict):
            continue
        ref = part.get("ref") or part.get("reference")
        if ref is None or part.get("x") is None or part.get("y") is None:
            continue
        try:
            positions[str(ref)] = (float(part["x"]), float(part["y"]))
        except (TypeError, ValueError):
            continue
    part_index = {str(part.get("ref")): index for index, part in enumerate(parts)}
    blocks = spec.get("layout_hints", {}).get("functional_blocks", [])
    if not isinstance(blocks, list) or not blocks:
        return positions
    for block_index, block in enumerate(blocks):
        parts = block.get("parts", []) if isinstance(block, dict) else []
        for local_index, ref in enumerate(parts):
            if str(ref) not in part_index:
                continue
            positions.setdefault(str(ref), (
                35.56 + block_index * 63.5,
                38.1 + local_index * 17.78,
            ))
    return positions


def _default_v2_symbol_position(index: int) -> tuple[float, float]:
    columns = 4
    return 35.56 + (index % columns) * 50.8, 38.1 + (index // columns) * 25.4


def _resolve_symbol_embed_chain(lib_id: str) -> list[tuple[str, Any]]:
    """Return parent lib symbols followed by the requested symbol."""
    resolved = resolve_symbol(lib_id)
    parent = _symbol_extends(resolved["node"])
    if parent and not _node_has_pin(resolved["node"]):
        parent_node = _nearest_parent_with_pins(resolved["library"], parent)
        if parent_node is not None:
            flattened = deepcopy(parent_node)
            _rename_embedded_symbol(flattened, lib_id)
            return [(lib_id, flattened)]
    chain: list[tuple[str, Any]] = [(lib_id, resolved["node"])]
    library = resolved["library"]
    seen = {lib_id}
    while parent:
        parent_lib_id = f"{library}:{parent}"
        if parent_lib_id in seen:
            break
        seen.add(parent_lib_id)
        parent_resolved = resolve_symbol(parent_lib_id)
        chain.insert(0, (parent_lib_id, parent_resolved["node"]))
        parent = _symbol_extends(parent_resolved["node"])
    return chain


def _nearest_parent_with_pins(library: str, parent: str) -> Any | None:
    seen: set[str] = set()
    while parent:
        parent_lib_id = f"{library}:{parent}"
        if parent_lib_id in seen:
            return None
        seen.add(parent_lib_id)
        resolved = resolve_symbol(parent_lib_id)
        if _node_has_pin(resolved["node"]):
            return resolved["node"]
        parent = _symbol_extends(resolved["node"]) or ""
    return None


def _node_has_pin(node: Any) -> bool:
    if hasattr(node, "head") and node.head() == "pin":
        return True
    if not hasattr(node, "child_lists"):
        return False
    return any(_node_has_pin(child) for child in node.child_lists())


def _rename_embedded_symbol(node: Any, lib_id: str) -> None:
    if not hasattr(node, "head") or node.head() != "symbol" or len(node.items) < 2:
        return
    old_name = node.items[1].value if hasattr(node.items[1], "value") else ""
    old_part = old_name.split(":", 1)[-1]
    new_part = lib_id.split(":", 1)[-1]
    node.items[1] = SExprAtom(lib_id, quoted=True)
    for child in node.child_lists():
        if child.head() == "symbol" and len(child.items) >= 2 and hasattr(child.items[1], "value"):
            child_name = child.items[1].value
            if child_name.startswith(old_part):
                child.items[1] = SExprAtom(
                    f"{new_part}{child_name[len(old_part):]}", quoted=True
                )


def _symbol_extends(node: Any) -> str | None:
    child = node.first_child("extends") if hasattr(node, "first_child") else None
    if child is None or len(child.items) < 2:
        return None
    value = child.items[1]
    return value.value if hasattr(value, "value") else None


def _membership_from_native(native: dict[str, Any], reference: str, pin: str, net_name: str) -> bool:
    for candidate_name in (net_name, f"/{net_name}"):
        net = native.get("nets", {}).get(candidate_name)
        if not net:
            continue
        for node in net.get("nodes", []):
            pinfunction = node.get("pinfunction", "")
            if node.get("ref") == reference and (
                node.get("pin") == pin or pinfunction == pin or pinfunction.startswith(f"{pin}_")
            ):
                return True
    return False


def _erc_sensitive_pins(spec: dict[str, Any]) -> list[dict[str, str]]:
    sensitive = []
    for connection in spec.get("connections", []):
        if connection["net"] in {"+5V", "+3.3V", "GND"}:
            sensitive.append({"ref": connection["ref"], "pin": connection["pin"], "net": connection["net"]})
    return sensitive


def _compact_native_netlist(native: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": native.get("success"),
        "component_count": native.get("component_count"),
        "net_count": native.get("net_count"),
        "connectivity_complete": native.get("connectivity_complete"),
        "error": native.get("error"),
    }


def _compact_quality_report(quality: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": quality.get("success"),
        "symbol_count": quality.get("symbol_count"),
        "label_count": quality.get("label_count"),
        "wire_count": quality.get("wire_count"),
        "quality_gate": quality.get("quality_gate"),
        "erc": quality.get("erc"),
        "native_netlist": quality.get("native_netlist"),
    }


def _schematic_has_user_content(schematic_path: str) -> bool:
    path = Path(schematic_path)
    if not path.exists():
        return False
    schematic = KiCadSchematic.from_file(str(path))
    return bool(
        schematic.list_symbols()
        or schematic.list_labels()
        or schematic.list_wires()
        or schematic.list_no_connects()
    )


def _schematic_path(project_or_schematic_path: str) -> str:
    if project_or_schematic_path.endswith(".kicad_sch"):
        return project_or_schematic_path
    files = get_project_files(project_or_schematic_path)
    if "schematic" not in files:
        raise FileNotFoundError("Schematic file not found")
    return files["schematic"]


def _paper(schematic: KiCadSchematic) -> str:
    paper = schematic.root.first_child("paper")
    if paper is not None and len(paper.items) >= 2:
        atom = paper.items[1]
        return atom.value if hasattr(atom, "value") else "A4"
    return "A4"


def _off_grid_items(schematic: KiCadSchematic) -> list[dict[str, Any]]:
    items = []
    for symbol in schematic.list_symbols():
        if not _on_grid(symbol["position"]["x"]) or not _on_grid(symbol["position"]["y"]):
            items.append({"type": "symbol", "id": symbol["reference"], "position": symbol["position"]})
    for label in schematic.list_labels():
        if not _on_grid(label["position"]["x"]) or not _on_grid(label["position"]["y"]):
            items.append({"type": "label", "id": label.get("uuid"), "position": label["position"]})
    for wire in schematic.list_wires():
        for point in wire["points"]:
            if not _on_grid(point["x"]) or not _on_grid(point["y"]):
                items.append({"type": "wire", "id": wire.get("uuid"), "position": point})
    for marker in schematic.list_no_connects():
        if not _on_grid(marker["position"]["x"]) or not _on_grid(marker["position"]["y"]):
            items.append({"type": "no_connect", "id": marker.get("uuid"), "position": marker["position"]})
    return items


def _dangling_labels(schematic: KiCadSchematic, schematic_path: str) -> list[dict[str, Any]]:
    pin_points = _schematic_pin_points(schematic, schematic_path)
    dangling = []
    for label in schematic.list_labels():
        position = label["position"]
        point = (position["x"], position["y"])
        touches_pin = point in pin_points
        touches_wire = bool(schematic.find_wires_touching_point(position["x"], position["y"]))
        if not touches_pin and not touches_wire:
            dangling.append(
                {
                    "text": label["text"],
                    "type": label["type"],
                    "uuid": label.get("uuid"),
                    "position": position,
                }
            )
    return dangling


def _tiny_stubs(wires: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [wire for wire in wires if 0.0 < _wire_length(wire) < 2.54]


def _wire_length(wire: dict[str, Any]) -> float:
    length = 0.0
    points = wire.get("points", [])
    for start, end in zip(points, points[1:]):
        length += math.dist((start["x"], start["y"]), (end["x"], end["y"]))
    return length


def _duplicate_nearby_labels(labels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    duplicates = []
    for index, first in enumerate(labels):
        for second in labels[index + 1:]:
            if first.get("text") != second.get("text"):
                continue
            first_pos = first.get("position", {})
            second_pos = second.get("position", {})
            if math.dist(
                (first_pos.get("x", 0.0), first_pos.get("y", 0.0)),
                (second_pos.get("x", 0.0), second_pos.get("y", 0.0)),
            ) <= 1.0:
                duplicates.append(
                    {
                        "text": first.get("text"),
                        "first_uuid": first.get("uuid"),
                        "second_uuid": second.get("uuid"),
                        "position": first_pos,
                    }
                )
    return duplicates


def _wire_touches_pin_or_label(
    wire: dict[str, Any],
    pin_points: set[tuple[float, float]],
    labels: list[dict[str, Any]],
) -> bool:
    label_points = {
        (label["position"]["x"], label["position"]["y"])
        for label in labels
        if "position" in label and "x" in label["position"] and "y" in label["position"]
    }
    for point in wire.get("points", []):
        xy = (point["x"], point["y"])
        if xy in pin_points or xy in label_points:
            return True
    return False


def _symbol_overlaps(schematic: KiCadSchematic, schematic_path: str) -> list[dict[str, Any]]:
    symbols = [
        symbol
        for symbol in schematic.list_symbols()
        if not str(symbol.get("reference", "")).startswith("#")
    ]
    boxes = [
        (symbol["reference"], _approx_symbol_rect(schematic, schematic_path, symbol))
        for symbol in symbols
    ]
    overlaps = []
    for index, (first_ref, first_box) in enumerate(boxes):
        for second_ref, second_box in boxes[index + 1 :]:
            if _rects_intersect(first_box, second_box, padding=0.0):
                overlaps.append(
                    {
                        "first_reference": first_ref,
                        "second_reference": second_ref,
                        "first_box": _rect_to_dict(first_box),
                        "second_box": _rect_to_dict(second_box),
                    }
                )
    return overlaps


def _labels_inside_symbols(
    schematic: KiCadSchematic, schematic_path: str
) -> list[dict[str, Any]]:
    symbol_boxes = [
        (
            symbol["reference"],
            _approx_symbol_rect(schematic, schematic_path, symbol),
        )
        for symbol in schematic.list_symbols()
        if not str(symbol.get("reference", "")).startswith("#")
    ]
    inside = []
    for label in schematic.list_labels():
        if _is_rail_like_label(str(label.get("text") or "")):
            continue
        pos = label.get("position", {})
        point = (float(pos.get("x", 0.0)), float(pos.get("y", 0.0)))
        for ref, box in symbol_boxes:
            if ref[:1] in {"R", "C", "L", "D"}:
                continue
            if _point_in_rect(point, box):
                inside.append(
                    {
                        "label": label,
                        "reference": ref,
                        "symbol_box": _rect_to_dict(box),
                    }
                )
                break
    return inside


def _overlapping_labels(labels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    boxes = [(label, _label_rect(label)) for label in labels]
    overlaps = []
    for index, (first, first_box) in enumerate(boxes):
        for second, second_box in boxes[index + 1 :]:
            if _rects_intersect(first_box, second_box, padding=0.5):
                overlaps.append(
                    {
                        "first_text": first.get("text"),
                        "second_text": second.get("text"),
                        "first_uuid": first.get("uuid"),
                        "second_uuid": second.get("uuid"),
                    }
                )
    return overlaps


def _unreadable_label_orientations(labels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unreadable = []
    for label in labels:
        angle = float(label.get("position", {}).get("angle", 0.0)) % 360
        if angle not in {0.0, 90.0, 180.0, 270.0}:
            unreadable.append({"label": label, "angle": angle})
    return unreadable


def _long_wires(wires: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [wire for wire in wires if _wire_length(wire) > 50.8]


def _approx_symbol_rect(
    schematic: KiCadSchematic,
    schematic_path: str,
    symbol: dict[str, Any],
) -> tuple[float, float, float, float]:
    pin_map = get_symbol_pin_map_from_schematic(schematic, schematic_path, symbol["reference"])
    if pin_map.get("success") and pin_map.get("pins"):
        xs = [pin["connection_point"]["x"] for pin in pin_map["pins"]]
        ys = [pin["connection_point"]["y"] for pin in pin_map["pins"]]
        return (
            min(xs) - 3.81,
            min(ys) - 3.81,
            max(xs) + 3.81,
            max(ys) + 3.81,
        )
    pos = symbol["position"]
    return (
        pos["x"] - 7.62,
        pos["y"] - 7.62,
        pos["x"] + 7.62,
        pos["y"] + 7.62,
    )


def _label_rect(label: dict[str, Any]) -> tuple[float, float, float, float]:
    pos = label.get("position", {})
    x = float(pos.get("x", 0.0))
    y = float(pos.get("y", 0.0))
    text = str(label.get("text") or "")
    width = max(3.0, len(text) * 0.9)
    height = 2.0
    angle = float(pos.get("angle", 0.0)) % 360
    if angle in {90.0, 270.0}:
        return (x - height / 2.0, y, x + height / 2.0, y + width)
    if angle == 180.0:
        return (x - width, y - height / 2.0, x, y + height / 2.0)
    return (x, y - height / 2.0, x + width, y + height / 2.0)


def _rects_intersect(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    *,
    padding: float = 0.0,
) -> bool:
    return not (
        first[2] + padding <= second[0]
        or first[0] - padding >= second[2]
        or first[3] + padding <= second[1]
        or first[1] - padding >= second[3]
    )


def _point_in_rect(
    point: tuple[float, float], rect: tuple[float, float, float, float]
) -> bool:
    return rect[0] <= point[0] <= rect[2] and rect[1] <= point[1] <= rect[3]


def _rect_to_dict(rect: tuple[float, float, float, float]) -> dict[str, float]:
    return {"left": rect[0], "top": rect[1], "right": rect[2], "bottom": rect[3]}


def _is_rail_like_label(text: str) -> bool:
    upper = text.upper()
    return upper in {"GND", "AGND", "DGND", "VCC", "VDD", "VBUS"} or upper.startswith("+")


def _tiny_stubs_by_symbol(
    stubs: list[dict[str, Any]], schematic: KiCadSchematic, schematic_path: str
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for symbol in schematic.list_symbols():
        pin_points = set()
        try:
            pin_map = get_symbol_pin_map_from_schematic(
                schematic,
                schematic_path,
                symbol["reference"],
            )
        except Exception:
            pin_map = {"success": False}
        if pin_map.get("success"):
            pin_points = {
                (pin["connection_point"]["x"], pin["connection_point"]["y"])
                for pin in pin_map.get("pins", [])
            }
        if not pin_points:
            continue
        for wire in stubs:
            if any((point["x"], point["y"]) in pin_points for point in wire.get("points", [])):
                counts[symbol["reference"]] = counts.get(symbol["reference"], 0) + 1
    return counts


def _isolated_labels(schematic: KiCadSchematic, native: dict[str, Any]) -> list[dict[str, Any]]:
    if not native.get("success"):
        return []
    nets = native.get("nets", {})
    isolated = []
    for label in schematic.list_labels():
        net = nets.get(label["text"])
        if not net or not net.get("nodes"):
            isolated.append(
                {
                    "text": label["text"],
                    "type": label["type"],
                    "uuid": label.get("uuid"),
                    "position": label["position"],
                }
            )
    return isolated


def _schematic_pin_points(schematic: KiCadSchematic, schematic_path: str) -> set[tuple[float, float]]:
    points: set[tuple[float, float]] = set()
    for symbol in schematic.list_symbols():
        pin_map = None
        try:
            from kicad_mcp.utils.schematic_pins import get_symbol_pin_map_from_schematic

            pin_map = get_symbol_pin_map_from_schematic(
                schematic, schematic_path, symbol["reference"]
            )
        except Exception:
            pin_map = None
        if not pin_map or not pin_map.get("success"):
            continue
        for pin in pin_map.get("pins", []):
            point = pin.get("connection_point", {})
            if "x" in point and "y" in point:
                points.add((point["x"], point["y"]))
    return points


def _native_power_ground_mismatches(native: dict[str, Any]) -> list[dict[str, Any]]:
    if not native.get("success"):
        return []
    mismatches = []
    for net_name, net in native.get("nets", {}).items():
        for node in net.get("nodes", []):
            mismatch = _power_ground_mismatch(
                node.get("ref", ""),
                node.get("pinfunction") or node.get("pin", ""),
                net_name,
            )
            if mismatch is not None:
                mismatches.append(
                    {
                        **mismatch,
                        "ref": node.get("ref", ""),
                        "pin": node.get("pin", ""),
                        "pinfunction": node.get("pinfunction", ""),
                        "pintype": node.get("pintype", ""),
                    }
                )
    return mismatches


def _power_ground_mismatch(reference: str, pin_or_function: str, net_name: str) -> dict[str, str] | None:
    pin_kind = _power_pin_kind(pin_or_function)
    net_kind = _power_net_kind(net_name)
    if not pin_kind or not net_kind or pin_kind == net_kind:
        return None
    return {
        "ref": reference,
        "pin": pin_or_function,
        "net": net_name,
        "pin_kind": pin_kind,
        "net_kind": net_kind,
        "reason": f"{pin_or_function} looks like {pin_kind}, but net {net_name} looks like {net_kind}",
    }


def _power_pin_kind(pin_or_function: str) -> str | None:
    text = pin_or_function.upper().replace(" ", "").replace("-", "")
    if (
        text in {"GND", "VSS", "GNDA", "DGND", "AGND", "PGND"}
        or text.startswith(("GND_", "VSS_", "GNDA_", "DGND_", "AGND_", "PGND_"))
        or text in {"GND1", "GND2", "VSS1", "VSS2"}
    ):
        return "ground"
    if text in {"VCC", "VDD", "VDDA", "VDDD", "VBUS", "VIN", "VUSB", "3V3", "3.3V", "+3.3V", "+5V"}:
        return "power"
    if text.startswith(
        ("VCC_", "VDD_", "VDDA_", "VDDD_", "VBUS_", "VIN_", "3V3_", "3.3V_", "+3.3V_", "+5V_")
    ):
        return "power"
    return None


def _power_net_kind(net_name: str) -> str | None:
    text = net_name.upper().replace(" ", "")
    if text in {"GND", "VSS", "GNDA", "DGND", "AGND", "PGND"}:
        return "ground"
    if text.startswith("+") or text in {"VCC", "VDD", "VBUS", "VIN", "VUSB", "3V3", "3.3V"}:
        return "power"
    return None


def _on_grid(value: float, grid: float = SCHEMATIC_GRID_MM) -> bool:
    return abs((value / grid) - round(value / grid)) < 1e-5


def _snap(value: float, grid: float = SCHEMATIC_GRID_MM) -> float:
    return round(round(value / grid) * grid, 6)


def _sym(
    reference: str,
    lib_id: str,
    value: str,
    x: float,
    y: float,
    angle: float = 0.0,
    footprint: str | None = None,
) -> dict[str, Any]:
    return {
        "reference": reference,
        "lib_id": lib_id,
        "value": value,
        "x": _snap(x),
        "y": _snap(y),
        "angle": angle,
        "footprint": footprint,
    }


def _conn(
    ref: str, pin: str, net: str, allow_hidden_power: bool = False
) -> dict[str, Any]:
    return {
        "ref": ref,
        "pin": pin,
        "net": net,
        "label_type": "global",
        "stub_length_mm": 5.08,
        "allow_hidden_power": allow_hidden_power,
    }
