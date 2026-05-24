"""Generic bulk design-intent compiler for schematic generation."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any

from kicad_mcp.utils.file_utils import get_project_files
from kicad_mcp.utils.kicad_s_expr import KiCadSchematic
from kicad_mcp.utils.schematic_pins import _resolve_symbol_pins

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
        self.nets: dict[str, list[list[str]]] = {}
        self.no_connects: list[dict[str, str]] = []
        self.generated_refs: dict[str, list[str]] = {}
        self.pin_maps: dict[str, list[dict[str, Any]]] = {}
        self.pin_name_counts: dict[str, dict[str, int]] = {}
        self.pin_assignments: dict[tuple[str, str], str] = {}
        self.existing_refs = _existing_schematic_refs(project_path)
        self.allocator = ReferenceAllocator(self.existing_refs)

    def compile(self) -> dict[str, Any]:
        normalized = self._normalize_intent()
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
        summary = {
            "input_part_count": len(normalized["parts"]),
            "generated_part_count": max(len(self.parts) - len(normalized["parts"]), 0),
            "total_part_count": len(self.parts),
            "connection_count": sum(len(pins) for pins in self.nets.values()),
            "net_count": len(self.nets),
            "no_connect_count": len(self.no_connects),
        }
        success = not self.errors
        result = {
            "success": success,
            "expanded_spec": expanded_spec,
            "summary": summary,
            "generated_refs": self.generated_refs,
            "warnings": self.warnings,
            "errors": self.errors,
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
        for key in (
            "parts",
            "pin_rules",
            "interfaces",
            "support_circuits",
            "bulk_connections",
            "no_connect_rules",
        ):
            if not isinstance(normalized[key], list):
                self.errors.append({"path": key, "error": f"{key} must be a list"})
                normalized[key] = []
        if not isinstance(normalized["rails"], dict):
            self.errors.append({"path": "rails", "error": "rails must be an object"})
            normalized["rails"] = {}
        if not isinstance(normalized["layout_hints"], dict):
            self.errors.append({"path": "layout_hints", "error": "layout_hints must be an object"})
            normalized["layout_hints"] = {}
        return normalized

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
                    self._add_connection(str(rail_name), parsed[0], parsed[1], "rails")

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
            for pin in pins:
                self._add_connection(net, ref, self._pin_identifier(ref, pin), path, pin_info=pin)

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
                self._add_two_pin_part("R", value, footprint, scl_net, str(rail), "i2c_pullups")
                self._add_two_pin_part("R", value, footprint, sda_net, str(rail), "i2c_pullups")

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
        self._add_header_part(ref, pin_count, str(header.get("footprint") or _header_footprint(pin_count)), header.get("value") or "SWD")
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
            self._add_two_pin_part("C", value_str, footprint, str(rail), str(ground), "decoupling")

    def _support_pullup(self, circuit: dict[str, Any], path: str) -> None:
        self._support_resistor_to_rail(circuit, path, "rail", "pullups")

    def _support_pulldown(self, circuit: dict[str, Any], path: str) -> None:
        self._support_resistor_to_rail(circuit, path, "ground", "pulldowns", default_rail="GND")

    def _support_resistor_to_rail(self, circuit: dict[str, Any], path: str, rail_key: str, bucket: str, default_rail: str | None = None) -> None:
        net = circuit.get("net")
        rail = circuit.get(rail_key) or default_rail
        if not net or not rail:
            self.errors.append({"path": path, "error": f"{bucket[:-1]} requires net and {rail_key}"})
            return
        self._add_two_pin_part(
            "R",
            str(circuit.get("value") or "10k"),
            str(circuit.get("footprint") or DEFAULT_FOOTPRINTS["resistor"]),
            str(net),
            str(rail),
            bucket,
        )

    def _support_series_resistor(self, circuit: dict[str, Any], path: str) -> None:
        in_net = circuit.get("in_net") or circuit.get("from")
        out_net = circuit.get("out_net") or circuit.get("to")
        if not in_net or not out_net:
            self.errors.append({"path": path, "error": "series_resistor requires in_net/from and out_net/to"})
            return
        self._add_two_pin_part("R", str(circuit.get("value") or "0"), str(circuit.get("footprint") or DEFAULT_FOOTPRINTS["resistor"]), str(in_net), str(out_net), "series_resistors")

    def _support_reset_button(self, circuit: dict[str, Any], path: str) -> None:
        target = circuit.get("target")
        pin = circuit.get("pin")
        net = str(circuit.get("net") or "RESET_N")
        if target and pin:
            self._add_connection(net, str(target), str(pin), path)
        if circuit.get("pullup") and circuit.get("rail"):
            self._add_two_pin_part("R", str(circuit["pullup"]), str(circuit.get("resistor_footprint") or DEFAULT_FOOTPRINTS["resistor"]), net, str(circuit["rail"]), "reset")
        sw_ref = str(circuit.get("ref") or self._allocate("SW", "reset"))
        self._add_part({"ref": sw_ref, "lib_id": "Switch:SW_Push", "value": str(circuit.get("value") or "RESET"), "footprint": str(circuit.get("footprint") or DEFAULT_FOOTPRINTS["switch"])})
        self._add_connection(net, sw_ref, "1", path)
        self._add_connection(str(circuit.get("ground") or "GND"), sw_ref, "2", path)

    def _support_led_indicator(self, circuit: dict[str, Any], path: str) -> None:
        rail = circuit.get("rail")
        ground = circuit.get("ground", "GND")
        if not rail:
            self.errors.append({"path": path, "error": "led_indicator requires rail"})
            return
        led_net = str(circuit.get("net") or f"{circuit.get('name') or 'LED'}_K")
        self._add_two_pin_part("R", str(circuit.get("resistor") or "1k"), str(circuit.get("resistor_footprint") or DEFAULT_FOOTPRINTS["resistor"]), str(rail), led_net, "led_indicators")
        led_ref = self._allocate("D", "led_indicators")
        self._add_part({"ref": led_ref, "lib_id": "Device:LED", "value": str(circuit.get("led_color") or "LED"), "footprint": str(circuit.get("led_footprint") or DEFAULT_FOOTPRINTS["led"])})
        self._add_connection(led_net, led_ref, "2", path)
        self._add_connection(str(ground), led_ref, "1", path)

    def _support_connector_header(self, circuit: dict[str, Any], path: str) -> None:
        pins = circuit.get("pins", [])
        pin_count = int(circuit.get("pin_count") or len(pins) or 2)
        ref = str(circuit.get("ref") or self._allocate("J", "headers"))
        self._add_header_part(ref, pin_count, str(circuit.get("footprint") or _header_footprint(pin_count)), circuit.get("value") or circuit.get("name") or "HEADER")
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
        self._add_part({"ref": ref, "lib_id": "Connector:TestPoint", "value": str(circuit.get("value") or net), "footprint": str(circuit.get("footprint") or DEFAULT_FOOTPRINTS["test_point"])})
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
        self._add_two_pin_part("R", str(circuit.get("resistor") or "100"), str(circuit.get("resistor_footprint") or DEFAULT_FOOTPRINTS["resistor"]), str(in_net), str(out_net), "rc_filters")
        self._add_two_pin_part("C", str(circuit.get("capacitor") or "100n"), str(circuit.get("capacitor_footprint") or DEFAULT_FOOTPRINTS["capacitor"]), str(out_net), str(ground), "rc_filters")

    def _support_ferrite_filter(self, circuit: dict[str, Any], path: str) -> None:
        in_net = circuit.get("in_net")
        out_net = circuit.get("out_net")
        if not in_net or not out_net:
            self.errors.append({"path": path, "error": "ferrite_filter requires in_net and out_net"})
            return
        self._add_two_pin_part("FB", str(circuit.get("value") or "Ferrite"), str(circuit.get("footprint") or DEFAULT_FOOTPRINTS["ferrite"]), str(in_net), str(out_net), "ferrite_filters")

    def _support_crystal(self, circuit: dict[str, Any], path: str) -> None:
        ref = str(circuit.get("ref") or self._allocate("Y", "crystals"))
        pins = circuit.get("pins", [])
        if len(pins) < 2:
            self.errors.append({"path": path, "error": "crystal requires two nets in pins"})
            return
        self._add_part({"ref": ref, "lib_id": "Device:Crystal", "value": str(circuit.get("value") or "Crystal"), "footprint": circuit.get("footprint")})
        self._add_connection(str(pins[0]), ref, "1", path)
        self._add_connection(str(pins[1]), ref, "2", path)

    def _support_esd_diode(self, circuit: dict[str, Any], path: str) -> None:
        net = circuit.get("net")
        ground = circuit.get("ground", "GND")
        if not net:
            self.errors.append({"path": path, "error": "esd_diode requires net"})
            return
        ref = str(circuit.get("ref") or self._allocate("D", "esd_diodes"))
        self._add_part({"ref": ref, "lib_id": str(circuit.get("lib_id") or "Diode:ESD5Zxx"), "value": str(circuit.get("value") or "ESD"), "footprint": circuit.get("footprint")})
        self._add_connection(str(net), ref, "1", path)
        self._add_connection(str(ground), ref, "2", path)

    def _expand_bulk_connections(self, bulk_connections: list[Any]) -> None:
        for index, item in enumerate(bulk_connections):
            path = f"bulk_connections[{index}]"
            if not isinstance(item, dict):
                self.errors.append({"path": path, "error": "bulk connection must be an object"})
                continue
            if item.get("net") and isinstance(item.get("pins"), list):
                for endpoint in item["pins"]:
                    parsed = _endpoint(endpoint)
                    if parsed:
                        self._add_connection(str(item["net"]), parsed[0], parsed[1], path)
            elif item.get("net_prefix") and isinstance(item.get("map"), dict):
                prefix = str(item["net_prefix"])
                for suffix, endpoints in item["map"].items():
                    net = f"{prefix}_{suffix}"
                    if isinstance(endpoints, list):
                        for endpoint in endpoints:
                            parsed = _endpoint(endpoint)
                            if parsed:
                                self._add_connection(net, parsed[0], parsed[1], path)
            else:
                self.errors.append({"path": path, "error": "bulk connection requires net/pins or net_prefix/map"})

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
            for pin in pins:
                ident = self._pin_identifier(ref, pin)
                if ident in except_pins or pin.get("name") in except_pins or pin.get("number") in except_pins:
                    continue
                if (ref, ident) in connected:
                    continue
                self.no_connects.append({"ref": ref, "pin": ident})

    def _select_rule_pins(self, ref: str, selector: Any, path: str) -> list[dict[str, Any]]:
        if ref not in self.pin_maps:
            self.errors.append({"path": path, "error": "unknown ref", "ref": ref})
            return []
        if not isinstance(selector, dict):
            self.errors.append({"path": path, "error": "selector must be an object", "ref": ref})
            return []
        pins = select_pins(self.pin_maps[ref], selector)
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

    def _add_two_pin_part(self, prefix: str, value: str, footprint: str, net_1: str, net_2: str, bucket: str) -> str:
        ref = self._allocate(prefix, bucket)
        symbol_template, _ = PASSIVE_SYMBOLS[prefix]
        self._add_part({"ref": ref, "lib_id": symbol_template, "value": value, "footprint": footprint})
        self._add_connection(net_1, ref, "1", bucket)
        self._add_connection(net_2, ref, "2", bucket)
        return ref

    def _add_header_part(self, ref: str, pin_count: int, footprint: str, value: Any) -> None:
        lib_id = f"Connector_Generic:Conn_01x{pin_count:02d}"
        self._add_part({"ref": ref, "lib_id": lib_id, "value": str(value), "footprint": footprint})
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

    def _add_connection(self, net: str, ref: str, pin: str, path: str, pin_info: dict[str, Any] | None = None) -> None:
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
        endpoints = self.nets.setdefault(net, [])
        endpoint = [ref, pin]
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
            normalized_path.write_text(json.dumps(normalized, indent=2, sort_keys=True), encoding="utf-8")
            expanded_path.write_text(json.dumps(expanded_spec, indent=2, sort_keys=True), encoding="utf-8")
            compact_report = {key: value for key, value in report.items() if key != "expanded_spec"}
            report_path.write_text(json.dumps(compact_report, indent=2, sort_keys=True), encoding="utf-8")
            return {
                "normalized_intent_path": str(normalized_path),
                "expanded_spec_path": str(expanded_path),
                "report_path": str(report_path),
            }
        except Exception as exc:
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
        elif key == "pins":
            expected_values = {str(item) for item in expected} if isinstance(expected, list) else {str(expected)}
            if not expected_values.intersection(_pin_values(pin)):
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


def _power_ground_mismatch(pin: dict[str, Any], net: str) -> dict[str, Any] | None:
    name = str(pin.get("name") or pin.get("number") or "").upper()
    net_upper = net.upper()
    ground_pin = bool(re.search(r"(^|[^A-Z])(GND|VSS|VSSA|AGND|DGND)([^A-Z]|$)", name))
    power_pin = bool(re.search(r"(^|[^A-Z])(VDD|VDDA|VCC|VBAT|VIN|VOUT|3V3|5V)([^A-Z]|$)", name))
    ground_net = net_upper in {"GND", "AGND", "DGND", "VSS"} or net_upper.endswith("_GND")
    if ground_pin and not ground_net:
        return {"error": "ground-looking pin connected to non-ground net"}
    if power_pin and ground_net:
        return {"error": "power-looking pin connected to GND"}
    return None


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


def _empty_intent() -> dict[str, Any]:
    return {
        "name": None,
        "parts": [],
        "rails": {},
        "pin_rules": [],
        "interfaces": [],
        "support_circuits": [],
        "bulk_connections": [],
        "no_connect_rules": [],
        "layout_hints": {},
    }


def _sorted_nets(nets: dict[str, list[list[str]]]) -> dict[str, list[list[str]]]:
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
    path = Path(project_path)
    if path.suffix in {".kicad_pro", ".kicad_sch"}:
        return path.parent / ".kicad_mcp"
    return path / ".kicad_mcp"
