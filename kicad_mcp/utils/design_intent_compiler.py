"""Generic bulk design-intent compiler for schematic generation."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any

from kicad_mcp.utils.file_utils import get_project_files
from kicad_mcp.utils.kicad_s_expr import KiCadSchematic
from kicad_mcp.utils.path_validator import PathValidationError, get_configured_validator
from kicad_mcp.utils.schematic_pins import PinVisibility, _resolve_symbol_pins, classify_pin
from kicad_mcp.utils.transactional_edit import atomic_write_text

DEFAULT_FOOTPRINTS = {
    "capacitor": "Capacitor_SMD:C_0603_1608Metric",
    "resistor": "Resistor_SMD:R_0603_1608Metric",
    "led": "LED_SMD:LED_0603_1608Metric",
    "switch": "Button_Switch_SMD:SW_SPST_SKQG_WithStem",
    "header_1x02": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
    "header_1x03": "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
    "header_1x04": "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
    "header_1x05": "Connector_PinHeader_2.54mm:PinHeader_1x05_P2.54mm_Vertical",
    "header_1x06": "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical",
    "test_point": "TestPoint:TestPoint_Pad_D1.0mm",
    "ferrite": "Inductor_SMD:L_0603_1608Metric",
    "crystal": "Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm",
}

PASSIVE_SYMBOLS = {
    "C": ("Device:C", "capacitor"),
    "R": ("Device:R", "resistor"),
    "D": ("Device:LED", "led"),
    "SW": ("Switch:SW_Push", "switch"),
    "J": ("Connector_Generic:Conn_01x{pin_count}", "header_1x{pin_count:02d}"),
    "TP": ("Connector:TestPoint", "test_point"),
    "FB": ("Device:FerriteBead", "ferrite"),
    "#FLG": ("power:PWR_FLAG", None),
}


DESIGN_INTENT_TOP_LEVEL_SCHEMA = {
    "accepted_top_level_shape": {
        "parts": [],
        "rails": {},
        "pin_rules": [],
        "interfaces": {
            "i2c": [],
            "spi": [],
            "uart": [],
            "usb2": [],
            "swd": [],
            "gpio": [],
            "interrupt": [],
            "analog": [],
            "power": [],
        },
        "support_circuits": {
            "decoupling": [],
            "pullup": [],
            "pulldown": [],
            "crystal": [],
            "reset_button": [],
            "led_indicator": [],
            "ferrite_filter": [],
            "power_flag": [],
            "connector_header": [],
        },
        "bulk_connections": [],
        "no_connect_rules": [],
        "layout_hints": {},
        "paper": "A4",
        "allow_hidden_power": False,
        "action": "replace | merge",
    },
    "alternate_flat_shape": {
        "interfaces": [{"type": "i2c"}],
        "support_circuits": [{"type": "decoupling"}],
    },
}

DESIGN_INTENT_OVERVIEW = {
    "description": "High-level schematic intent compiled by schematic_start_design_intent_job.",
    "workflow": [
        "resolve_symbols/resolve_footprints when using installed libraries",
        "schematic_preview_design_intent",
        "schematic_start_design_intent_job",
        "schematic_get_job_status until terminal",
        "schematic_get_job_result",
        "pcb_preview_layout_intent or schematic_export_candidate_to_project on recoverable failure",
    ],
    "symbol_resolution_detail_values": ["compact", "pins", "full"],
    "candidate_artifacts": {
        "policy": "failed generated schematics are preserved as candidate_schematic_artifacts",
        "promotion_tool": "schematic_export_candidate_to_project",
    },
}

DESIGN_INTENT_FULL_EXAMPLE = {
    "name": "mcu_usb_spi_i2c_debug",
    "parts": [
        {
            "ref": "U1",
            "lib_id": "MCU_ST_STM32G4:STM32G431KBTx",
            "value": "STM32G431KBTx",
            "footprint": "Package_QFP:LQFP-32_7x7mm_P0.8mm",
            "block": "mcu",
        },
        {
            "ref": "U2",
            "lib_id": "Regulator_Linear:AP2112K-3.3",
            "value": "AP2112K-3.3",
            "footprint": "Package_TO_SOT_SMD:SOT-23-5",
            "block": "power",
        },
        {
            "ref": "J1",
            "lib_id": "Connector:USB_C_Receptacle_USB2.0",
            "value": "USB-C",
            "footprint": "Connector_USB:USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal",
            "block": "power",
        },
        {
            "ref": "J2",
            "lib_id": "Connector_Generic:Conn_01x05",
            "value": "DEBUG",
            "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x05_P2.54mm_Vertical",
            "block": "interfaces",
        },
        {
            "ref": "U3",
            "value": "I2C_SENSOR",
            "footprint": "Package_LGA:LGA-8_2x2mm_P0.5mm",
            "pins": [
                {"number": "1", "name": "SCL", "pintype": "bidirectional"},
                {"number": "2", "name": "SDA", "pintype": "bidirectional"},
                {"number": "3", "name": "VDD", "pintype": "power_in"},
                {"number": "4", "name": "GND", "pintype": "power_in"},
                {"number": "5", "name": "INT", "pintype": "output"},
            ],
            "block": "sensors",
        },
        {
            "ref": "U4",
            "lib_id": "Memory_Flash:W25Q32JVSS",
            "value": "W25Q32",
            "footprint": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
            "block": "memory",
        },
    ],
    "rails": {
        "+5V": {"pins": [["J1", "A4"], ["J1", "B9"], ["U2", "VIN"]]},
        "+3V3": {"pins": [["U2", "VOUT"], ["U1", "VDD"], ["U3", "VDD"], ["U4", "VCC"]]},
        "GND": {"pins": [["J1", "A1"], ["J1", "B1"], ["U2", "GND"], ["U1", "VSS"], ["U3", "GND"], ["U4", "GND"], ["J2", "3"]]},
    },
    "interfaces": [
        {
            "type": "i2c",
            "name": "SENSOR_I2C",
            "controller": {"ref": "U1", "scl": "PB6", "sda": "PB7"},
            "devices": [{"ref": "U3", "scl": "SCL", "sda": "SDA"}],
        },
        {
            "type": "spi",
            "name": "FLASH_SPI",
            "controller": {"ref": "U1", "sck": "PA5", "miso": "PA6", "mosi": "PA7"},
            "devices": [{"ref": "U4", "sck": "CLK", "miso": "DO", "mosi": "DI", "cs": "FLASH_CS", "cs_pin": "~{CS}"}],
        },
    ],
    "bulk_connections": [
        {"net": "SWDIO", "pins": [["U1", "PA13"], ["J2", "2"]]},
        {"net": "SWCLK", "pins": [["U1", "PA14"], ["J2", "4"]]},
        {"net": "RESET_N", "pins": [["U1", "NRST"], ["J2", "5"]]},
        {"net": "SENSOR_INT", "pins": [["U3", "INT"], ["U1", "PB8"]]},
    ],
    "support_circuits": {
        "decoupling": [
            {"target": "U1", "rail": "+3V3", "ground": "GND", "capacitors": ["100n", "1u"]},
            {"target": "U3", "rail": "+3V3", "ground": "GND", "capacitors": ["100n"]},
        ],
        "reset_button": [{"target": "U1", "pin": "NRST", "net": "RESET_N", "ground": "GND"}],
        "pullup": [
            {"net": "SENSOR_I2C_SCL", "rail": "+3V3", "value": "4.7k"},
            {"net": "SENSOR_I2C_SDA", "rail": "+3V3", "value": "4.7k"},
        ],
        "power_flag": [{"net": "+5V"}, {"net": "+3V3"}, {"net": "GND"}],
    },
    "no_connect_rules": [
        {
            "ref": "U1",
            "match": {"name_regex": "^(PA|PB)[0-9]+$"},
            "except": ["PA5", "PA6", "PA7", "PA13", "PA14", "PB6", "PB7"],
            "action": "mark_no_connect",
        }
    ],
}


DESIGN_INTENT_SCHEMA = {
    "parts": {
        "example": [
            {
                "ref": "U1",
                "lib_id": "MCU_ST_STM32F1:STM32F103C8Tx",
                "value": "STM32F103C8T6",
                "footprint": "Package_QFP:LQFP-48_7x7mm_P0.5mm",
            }
        ],
        "required_fields": ["ref", "lib_id or pins", "value", "footprint for non-power parts"],
        "optional_fields": ["pins", "x", "y", "angle", "properties"],
    },
    "action": {
        "example": "merge",
        "values": ["replace", "merge", "add", "update", "patch"],
        "default": "replace",
        "notes": "Use merge/add/update/patch to combine the supplied intent with the last successfully committed intent for the project.",
    },
    "rails": {
        "example": {
            "+3V3": {"pins": [["U1", "VDD"], ["U2", "VDD"]]},
            "GND": {"pins": [["U1", "VSS"], ["U2", "GND"]]},
        },
        "alternate_example": [{"name": "+3V3", "pins": [["U1", "VDD"]]}],
        "required_fields": ["rail name", "pins"],
        "optional_fields": [],
    },
    "pin_rules": {
        "example": [
            {"ref": "U1", "match": {"name": "VDD"}, "net": "+3V3"},
            {"ref": "U1", "match": {"name_regex": "^(VSS|VSSA|GND)$"}, "net": "GND"},
        ],
        "selector_fields": [
            "pin",
            "pins",
            "name",
            "number",
            "names",
            "numbers",
            "name_regex",
            "number_regex",
            "pin_type",
            "name_contains",
            "exclude",
        ],
        "selector_notes": {
            "name": "Exact pin-name match. Prefer this when connecting one named pin.",
            "number": "Exact pin-number match.",
            "name_regex": "Regex uses substring search; use anchors such as ^VDD$ for exact regex matching.",
            "number_regex": "Regex uses substring search; use anchors such as ^18$ for exact regex matching.",
        },
        "required_fields": ["ref", "match", "net"],
        "optional_fields": ["match.exclude", "allow_hidden_power"],
    },
    "interfaces.i2c": {
        "example": [
            {
                "type": "i2c",
                "name": "IMU_I2C",
                "controller": {"ref": "U1", "scl": "PB6", "sda": "PB7"},
                "devices": [{"ref": "U2", "scl": "SCL", "sda": "SDA"}],
                "pullups": {"rail": "+3V3", "value": "4.7k"},
            }
        ],
        "required_fields": ["type", "controller", "devices"],
        "optional_fields": ["name", "scl_net", "sda_net", "pullups"],
    },
    "interfaces.spi": {
        "example": [
            {
                "type": "spi",
                "name": "FLASH_SPI",
                "controller": {"ref": "U1", "sck": "PA5", "miso": "PA6", "mosi": "PA7"},
                "devices": [{"ref": "U3", "sck": "SCK", "miso": "SO", "mosi": "SI", "cs": "FLASH_CS", "cs_pin": "~{CS}"}],
            }
        ],
        "required_fields": ["type", "controller", "devices"],
        "optional_fields": ["name", "sck_net", "miso_net", "mosi_net", "cs_net"],
    },
    "interfaces.swd": {
        "example": [
            {
                "type": "swd",
                "target": "U1",
                "swdio": "PA13",
                "swclk": "PA14",
                "reset": "NRST",
                "rail": "+3V3",
                "ground": "GND",
            }
        ],
        "required_fields": ["type", "target"],
        "optional_fields": ["swdio", "swclk", "reset", "rail", "ground", "header"],
    },
    "support_circuits.decoupling": {
        "example": [{"type": "decoupling", "target": "U1", "rail": "+3V3", "ground": "GND", "capacitors": ["100n", "4.7u"]}],
        "required_fields": ["type", "rail", "capacitors"],
        "optional_fields": ["target", "ground", "footprint", "footprints"],
        "generated_parts_summary": "One capacitor per capacitors entry.",
        "generated_nets_summary": "Each capacitor connects rail to ground.",
    },
    "support_circuits.pullup": {
        "example": [{"type": "pullup", "net": "RESET_N", "rail": "+3V3", "value": "10k"}],
        "target_pin_alias_example": [{"type": "pullup", "target": "U1", "pin": "NRST", "rail": "+3V3", "net": "RESET_N"}],
        "required_fields": ["type", "net or target/ref+pin", "rail"],
        "optional_fields": ["target", "ref", "pin", "value", "footprint"],
        "generated_parts_summary": "One resistor.",
        "generated_nets_summary": "Resistor connects net to rail; with target/ref+pin, the target pin is also connected to the net.",
    },
    "support_circuits.pulldown": {
        "example": [{"type": "pulldown", "net": "BOOT0", "ground": "GND", "value": "10k"}],
        "required_fields": ["type", "net"],
        "optional_fields": ["target", "ground", "value", "footprint"],
        "generated_parts_summary": "One resistor.",
        "generated_nets_summary": "Resistor connects net to ground.",
    },
    "support_circuits.crystal": {
        "example": [{"type": "crystal", "target": "U1", "pins": ["OSC_IN", "OSC_OUT"], "value": "8MHz"}],
        "alias_example": [{"type": "crystal", "target": "U1", "xin": "OSC_IN", "xout": "OSC_OUT", "value": "8MHz"}],
        "grounded_example": [{"type": "crystal", "lib_id": "Device:Crystal_GND2", "target": "U1", "pins": ["OSC_IN", "OSC_OUT"], "ground": "GND", "value": "8MHz"}],
        "required_fields": ["type", "pins or xin+xout"],
        "optional_fields": ["target", "ref", "value", "footprint", "lib_id", "symbol", "ground", "xin", "xout"],
        "generated_parts_summary": "One crystal.",
        "generated_nets_summary": "Crystal pins 1 and 2 connect to the two listed nets; grounded symbols also connect ground pins.",
    },
    "support_circuits.reset_button": {
        "example": [{"type": "reset_button", "target": "U1", "pin": "NRST", "net": "RESET_N", "rail": "+3V3", "pullup": "10k", "ground": "GND"}],
        "required_fields": ["type"],
        "optional_fields": ["target", "pin", "net", "rail", "pullup", "ground", "ref", "footprint"],
        "generated_parts_summary": "One push button and optional pullup resistor.",
        "generated_nets_summary": "Button connects reset net to ground; pullup connects reset net to rail.",
    },
    "support_circuits.led_indicator": {
        "example": [{"type": "led_indicator", "name": "STATUS", "rail": "+3V3", "ground": "GND", "resistor": "1k"}],
        "required_fields": ["type", "rail"],
        "optional_fields": ["name", "target", "ground", "net", "resistor", "resistor_footprint", "led_footprint", "led_color"],
        "generated_parts_summary": "One resistor and one LED.",
        "generated_nets_summary": "Resistor connects rail to LED anode net; LED cathode connects to ground.",
    },
    "support_circuits.ferrite_filter": {
        "example": [{"type": "ferrite_filter", "in_net": "+3V3", "out_net": "+3V3_A", "value": "Ferrite"}],
        "alias_example": [{"type": "ferrite_filter", "rail": "+3V3", "supply_rail": "+3V3_A", "value": "Ferrite"}],
        "required_fields": ["type", "in_net/rail", "out_net/supply_rail"],
        "optional_fields": ["target", "value", "footprint", "rail", "supply_rail", "input_net", "output_net", "filtered_net", "net"],
        "generated_parts_summary": "One ferrite bead.",
        "generated_nets_summary": "Ferrite connects input net to output net.",
    },
    "support_circuits.power_flag": {
        "example": [{"type": "power_flag", "net": "+3V3"}],
        "required_fields": ["type", "net or rail"],
        "optional_fields": ["ref"],
        "generated_parts_summary": "One PWR_FLAG power symbol.",
        "generated_nets_summary": "PWR_FLAG pin connects to the selected power net.",
    },
    "support_circuits.connector_header": {
        "example": [{"type": "connector_header", "ref": "J2", "name": "DEBUG", "pins": ["+3V3", "SWDIO", "GND", "SWCLK", "RESET_N"]}],
        "required_fields": ["type"],
        "optional_fields": ["ref", "name", "value", "pin_count", "pins", "footprint", "target"],
        "generated_parts_summary": "One generic 1xN connector.",
        "generated_nets_summary": "Each listed net connects to the matching connector pin number.",
    },
    "bulk_connections": {
        "example": [{"net": "IMU_INT", "pins": [["U1", "PA0"], ["U2", "INT"]]}],
        "connection_plan_alias_example": [{"type": "pin_to_net", "ref": "U1", "pin": "PA0", "net": "IMU_INT"}],
        "required_fields": ["net and pins, net_prefix and map, or type=pin_to_net/pin_to_pin"],
        "optional_fields": ["allow_hidden_power"],
    },
    "no_connect_rules": {
        "example": [{"ref": "U1", "match": {"name_regex": "PA[0-9]+|PB[0-9]+"}, "except": ["PB6", "PB7", "PA13", "PA14"], "action": "mark_no_connect"}],
        "required_fields": ["ref", "match"],
        "optional_fields": ["except", "include_hidden", "allow_hidden_no_connect", "action"],
    },
}


def design_intent_schema(section: str = "all") -> dict[str, Any]:
    """Return compact design-intent examples and field metadata for agents."""
    normalized = str(section or "all").strip().lower()
    base: dict[str, Any]
    if normalized == "all":
        schemas = {"intent": deepcopy(DESIGN_INTENT_TOP_LEVEL_SCHEMA)}
        schemas["overview"] = deepcopy(DESIGN_INTENT_OVERVIEW)
        schemas["full_example"] = deepcopy(DESIGN_INTENT_FULL_EXAMPLE)
        schemas.update(deepcopy(DESIGN_INTENT_SCHEMA))
        base = {"success": True, "section": "all", "schemas": schemas}
    elif normalized == "overview":
        base = {
            "success": True,
            "section": "overview",
            "schema": deepcopy(DESIGN_INTENT_OVERVIEW),
        }
    elif normalized in {"full_example", "example"}:
        base = {
            "success": True,
            "section": "full_example",
            "schema": deepcopy(DESIGN_INTENT_FULL_EXAMPLE),
        }
    elif normalized in {"intent", "top_level", "top-level"}:
        base = {
            "success": True,
            "section": "intent",
            "schema": deepcopy(DESIGN_INTENT_TOP_LEVEL_SCHEMA),
        }
    elif normalized in DESIGN_INTENT_SCHEMA:
        base = {"success": True, "section": normalized, "schema": deepcopy(DESIGN_INTENT_SCHEMA[normalized])}
    else:
        prefix = f"{normalized}."
        matches = {
            key: value for key, value in DESIGN_INTENT_SCHEMA.items() if key.startswith(prefix)
        }
        if matches:
            base = {"success": True, "section": normalized, "schemas": deepcopy(matches)}
        else:
            return {
                "success": False,
                "section": normalized,
                "error": "unknown design-intent schema section",
                "available_sections": sorted(
                    [*DESIGN_INTENT_SCHEMA, "intent", "overview", "full_example"]
                ),
            }

    base["recommended_apply_tool"] = "schematic_start_design_intent_job"
    base["recommended_status_tool"] = "schematic_get_job_status"
    base["recommended_result_tool"] = "schematic_get_job_result"
    return base


def compile_design_intent(
    project_path: str,
    intent: dict[str, Any],
    *,
    strict: bool = False,
) -> dict[str, Any]:
    """Compile generic bulk circuit intent into the existing v2 schematic spec."""
    compiler = _DesignIntentCompiler(project_path, intent, strict=strict)
    return compiler.compile()


def select_pins(symbol_pin_map: list[dict[str, Any]], selector: dict[str, Any]) -> list[dict[str, Any]]:
    """Select symbol pins by exact name/number, regex, type, contains, and exclusion rules."""
    if not isinstance(selector, dict):
        return []
    exclude = selector.get("exclude")
    positive = {key: value for key, value in selector.items() if key != "exclude"}
    matches = [pin for pin in symbol_pin_map if _pin_matches(pin, positive)]
    if isinstance(exclude, dict):
        matches = [pin for pin in matches if not _pin_matches(pin, exclude)]
    return matches


class ReferenceAllocator:
    """Allocate KiCad references while avoiding existing and already-generated refs."""

    def __init__(self, existing_refs: list[str] | None = None) -> None:
        self.used = set(existing_refs or [])

    def next(self, prefix: str) -> str:
        normalized = str(prefix)
        start = 1
        if normalized.startswith("#"):
            start = 1
        for index in range(start, 10000):
            ref = f"{normalized}{index}" if not normalized.startswith("#FLG") else f"#FLG{index:02d}"
            if ref not in self.used:
                self.used.add(ref)
                return ref
        raise ValueError(f"Unable to allocate reference for prefix {prefix}")


class _DesignIntentCompiler:
    def __init__(self, project_path: str, intent: dict[str, Any], *, strict: bool) -> None:
        self.project_path = project_path
        self.intent = deepcopy(intent) if isinstance(intent, dict) else {}
        self.strict = strict
        self.errors: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []
        self.parts: list[dict[str, Any]] = []
        self.nets: dict[str, list[Any]] = {}
        self.no_connects: list[dict[str, Any]] = []
        self.generated_refs: dict[str, list[str]] = {}
        self.pin_maps: dict[str, list[dict[str, Any]]] = {}
        self.pin_name_counts: dict[str, dict[str, int]] = {}
        self.pin_assignments: dict[tuple[str, str], str] = {}
        self.skipped_hidden_pins: list[dict[str, Any]] = []
        self.existing_refs = _existing_schematic_refs(project_path)
        self.allocator = ReferenceAllocator(self.existing_refs)
        self.default_allow_hidden_power = False

    def compile(self) -> dict[str, Any]:
        normalized = self._normalize_intent()
        self.default_allow_hidden_power = bool(normalized.get("allow_hidden_power", False))
        self.parts.extend(normalized["parts"])
        for part in self.parts:
            ref = str(part.get("ref") or part.get("reference") or "")
            if ref:
                self.allocator.used.add(ref)
        self._load_pin_maps()
        self._expand_rails(normalized.get("rails", {}))
        self._expand_pin_rules(normalized["pin_rules"])
        self._expand_interfaces(normalized["interfaces"])
        self._expand_support_circuits(normalized["support_circuits"])
        self._expand_bulk_connections(normalized["bulk_connections"])
        self._expand_no_connect_rules(normalized["no_connect_rules"])
        self._validate_generated_refs()
        expanded_spec = {
            "name": normalized.get("name") or "design_intent",
            "parts": self.parts,
            "nets": _sorted_nets(self.nets),
            "no_connects": self.no_connects,
            "layout_hints": normalized.get("layout_hints", {}),
        }
        if normalized.get("paper"):
            expanded_spec["paper"] = str(normalized["paper"])
        summary = {
            "input_part_count": len(normalized["parts"]),
            "generated_part_count": max(len(self.parts) - len(normalized["parts"]), 0),
            "total_part_count": len(self.parts),
            "connection_count": sum(len(pins) for pins in self.nets.values()),
            "net_count": len(self.nets),
            "no_connect_count": len(self.no_connects),
            "skipped_hidden_pin_count": len(self.skipped_hidden_pins),
            "skipped_hidden_pins": self.skipped_hidden_pins,
        }
        success = not self.errors
        result = {
            "success": success,
            "expanded_spec": expanded_spec,
            "summary": summary,
            "generated_refs": self.generated_refs,
            "warnings": self.warnings,
            "errors": self.errors,
            "skipped_hidden_pin_count": len(self.skipped_hidden_pins),
            "skipped_hidden_pins": self.skipped_hidden_pins,
        }
        if not success:
            result["recoverable"] = True
        result.update(self._save_artifacts(normalized, expanded_spec, result))
        return result

    def _normalize_intent(self) -> dict[str, Any]:
        if not isinstance(self.intent, dict):
            self.errors.append({"path": "intent", "error": "intent must be an object"})
            return _empty_intent()
        normalized = _empty_intent()
        normalized.update({key: deepcopy(value) for key, value in self.intent.items() if key in normalized})
        for key in ("parts", "pin_rules", "bulk_connections", "no_connect_rules"):
            if not isinstance(normalized[key], list):
                self.errors.append({"path": key, "error": f"{key} must be a list"})
                normalized[key] = []
        normalized["interfaces"] = self._normalize_grouped_entries(
            normalized["interfaces"],
            "interfaces",
        )
        normalized["support_circuits"] = self._normalize_grouped_entries(
            normalized["support_circuits"],
            "support_circuits",
        )
        normalized["rails"] = self._normalize_rails(normalized["rails"])
        if not isinstance(normalized["layout_hints"], dict):
            self.errors.append({"path": "layout_hints", "error": "layout_hints must be an object"})
            normalized["layout_hints"] = {}
        normalized["allow_hidden_power"] = bool(normalized.get("allow_hidden_power", False))
        return normalized

    def _normalize_grouped_entries(self, value: Any, path: str) -> list[Any]:
        if isinstance(value, list):
            return value
        if not isinstance(value, dict):
            self.errors.append({"path": path, "error": f"{path} must be a list or grouped object"})
            return []

        flattened: list[Any] = []
        for group, entries in value.items():
            group_path = f"{path}.{group}"
            if isinstance(entries, dict):
                candidate_entries = [entries]
            elif isinstance(entries, list):
                candidate_entries = entries
            else:
                self.errors.append(
                    {
                        "path": group_path,
                        "error": f"{path} group must be a list or object",
                    }
                )
                continue
            for index, entry in enumerate(candidate_entries):
                if not isinstance(entry, dict):
                    self.errors.append(
                        {
                            "path": f"{group_path}[{index}]",
                            "error": f"{path} entry must be an object",
                        }
                    )
                    continue
                normalized_entry = deepcopy(entry)
                normalized_entry.setdefault("type", str(group))
                flattened.append(normalized_entry)
        return flattened

    def _normalize_rails(self, rails: Any) -> dict[str, Any]:
        if isinstance(rails, dict):
            return rails
        if isinstance(rails, list):
            normalized: dict[str, Any] = {}
            for index, rail in enumerate(rails):
                if not isinstance(rail, dict):
                    self.errors.append({"path": f"rails[{index}]", "error": "rail entry must be an object"})
                    continue
                name = rail.get("name") or rail.get("net") or rail.get("rail")
                if not name:
                    self.errors.append({"path": f"rails[{index}].name", "error": "rail entry requires name"})
                    continue
                normalized[str(name)] = {key: deepcopy(value) for key, value in rail.items() if key != "name"}
            return normalized
        self.errors.append({"path": "rails", "error": "rails must be an object or list"})
        return {}

    def _load_pin_maps(self) -> None:
        refs_seen: set[str] = set(self.existing_refs)
        for index, part in enumerate(self.parts):
            if not isinstance(part, dict):
                self.errors.append({"path": f"parts[{index}]", "error": "part must be an object"})
                continue
            ref = str(part.get("ref") or part.get("reference") or "")
            if not ref:
                self.errors.append({"path": f"parts[{index}]", "error": "part requires ref"})
                continue
            if ref in refs_seen and ref not in self.existing_refs:
                self.errors.append({"path": f"parts[{index}].ref", "error": "duplicate part ref", "ref": ref})
                continue
            refs_seen.add(ref)
            pins = self._pins_for_part(part, f"parts[{index}]")
            if pins:
                self.pin_maps[ref] = pins
                self.pin_name_counts[ref] = _pin_name_counts(pins)
        self._load_existing_pin_maps()

    def _pins_for_part(self, part: dict[str, Any], path: str) -> list[dict[str, Any]]:
        if isinstance(part.get("pins"), list):
            pins = []
            for index, pin in enumerate(part["pins"]):
                if not isinstance(pin, dict):
                    self.errors.append({"path": f"{path}.pins[{index}]", "error": "pin must be an object"})
                    continue
                number = str(pin.get("number") or pin.get("pin") or "")
                if not number:
                    self.errors.append({"path": f"{path}.pins[{index}]", "error": "pin requires number"})
                    continue
                name = str(pin.get("name") or number)
                pins.append(
                    {
                        "number": number,
                        "name": name,
                        "pinfunction": f"{name}_{number}" if name and number else name or number,
                        "pintype": str(pin.get("type") or pin.get("pintype") or "passive"),
                        "hidden": bool(pin.get("hidden", False)),
                    }
                )
            return pins
        lib_id = part.get("lib_id") or part.get("symbol") or part.get("kicad_symbol")
        if not lib_id:
            self.errors.append({"path": path, "error": "part requires lib_id/symbol or custom pins"})
            return []
        try:
            return _resolve_symbol_pins(str(lib_id))
        except Exception as exc:
            if self.strict:
                self.errors.append({"path": path, "error": "unable to resolve symbol pins", "lib_id": str(lib_id), "detail": str(exc)})
            else:
                self.warnings.append({"path": path, "warning": "unable to resolve symbol pins; selector rules cannot target this part", "lib_id": str(lib_id), "detail": str(exc)})
            return []

    def _load_existing_pin_maps(self) -> None:
        schematic_path = _schematic_path_if_exists(self.project_path)
        if schematic_path is None:
            return
        try:
            schematic = KiCadSchematic.from_file(schematic_path)
        except Exception:
            return
        for symbol in schematic.list_symbols():
            ref = str(symbol.get("reference") or "")
            lib_id = symbol.get("lib_id")
            if not ref or ref in self.pin_maps or not lib_id:
                continue
            try:
                pins = _resolve_symbol_pins(str(lib_id))
            except Exception:
                continue
            self.pin_maps[ref] = pins
            self.pin_name_counts[ref] = _pin_name_counts(pins)

    def _expand_rails(self, rails: dict[str, Any]) -> None:
        for rail_name, rail_spec in rails.items():
            if not isinstance(rail_spec, dict):
                continue
            for endpoint in rail_spec.get("pins", []):
                parsed = _endpoint(endpoint)
                if parsed:
                    self._add_connection(
                        str(rail_name),
                        parsed[0],
                        parsed[1],
                        "rails",
                        allow_hidden_power=_endpoint_allow_hidden_power(endpoint)
                        or self.default_allow_hidden_power,
                    )

    def _expand_pin_rules(self, rules: list[Any]) -> None:
        for index, rule in enumerate(rules):
            path = f"pin_rules[{index}]"
            if not isinstance(rule, dict):
                self.errors.append({"path": path, "error": "pin rule must be an object"})
                continue
            ref = str(rule.get("ref") or "")
            net = str(rule.get("net") or "")
            if not ref or not net:
                self.errors.append({"path": path, "error": "pin rule requires ref and net"})
                continue
            pins = self._select_rule_pins(ref, rule.get("match", {}), path)
            allow_hidden_power = bool(rule.get("allow_hidden_power", self.default_allow_hidden_power))
            for pin in pins:
                self._add_connection(
                    net,
                    ref,
                    self._pin_identifier(ref, pin),
                    path,
                    pin_info=pin,
                    allow_hidden_power=allow_hidden_power,
                )

    def _expand_interfaces(self, interfaces: list[Any]) -> None:
        expanders = {
            "i2c": self._expand_i2c,
            "spi": self._expand_spi,
            "uart": self._expand_uart,
            "usb2": self._expand_usb2,
            "swd": self._expand_swd,
            "gpio": self._expand_gpio,
            "interrupt": self._expand_gpio,
            "analog": self._expand_gpio,
            "power": self._expand_power_interface,
        }
        for index, interface in enumerate(interfaces):
            path = f"interfaces[{index}]"
            if not isinstance(interface, dict):
                self.errors.append({"path": path, "error": "interface must be an object"})
                continue
            kind = str(interface.get("type") or "").lower()
            expander = expanders.get(kind)
            if expander is None:
                self.errors.append({"path": path, "error": "unsupported interface type", "type": kind})
                continue
            expander(interface, path)

    def _expand_i2c(self, interface: dict[str, Any], path: str) -> None:
        name = str(interface.get("name") or "I2C")
        scl_net = str(interface.get("scl_net") or f"{name}_SCL")
        sda_net = str(interface.get("sda_net") or f"{name}_SDA")
        controller = interface.get("controller")
        if not isinstance(controller, dict):
            self.errors.append({"path": f"{path}.controller", "error": "i2c requires controller"})
            return
        self._connect_named_pin(scl_net, controller, "scl", f"{path}.controller")
        self._connect_named_pin(sda_net, controller, "sda", f"{path}.controller")
        for device_index, device in enumerate(interface.get("devices", [])):
            dpath = f"{path}.devices[{device_index}]"
            if not isinstance(device, dict):
                self.errors.append({"path": dpath, "error": "i2c device must be an object"})
                continue
            self._connect_named_pin(scl_net, device, "scl", dpath)
            self._connect_named_pin(sda_net, device, "sda", dpath)
            self._connect_pin_net_map(device.get("interrupts", {}), device.get("ref"), dpath)
            self._connect_pin_net_map(device.get("address_pins", {}), device.get("ref"), dpath)
        pullups = interface.get("pullups")
        if isinstance(pullups, dict):
            rail = pullups.get("rail")
            if not rail:
                self.errors.append({"path": f"{path}.pullups.rail", "error": "i2c pullups require rail"})
            else:
                value = str(pullups.get("value") or "4.7k")
                footprint = str(pullups.get("footprint") or DEFAULT_FOOTPRINTS["resistor"])
                target = str(controller.get("ref") or "")
                self._add_two_pin_part("R", value, footprint, scl_net, str(rail), "i2c_pullups", {"target": target, "rail": str(rail)})
                self._add_two_pin_part("R", value, footprint, sda_net, str(rail), "i2c_pullups", {"target": target, "rail": str(rail)})

    def _expand_spi(self, interface: dict[str, Any], path: str) -> None:
        name = str(interface.get("name") or "SPI")
        nets = {
            "sck": str(interface.get("sck_net") or f"{name}_SCK"),
            "miso": str(interface.get("miso_net") or f"{name}_MISO"),
            "mosi": str(interface.get("mosi_net") or f"{name}_MOSI"),
        }
        controller = interface.get("controller")
        if not isinstance(controller, dict):
            self.errors.append({"path": f"{path}.controller", "error": "spi requires controller"})
            return
        for signal, net in nets.items():
            self._connect_named_pin(net, controller, signal, f"{path}.controller")
        for device_index, device in enumerate(interface.get("devices", [])):
            dpath = f"{path}.devices[{device_index}]"
            if not isinstance(device, dict):
                self.errors.append({"path": dpath, "error": "spi device must be an object"})
                continue
            for signal, net in nets.items():
                self._connect_named_pin(net, device, signal, dpath)
            cs_net = device.get("cs_net") or device.get("cs")
            cs_pin = device.get("cs_pin") or ("CS" if device.get("cs_net") else None)
            if cs_net and cs_pin:
                self._add_connection(str(cs_net), str(device.get("ref")), str(cs_pin), dpath)
            elif cs_net and controller.get("cs"):
                self._add_connection(str(cs_net), str(controller.get("ref")), str(controller.get("cs")), f"{path}.controller")

    def _expand_uart(self, interface: dict[str, Any], path: str) -> None:
        name = str(interface.get("name") or "UART")
        a = interface.get("controller") or interface.get("a")
        b = interface.get("device") or interface.get("b")
        if not isinstance(a, dict) or not isinstance(b, dict):
            self.errors.append({"path": path, "error": "uart requires controller/device endpoints"})
            return
        tx_net = str(interface.get("tx_net") or f"{name}_TX")
        rx_net = str(interface.get("rx_net") or f"{name}_RX")
        self._connect_named_pin(tx_net, a, "tx", f"{path}.controller")
        self._connect_named_pin(tx_net, b, "rx", f"{path}.device")
        self._connect_named_pin(rx_net, a, "rx", f"{path}.controller")
        self._connect_named_pin(rx_net, b, "tx", f"{path}.device")

    def _expand_usb2(self, interface: dict[str, Any], path: str) -> None:
        name = str(interface.get("name") or "USB")
        dp = str(interface.get("dp_net") or f"{name}_D+")
        dm = str(interface.get("dm_net") or f"{name}_D-")
        for side_key in ("controller", "device", "connector"):
            side = interface.get(side_key)
            if isinstance(side, dict):
                self._connect_named_pin(dp, side, "dp", f"{path}.{side_key}")
                self._connect_named_pin(dm, side, "dm", f"{path}.{side_key}")

    def _expand_swd(self, interface: dict[str, Any], path: str) -> None:
        target = str(interface.get("target") or "")
        if not target:
            self.errors.append({"path": path, "error": "swd requires target"})
            return
        nets = {
            "swdio": str(interface.get("swdio_net") or "SWDIO"),
            "swclk": str(interface.get("swclk_net") or "SWCLK"),
            "reset": str(interface.get("reset_net") or "RESET_N"),
        }
        for signal in ("swdio", "swclk", "reset"):
            pin = interface.get(signal)
            if pin:
                self._add_connection(nets[signal], target, str(pin), path)
        rail = interface.get("rail")
        ground = interface.get("ground", "GND")
        header = interface.get("header") if isinstance(interface.get("header"), dict) else {}
        ref = str(header.get("ref") or self._allocate("J", "swd_header"))
        pin_count = int(header.get("pin_count") or (5 if rail else 4))
        self._add_header_part(ref, pin_count, str(header.get("footprint") or _header_footprint(pin_count)), header.get("value") or "SWD", {"generated_by": "swd_header", "target": target})
        assignments = [("1", str(rail) if rail else None), ("2", nets["swdio"]), ("3", ground), ("4", nets["swclk"])]
        if pin_count >= 5:
            assignments.append(("5", nets["reset"]))
        elif interface.get("reset"):
            assignments[0] = ("1", nets["reset"])
        for pin, net in assignments:
            if net:
                self._add_connection(str(net), ref, pin, path)

    def _expand_gpio(self, interface: dict[str, Any], path: str) -> None:
        for index, item in enumerate(interface.get("signals", [])):
            if not isinstance(item, dict):
                continue
            net = str(item.get("net") or item.get("name") or "")
            if not net:
                self.errors.append({"path": f"{path}.signals[{index}]", "error": "signal requires net/name"})
                continue
            for endpoint in item.get("pins", []):
                parsed = _endpoint(endpoint)
                if parsed:
                    self._add_connection(net, parsed[0], parsed[1], f"{path}.signals[{index}]")

    def _expand_power_interface(self, interface: dict[str, Any], path: str) -> None:
        net = str(interface.get("net") or interface.get("rail") or "")
        if not net:
            self.errors.append({"path": path, "error": "power interface requires net or rail"})
            return
        for endpoint in interface.get("pins", []):
            parsed = _endpoint(endpoint)
            if parsed:
                self._add_connection(net, parsed[0], parsed[1], path)

    def _expand_support_circuits(self, circuits: list[Any]) -> None:
        expanders = {
            "decoupling": self._support_decoupling,
            "pullup": self._support_pullup,
            "pulldown": self._support_pulldown,
            "series_resistor": self._support_series_resistor,
            "reset_button": self._support_reset_button,
            "led_indicator": self._support_led_indicator,
            "connector_header": self._support_connector_header,
            "test_point": self._support_test_point,
            "power_flag": self._support_power_flag,
            "rc_filter": self._support_rc_filter,
            "ferrite_filter": self._support_ferrite_filter,
            "crystal": self._support_crystal,
            "esd_diode": self._support_esd_diode,
        }
        for index, circuit in enumerate(circuits):
            path = f"support_circuits[{index}]"
            if not isinstance(circuit, dict):
                self.errors.append({"path": path, "error": "support circuit must be an object"})
                continue
            kind = str(circuit.get("type") or "").lower()
            expander = expanders.get(kind)
            if expander is None:
                self.errors.append({"path": path, "error": "unsupported support circuit type", "type": kind})
                continue
            expander(circuit, path)

    def _support_decoupling(self, circuit: dict[str, Any], path: str) -> None:
        rail = circuit.get("rail")
        ground = circuit.get("ground", "GND")
        caps = circuit.get("capacitors", [])
        footprints = circuit.get("footprints", {}) if isinstance(circuit.get("footprints"), dict) else {}
        if not rail or not isinstance(caps, list) or not caps:
            self.errors.append({"path": path, "error": "decoupling requires rail and capacitors"})
            return
        for value in caps:
            value_str = str(value)
            footprint = str(footprints.get(value_str) or circuit.get("footprint") or DEFAULT_FOOTPRINTS["capacitor"])
            self._add_two_pin_part("C", value_str, footprint, str(rail), str(ground), "decoupling", {"target": circuit.get("target"), "rail": str(rail)})

    def _support_pullup(self, circuit: dict[str, Any], path: str) -> None:
        self._support_resistor_to_rail(circuit, path, "rail", "pullups")

    def _support_pulldown(self, circuit: dict[str, Any], path: str) -> None:
        self._support_resistor_to_rail(circuit, path, "ground", "pulldowns", default_rail="GND")

    def _support_resistor_to_rail(self, circuit: dict[str, Any], path: str, rail_key: str, bucket: str, default_rail: str | None = None) -> None:
        target = circuit.get("target")
        pin = circuit.get("pin")
        if not target and pin and circuit.get("ref"):
            target = circuit.get("ref")
        net = circuit.get("net") or (str(pin) if target and pin else None)
        rail = circuit.get(rail_key) or default_rail
        if not net or not rail:
            self.errors.append({"path": path, "error": f"{bucket[:-1]} requires net and {rail_key}"})
            return
        if target and pin:
            self._add_connection(str(net), str(target), str(pin), path)
        self._add_two_pin_part(
            "R",
            str(circuit.get("value") or "10k"),
            str(circuit.get("footprint") or DEFAULT_FOOTPRINTS["resistor"]),
            str(net),
            str(rail),
            bucket,
            {"target": target, "rail": str(rail)},
        )

    def _support_series_resistor(self, circuit: dict[str, Any], path: str) -> None:
        in_net = circuit.get("in_net") or circuit.get("from")
        out_net = circuit.get("out_net") or circuit.get("to")
        if not in_net or not out_net:
            self.errors.append({"path": path, "error": "series_resistor requires in_net/from and out_net/to"})
            return
        self._add_two_pin_part("R", str(circuit.get("value") or "0"), str(circuit.get("footprint") or DEFAULT_FOOTPRINTS["resistor"]), str(in_net), str(out_net), "series_resistors", {"target": circuit.get("target")})

    def _support_reset_button(self, circuit: dict[str, Any], path: str) -> None:
        target = circuit.get("target")
        pin = circuit.get("pin")
        net = str(circuit.get("net") or "RESET_N")
        if target and pin:
            self._add_connection(net, str(target), str(pin), path)
        if circuit.get("pullup") and circuit.get("rail"):
            self._add_two_pin_part("R", str(circuit["pullup"]), str(circuit.get("resistor_footprint") or DEFAULT_FOOTPRINTS["resistor"]), net, str(circuit["rail"]), "reset", {"target": target, "rail": str(circuit["rail"])})
        sw_ref = str(circuit.get("ref") or self._allocate("SW", "reset"))
        self._add_part({"ref": sw_ref, "lib_id": "Switch:SW_Push", "value": str(circuit.get("value") or "RESET"), "footprint": str(circuit.get("footprint") or DEFAULT_FOOTPRINTS["switch"]), "generated_by": "reset", "target": target})
        self._add_connection(net, sw_ref, "1", path)
        self._add_connection(str(circuit.get("ground") or "GND"), sw_ref, "2", path)

    def _support_led_indicator(self, circuit: dict[str, Any], path: str) -> None:
        rail = circuit.get("rail")
        ground = circuit.get("ground", "GND")
        if not rail:
            self.errors.append({"path": path, "error": "led_indicator requires rail"})
            return
        led_net = str(circuit.get("net") or f"{circuit.get('name') or 'LED'}_K")
        self._add_two_pin_part("R", str(circuit.get("resistor") or "1k"), str(circuit.get("resistor_footprint") or DEFAULT_FOOTPRINTS["resistor"]), str(rail), led_net, "led_indicators", {"target": circuit.get("target"), "rail": str(rail)})
        led_ref = self._allocate("D", "led_indicators")
        self._add_part({"ref": led_ref, "lib_id": "Device:LED", "value": str(circuit.get("led_color") or "LED"), "footprint": str(circuit.get("led_footprint") or DEFAULT_FOOTPRINTS["led"]), "generated_by": "led_indicators", "target": circuit.get("target")})
        self._add_connection(led_net, led_ref, "2", path)
        self._add_connection(str(ground), led_ref, "1", path)

    def _support_connector_header(self, circuit: dict[str, Any], path: str) -> None:
        pins = circuit.get("pins", [])
        pin_count = int(circuit.get("pin_count") or len(pins) or 2)
        ref = str(circuit.get("ref") or self._allocate("J", "headers"))
        self._add_header_part(ref, pin_count, str(circuit.get("footprint") or _header_footprint(pin_count)), circuit.get("value") or circuit.get("name") or "HEADER", {"generated_by": "headers", "target": circuit.get("target")})
        if isinstance(pins, list):
            for index, net in enumerate(pins, start=1):
                if net:
                    self._add_connection(str(net), ref, str(index), path)

    def _support_test_point(self, circuit: dict[str, Any], path: str) -> None:
        net = circuit.get("net")
        if not net:
            self.errors.append({"path": path, "error": "test_point requires net"})
            return
        ref = str(circuit.get("ref") or self._allocate("TP", "test_points"))
        self._add_part({"ref": ref, "lib_id": "Connector:TestPoint", "value": str(circuit.get("value") or net), "footprint": str(circuit.get("footprint") or DEFAULT_FOOTPRINTS["test_point"]), "generated_by": "test_points", "target": circuit.get("target")})
        self._add_connection(str(net), ref, "1", path)

    def _support_power_flag(self, circuit: dict[str, Any], path: str) -> None:
        net = circuit.get("net") or circuit.get("rail")
        if not net:
            self.errors.append({"path": path, "error": "power_flag requires net or rail"})
            return
        ref = str(circuit.get("ref") or self._allocate("#FLG", "power_flags"))
        self._add_part({"ref": ref, "lib_id": "power:PWR_FLAG", "value": "PWR_FLAG"})
        self._add_connection(str(net), ref, "1", path)

    def _support_rc_filter(self, circuit: dict[str, Any], path: str) -> None:
        in_net = circuit.get("in_net")
        out_net = circuit.get("out_net")
        ground = circuit.get("ground", "GND")
        if not in_net or not out_net:
            self.errors.append({"path": path, "error": "rc_filter requires in_net and out_net"})
            return
        self._add_two_pin_part("R", str(circuit.get("resistor") or "100"), str(circuit.get("resistor_footprint") or DEFAULT_FOOTPRINTS["resistor"]), str(in_net), str(out_net), "rc_filters", {"target": circuit.get("target")})
        self._add_two_pin_part("C", str(circuit.get("capacitor") or "100n"), str(circuit.get("capacitor_footprint") or DEFAULT_FOOTPRINTS["capacitor"]), str(out_net), str(ground), "rc_filters", {"target": circuit.get("target")})

    def _support_ferrite_filter(self, circuit: dict[str, Any], path: str) -> None:
        in_net = circuit.get("in_net") or circuit.get("input_net") or circuit.get("rail")
        out_net = (
            circuit.get("out_net")
            or circuit.get("output_net")
            or circuit.get("filtered_net")
            or circuit.get("supply_rail")
            or circuit.get("net")
        )
        if not in_net or not out_net:
            self.errors.append({"path": path, "error": "ferrite_filter requires in_net/rail and out_net/supply_rail"})
            return
        self._add_two_pin_part("FB", str(circuit.get("value") or "Ferrite"), str(circuit.get("footprint") or DEFAULT_FOOTPRINTS["ferrite"]), str(in_net), str(out_net), "ferrite_filters", {"target": circuit.get("target")})

    def _support_crystal(self, circuit: dict[str, Any], path: str) -> None:
        ref = str(circuit.get("ref") or self._allocate("Y", "crystals"))
        pins = circuit.get("pins", [])
        if (not isinstance(pins, list) or len(pins) < 2) and circuit.get("xin") and circuit.get("xout"):
            pins = [circuit.get("xin"), circuit.get("xout")]
        if not isinstance(pins, list):
            pins = []
        if len(pins) < 2:
            self.errors.append({"path": path, "error": "crystal requires two nets in pins or xin/xout"})
            return
        lib_id = str(circuit.get("lib_id") or circuit.get("symbol") or "Device:Crystal")
        ground_pins = self._crystal_ground_pins(lib_id, circuit, path)
        if ground_pins is None:
            return
        self._add_part({"ref": ref, "lib_id": lib_id, "value": str(circuit.get("value") or "Crystal"), "footprint": circuit.get("footprint") or DEFAULT_FOOTPRINTS["crystal"], "generated_by": "crystals", "target": circuit.get("target")})
        self._add_connection(str(pins[0]), ref, "1", path)
        self._add_connection(str(pins[1]), ref, "2", path)
        ground_net = str(circuit.get("ground") or "GND")
        for pin in ground_pins:
            self._add_connection(ground_net, ref, pin, path)

    def _crystal_ground_pins(
        self,
        lib_id: str,
        circuit: dict[str, Any],
        path: str,
    ) -> list[str] | None:
        grounded_requested = bool(circuit.get("ground")) or "GND" in lib_id.upper()
        resolved_pins: list[dict[str, Any]] | None = None
        try:
            resolved_pins = _resolve_symbol_pins(lib_id)
        except Exception as exc:
            if self.strict:
                self.errors.append(
                    {
                        "path": f"{path}.lib_id",
                        "error": "unable to resolve crystal symbol pins",
                        "lib_id": lib_id,
                        "detail": str(exc),
                    }
                )
                return None
            self.warnings.append(
                {
                    "path": f"{path}.lib_id",
                    "warning": "unable to resolve crystal symbol pins; using KiCad crystal pin convention",
                    "lib_id": lib_id,
                    "detail": str(exc),
                }
            )

        if resolved_pins is not None:
            pin_numbers = {str(pin.get("number") or pin.get("pin") or "") for pin in resolved_pins}
            missing_signal_pins = [pin for pin in ("1", "2") if pin not in pin_numbers]
            if missing_signal_pins:
                self.errors.append(
                    {
                        "path": f"{path}.lib_id",
                        "error": "crystal symbol does not expose required signal pins",
                        "lib_id": lib_id,
                        "missing_pins": missing_signal_pins,
                    }
                )
                return None
            detected_ground_pins = [
                str(pin.get("number") or pin.get("pin"))
                for pin in resolved_pins
                if _pin_looks_like_ground(pin)
            ]
            if not detected_ground_pins and "GND2" in lib_id.upper() and {"3", "4"}.issubset(pin_numbers):
                detected_ground_pins = ["3", "4"]
            if grounded_requested and not detected_ground_pins:
                self.warnings.append(
                    {
                        "path": f"{path}.ground",
                        "warning": "crystal case ground requested but no ground/case pin found; generated 2-pin crystal",
                        "lib_id": lib_id,
                    }
                )
                return []
            return detected_ground_pins if grounded_requested else []

        if grounded_requested:
            if "GND2" in lib_id.upper():
                return ["3", "4"]
            self.errors.append(
                {
                    "path": f"{path}.ground",
                    "error": "grounded crystal requested but symbol pin topology could not be validated",
                    "lib_id": lib_id,
                }
            )
            return None
        return []

    def _support_esd_diode(self, circuit: dict[str, Any], path: str) -> None:
        net = circuit.get("net")
        ground = circuit.get("ground", "GND")
        if not net:
            self.errors.append({"path": path, "error": "esd_diode requires net"})
            return
        ref = str(circuit.get("ref") or self._allocate("D", "esd_diodes"))
        self._add_part({"ref": ref, "lib_id": str(circuit.get("lib_id") or "Diode:ESD5Zxx"), "value": str(circuit.get("value") or "ESD"), "footprint": circuit.get("footprint"), "generated_by": "esd_diodes", "target": circuit.get("target")})
        self._add_connection(str(net), ref, "1", path)
        self._add_connection(str(ground), ref, "2", path)

    def _expand_bulk_connections(self, bulk_connections: list[Any]) -> None:
        for index, item in enumerate(bulk_connections):
            path = f"bulk_connections[{index}]"
            if not isinstance(item, dict):
                self.errors.append({"path": path, "error": "bulk connection must be an object"})
                continue
            if item.get("type") in {"pin_to_net", "pin_to_power", "pin_to_ground"}:
                ref = item.get("ref")
                pin = item.get("pin")
                net = item.get("net") or ("GND" if item.get("type") == "pin_to_ground" else None)
                if not ref or not pin or not net:
                    self.errors.append({"path": path, "error": "pin_to_net bulk connection requires ref, pin, and net"})
                    continue
                self._add_connection(
                    str(net),
                    str(ref),
                    str(pin),
                    path,
                    allow_hidden_power=bool(item.get("allow_hidden_power", self.default_allow_hidden_power)),
                )
            elif item.get("type") == "pin_to_pin":
                start = item.get("from") or item.get("a")
                end = item.get("to") or item.get("b")
                start_endpoint = _endpoint(start)
                end_endpoint = _endpoint(end)
                if not start_endpoint or not end_endpoint:
                    self.errors.append({"path": path, "error": "pin_to_pin bulk connection requires from/to endpoint objects"})
                    continue
                net = str(item.get("net") or _auto_net_name(start_endpoint[0], start_endpoint[1], end_endpoint[0], end_endpoint[1]))
                allow_hidden_power = bool(item.get("allow_hidden_power", self.default_allow_hidden_power))
                self._add_connection(net, start_endpoint[0], start_endpoint[1], path, allow_hidden_power=allow_hidden_power)
                self._add_connection(net, end_endpoint[0], end_endpoint[1], path, allow_hidden_power=allow_hidden_power)
            elif item.get("net") and isinstance(item.get("pins"), list):
                for endpoint in item["pins"]:
                    parsed = _endpoint(endpoint)
                    if parsed:
                        self._add_connection(
                            str(item["net"]),
                            parsed[0],
                            parsed[1],
                            path,
                            allow_hidden_power=bool(
                                item.get(
                                    "allow_hidden_power",
                                    _endpoint_allow_hidden_power(endpoint) or self.default_allow_hidden_power,
                                )
                            ),
                        )
            elif item.get("net_prefix") and isinstance(item.get("map"), dict):
                prefix = str(item["net_prefix"])
                for suffix, endpoints in item["map"].items():
                    net = f"{prefix}_{suffix}"
                    if isinstance(endpoints, list):
                        for endpoint in endpoints:
                            parsed = _endpoint(endpoint)
                            if parsed:
                                self._add_connection(
                                    net,
                                    parsed[0],
                                    parsed[1],
                                    path,
                                    allow_hidden_power=bool(
                                        item.get(
                                            "allow_hidden_power",
                                            _endpoint_allow_hidden_power(endpoint) or self.default_allow_hidden_power,
                                        )
                                    ),
                                )
            else:
                self.errors.append({"path": path, "error": "bulk connection requires net/pins, net_prefix/map, or type=pin_to_net/pin_to_pin"})

    def _expand_no_connect_rules(self, rules: list[Any]) -> None:
        connected = set(self.pin_assignments)
        for index, rule in enumerate(rules):
            path = f"no_connect_rules[{index}]"
            if not isinstance(rule, dict):
                self.errors.append({"path": path, "error": "no-connect rule must be an object"})
                continue
            if rule.get("action", "mark_no_connect") != "mark_no_connect":
                self.errors.append({"path": path, "error": "unsupported no-connect action", "action": rule.get("action")})
                continue
            ref = str(rule.get("ref") or "")
            pins = self._select_rule_pins(ref, rule.get("match", {}), path)
            except_pins = {str(item) for item in rule.get("except", [])}
            include_hidden = bool(rule.get("include_hidden", False))
            allow_hidden_no_connect = bool(rule.get("allow_hidden_no_connect", False))
            for pin in pins:
                ident = self._pin_identifier(ref, pin)
                if ident in except_pins or pin.get("name") in except_pins or pin.get("number") in except_pins:
                    continue
                visibility = classify_pin(pin)
                if visibility != PinVisibility.VISIBLE and not include_hidden:
                    self.skipped_hidden_pins.append(
                        {
                            "ref": ref,
                            "pin": ident,
                            "name": pin.get("name"),
                            "visibility": visibility,
                            "reason": "hidden pins are skipped by no_connect_rules",
                        }
                    )
                    continue
                if (
                    visibility not in {PinVisibility.VISIBLE, PinVisibility.HIDDEN_NO_CONNECT}
                    and not allow_hidden_no_connect
                ):
                    self.errors.append(
                        {
                            "path": path,
                            "error": "hidden pin no-connect requires allow_hidden_no_connect",
                            "ref": ref,
                            "pin": ident,
                            "name": pin.get("name"),
                            "visibility": visibility,
                        }
                    )
                    continue
                if (ref, ident) in connected:
                    continue
                marker = {"ref": ref, "pin": ident}
                if allow_hidden_no_connect:
                    marker["allow_hidden_no_connect"] = True
                self.no_connects.append(marker)

    def _select_rule_pins(self, ref: str, selector: Any, path: str) -> list[dict[str, Any]]:
        if ref not in self.pin_maps:
            self.errors.append({"path": path, "error": "unknown ref", "ref": ref})
            return []
        if not isinstance(selector, dict):
            self.errors.append({"path": path, "error": "selector must be an object", "ref": ref})
            return []
        try:
            pins = select_pins(self.pin_maps[ref], selector)
        except re.error as exc:
            self.errors.append(
                {
                    "path": path,
                    "error": "invalid regex selector",
                    "ref": ref,
                    "selector": selector,
                    "detail": str(exc),
                }
            )
            return []
        if not pins:
            self.errors.append({"path": path, "error": "selector matched zero pins", "ref": ref, "selector": selector})
        return pins

    def _connect_named_pin(self, net: str, endpoint: dict[str, Any], key: str, path: str) -> None:
        ref = endpoint.get("ref")
        pin = endpoint.get(key)
        if not ref or not pin:
            self.errors.append({"path": f"{path}.{key}", "error": f"missing {key} pin or ref"})
            return
        self._add_connection(net, str(ref), str(pin), path)

    def _connect_pin_net_map(self, mapping: Any, ref: Any, path: str) -> None:
        if not isinstance(mapping, dict):
            return
        if not ref:
            self.errors.append({"path": path, "error": "pin/net map requires ref"})
            return
        for pin, net in mapping.items():
            self._add_connection(str(net), str(ref), str(pin), path)

    def _add_two_pin_part(self, prefix: str, value: str, footprint: str, net_1: str, net_2: str, bucket: str, metadata: dict[str, Any] | None = None) -> str:
        ref = self._allocate(prefix, bucket)
        symbol_template, _ = PASSIVE_SYMBOLS[prefix]
        part = {"ref": ref, "lib_id": symbol_template, "value": value, "footprint": footprint, "generated_by": bucket}
        if metadata:
            part.update({key: value for key, value in metadata.items() if value is not None})
        self._add_part(part)
        self._add_connection(net_1, ref, "1", bucket)
        self._add_connection(net_2, ref, "2", bucket)
        return ref

    def _add_header_part(self, ref: str, pin_count: int, footprint: str, value: Any, metadata: dict[str, Any] | None = None) -> None:
        lib_id = f"Connector_Generic:Conn_01x{pin_count:02d}"
        part = {"ref": ref, "lib_id": lib_id, "value": str(value), "footprint": footprint}
        if metadata:
            part.update({key: value for key, value in metadata.items() if value is not None})
        self._add_part(part)
        self._record_generated_ref("headers", ref)

    def _add_part(self, part: dict[str, Any]) -> None:
        ref = str(part.get("ref") or "")
        if not ref:
            self.errors.append({"path": "generated_parts", "error": "generated part requires ref"})
            return
        if ref in self.existing_refs:
            self.errors.append({"path": "generated_parts", "error": "generated ref collision", "ref": ref})
            return
        if ref in {str(item.get("ref")) for item in self.parts}:
            self.errors.append({"path": "generated_parts", "error": "generated ref collision", "ref": ref})
            return
        if not str(part.get("lib_id") or "").startswith("power:") and not part.get("footprint"):
            self.errors.append({"path": "generated_parts", "error": "missing footprint for generated part", "ref": ref})
            return
        if part.get("generated_by"):
            properties = dict(part.get("properties") or {})
            properties.setdefault("ki_mcp_generated_by", "kicad_mcp")
            properties.setdefault("ki_mcp_role", str(part.get("generated_by")))
            for key in ("target", "rail", "net"):
                if part.get(key) is not None:
                    properties.setdefault(f"ki_mcp_{key}", str(part[key]))
            part["properties"] = properties
        self.parts.append(part)
        self.allocator.used.add(ref)

    def _allocate(self, prefix: str, bucket: str) -> str:
        ref = self.allocator.next(prefix)
        self._record_generated_ref(bucket, ref)
        return ref

    def _record_generated_ref(self, bucket: str, ref: str) -> None:
        refs = self.generated_refs.setdefault(bucket, [])
        if ref not in refs:
            refs.append(ref)

    def _pin_identifier(self, ref: str, pin: dict[str, Any]) -> str:
        name = str(pin.get("name") or "")
        number = str(pin.get("number") or "")
        if name and self.pin_name_counts.get(ref, {}).get(name, 0) == 1:
            return name
        return number or name

    def _resolve_pin_identifier(
        self, ref: str, requested_pin: str, path: str
    ) -> tuple[str, dict[str, Any] | None] | None:
        pins = self.pin_maps.get(ref)
        if pins is None:
            return requested_pin, None
        matches = [
            pin
            for pin in pins
            if str(pin.get("number") or "") == requested_pin
            or str(pin.get("name") or "") == requested_pin
            or str(pin.get("pinfunction") or "") == requested_pin
        ]
        if not matches:
            self.errors.append(
                {
                    "path": path,
                    "error": "unknown pin",
                    "ref": ref,
                    "pin": requested_pin,
                }
            )
            return None
        if len(matches) > 1:
            self.errors.append(
                {
                    "path": path,
                    "error": "pin identifier is ambiguous",
                    "ref": ref,
                    "pin": requested_pin,
                    "suggestion": "Use pin_rules to connect all matching pins, or use a pin number.",
                    "example": {
                        "pin_rules": [
                            {"ref": ref, "match": {"pin": requested_pin}, "net": "<NET_NAME>"}
                        ]
                    },
                    "matches": [
                        {
                            "number": pin.get("number"),
                            "name": pin.get("name"),
                            "pinfunction": pin.get("pinfunction"),
                        }
                        for pin in matches
                    ],
                }
            )
            return None
        pin_info = matches[0]
        return self._pin_identifier(ref, pin_info), pin_info

    def _add_connection(
        self,
        net: str,
        ref: str,
        pin: str,
        path: str,
        pin_info: dict[str, Any] | None = None,
        allow_hidden_power: bool | None = None,
    ) -> None:
        if not net or not ref or not pin:
            self.errors.append({"path": path, "error": "connection requires net, ref, and pin"})
            return
        if ref not in self.pin_maps and ref not in {str(part.get("ref")) for part in self.parts}:
            self.errors.append({"path": path, "error": "unknown ref", "ref": ref})
            return
        resolved_pin_info = pin_info
        if pin_info is None:
            resolved = self._resolve_pin_identifier(ref, pin, path)
            if resolved is None:
                return
            pin, resolved_pin_info = resolved
        key = (ref, pin)
        existing = self.pin_assignments.get(key)
        if existing and existing != net:
            self.errors.append({"path": path, "error": "same ref/pin assigned to two different nets", "ref": ref, "pin": pin, "first_net": existing, "second_net": net})
            return
        self.pin_assignments[key] = net
        mismatch = _power_ground_mismatch(resolved_pin_info if resolved_pin_info is not None else {"name": pin, "number": pin}, net)
        if mismatch:
            self.errors.append({"path": path, **mismatch, "ref": ref, "pin": pin, "net": net})
            return
        allow_hidden = bool(self.default_allow_hidden_power if allow_hidden_power is None else allow_hidden_power)
        visibility = classify_pin(resolved_pin_info) if resolved_pin_info is not None else PinVisibility.VISIBLE
        if visibility == PinVisibility.HIDDEN_POWER:
            if allow_hidden or _net_looks_like_power_or_ground(net):
                allow_hidden = True
            else:
                self.errors.append(
                    {
                        "path": path,
                        "error": "hidden power pin connection requires allow_hidden_power",
                        "ref": ref,
                        "pin": pin,
                        "net": net,
                    }
                )
                return
        endpoints = self.nets.setdefault(net, [])
        endpoint: Any = (
            {"ref": ref, "pin": pin, "allow_hidden_power": True}
            if allow_hidden and visibility == PinVisibility.HIDDEN_POWER
            else [ref, pin]
        )
        if endpoint not in endpoints:
            endpoints.append(endpoint)

    def _validate_generated_refs(self) -> None:
        seen: set[str] = set()
        for part in self.parts:
            ref = str(part.get("ref") or "")
            if not ref:
                continue
            if ref in seen:
                self.errors.append({"path": "parts", "error": "generated ref collision", "ref": ref})
            seen.add(ref)

    def _save_artifacts(self, normalized: dict[str, Any], expanded_spec: dict[str, Any], report: dict[str, Any]) -> dict[str, str]:
        try:
            base = _artifact_dir(self.project_path)
            base.mkdir(parents=True, exist_ok=True)
            normalized_path = base / "design_intent.normalized.json"
            expanded_path = base / "design_intent.expanded_spec.json"
            report_path = base / "design_intent.report.json"
            atomic_write_text(normalized_path, json.dumps(normalized, indent=2, sort_keys=True))
            atomic_write_text(expanded_path, json.dumps(expanded_spec, indent=2, sort_keys=True))
            compact_report = {key: value for key, value in report.items() if key != "expanded_spec"}
            atomic_write_text(report_path, json.dumps(compact_report, indent=2, sort_keys=True))
            return {
                "normalized_intent_path": str(normalized_path),
                "expanded_spec_path": str(expanded_path),
                "report_path": str(report_path),
            }
        except (OSError, PathValidationError) as exc:
            self.warnings.append({"path": ".kicad_mcp", "warning": "unable to save design intent artifacts", "detail": str(exc)})
            return {}


def _pin_matches(pin: dict[str, Any], selector: dict[str, Any]) -> bool:
    if not selector:
        return True
    for key, expected in selector.items():
        if key == "pin":
            values = _pin_values(pin)
            if str(expected) not in values:
                return False
        elif key == "name":
            if str(pin.get("name") or "") != str(expected):
                return False
        elif key == "number":
            if str(pin.get("number") or "") != str(expected):
                return False
        elif key == "pins":
            expected_values = {str(item) for item in expected} if isinstance(expected, list) else {str(expected)}
            if not expected_values.intersection(_pin_values(pin)):
                return False
        elif key == "names":
            expected_values = {str(item) for item in expected} if isinstance(expected, list) else {str(expected)}
            if str(pin.get("name") or "") not in expected_values:
                return False
        elif key == "numbers":
            expected_values = {str(item) for item in expected} if isinstance(expected, list) else {str(expected)}
            if str(pin.get("number") or "") not in expected_values:
                return False
        elif key == "name_regex":
            if not re.search(str(expected), str(pin.get("name") or "")):
                return False
        elif key == "number_regex":
            if not re.search(str(expected), str(pin.get("number") or "")):
                return False
        elif key == "pin_type":
            if str(pin.get("pintype") or pin.get("type") or "").lower() != str(expected).lower():
                return False
        elif key == "name_contains":
            if str(expected).lower() not in str(pin.get("name") or "").lower():
                return False
        else:
            return False
    return True


def _pin_values(pin: dict[str, Any]) -> set[str]:
    return {str(value) for value in (pin.get("name"), pin.get("number"), pin.get("pinfunction")) if value}


def _pin_name_counts(pins: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for pin in pins:
        name = str(pin.get("name") or "")
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def _pin_looks_like_ground(pin: dict[str, Any]) -> bool:
    tokens = {
        str(pin.get("name") or ""),
        str(pin.get("pinfunction") or ""),
        str(pin.get("function") or ""),
    }
    return any(token.upper() in {"GND", "VSS", "GNDA", "DGND"} for token in tokens)


def _power_ground_mismatch(pin: dict[str, Any], net: str) -> dict[str, Any] | None:
    name = str(pin.get("name") or pin.get("number") or "").upper()
    net_upper = net.upper()
    ground_pin = bool(re.search(r"(^|[^A-Z])(GND|VSS|VSSA|AGND|DGND)([^A-Z]|$)", name))
    power_pin = bool(re.search(r"(^|[^A-Z])(VDD|VDDA|VCC|VBAT|VBUS|VIN|VOUT|3V3|5V)([^A-Z]|$)", name))
    ground_net = net_upper in {"GND", "AGND", "DGND", "VSS"} or net_upper.endswith("_GND")
    if ground_pin and not ground_net:
        return {"error": "ground-looking pin connected to non-ground net"}
    if power_pin and ground_net:
        return {"error": "power-looking pin connected to GND"}
    return None


def _net_looks_like_power_or_ground(net: str) -> bool:
    normalized = str(net or "").upper()
    if normalized in {"GND", "AGND", "DGND", "GNDA", "GNDD", "VSS", "VSSA", "VCC", "VDD", "VDDA", "VBAT", "VBUS"}:
        return True
    if normalized.startswith("+"):
        return True
    return bool(re.search(r"(^|_)(GND|VSS|VCC|VDD|VBAT|VBUS|3V3|5V)(_|$)", normalized))


def _endpoint(item: Any) -> tuple[str, str] | None:
    if isinstance(item, dict):
        ref = item.get("ref") or item.get("reference")
        pin = item.get("pin") or item.get("pin_number") or item.get("pin_name")
    elif isinstance(item, list | tuple) and len(item) >= 2:
        ref, pin = item[0], item[1]
    else:
        return None
    if not ref or not pin:
        return None
    return str(ref), str(pin)


def _endpoint_allow_hidden_power(item: Any) -> bool:
    return isinstance(item, dict) and bool(item.get("allow_hidden_power", False))


def _auto_net_name(ref_a: str, pin_a: str, ref_b: str, pin_b: str) -> str:
    safe = "_".join([ref_a, pin_a, ref_b, pin_b])
    return "".join(char if char.isalnum() else "_" for char in safe).upper()


def _empty_intent() -> dict[str, Any]:
    return {
        "name": None,
        "paper": None,
        "parts": [],
        "rails": {},
        "pin_rules": [],
        "interfaces": [],
        "support_circuits": [],
        "bulk_connections": [],
        "no_connect_rules": [],
        "layout_hints": {},
        "allow_hidden_power": False,
    }


def _sorted_nets(nets: dict[str, list[Any]]) -> dict[str, list[Any]]:
    return dict(sorted(nets.items(), key=lambda item: item[0]))


def _header_footprint(pin_count: int) -> str:
    key = f"header_1x{pin_count:02d}"
    return DEFAULT_FOOTPRINTS.get(key, f"Connector_PinHeader_2.54mm:PinHeader_1x{pin_count:02d}_P2.54mm_Vertical")


def _existing_schematic_refs(project_path: str) -> list[str]:
    schematic_path = _schematic_path_if_exists(project_path)
    if schematic_path is None:
        return []
    try:
        schematic = KiCadSchematic.from_file(schematic_path)
    except Exception:
        return []
    return [str(symbol.get("reference")) for symbol in schematic.list_symbols() if symbol.get("reference")]


def _schematic_path_if_exists(project_path: str) -> str | None:
    path = Path(project_path)
    if path.suffix == ".kicad_sch" and path.exists():
        return str(path)
    if path.suffix == ".kicad_pro" and path.exists():
        try:
            files = get_project_files(str(path))
        except Exception:
            return None
        schematic = files.get("schematic")
        if schematic and Path(schematic).exists():
            return schematic
    return None


def _artifact_dir(project_path: str) -> Path:
    validated = get_configured_validator().validate_path(project_path, must_exist=False)
    path = Path(validated)
    if path.suffix in {".kicad_pro", ".kicad_sch"}:
        return path.parent / ".kicad_mcp"
    return path / ".kicad_mcp"
