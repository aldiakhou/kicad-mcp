"""Intent-first schematic wiring helpers.

This module is the shared engine for agent-facing schematic connection tools.
It accepts electrical intent, computes pin locations from the schematic model,
snaps generated geometry to the schematic grid, and verifies the result with
KiCad's native netlist/ERC when available.
"""

from __future__ import annotations

from typing import Any

from kicad_mcp.utils.kicad_s_expr import KiCadSchematic, SExprAtom, SExprList
from kicad_mcp.utils.native_netlist import export_native_netlist, run_erc_via_cli
from kicad_mcp.utils.schematic_pins import (
    SCHEMATIC_GRID_MM,
    add_no_connect_to_pin,
    attach_net_to_pin,
)
from kicad_mcp.utils.transactional_edit import apply_transactional_schematic_edit


def connect_pin_to_net(
    schematic: KiCadSchematic,
    schematic_path: str,
    reference: str,
    pin: str,
    net_name: str,
    *,
    label_type: str = "global",
    stub_length_mm: float = 5.08,
    direction: str = "auto",
    allow_hidden_power: bool = False,
    label_placement: str = "pin_anchor",
    label_clearance_mm: float = 5.08,
    connection_style: str = "label",
) -> dict[str, Any]:
    """Connect one symbol pin to a named net using pin-resolved label placement."""
    if direction != "auto":
        # Direction is reserved for future explicit stub routing. Current pin
        # labels use the symbol pin's native orientation.
        direction = "auto"
    result = attach_net_to_pin(
        schematic,
        schematic_path,
        reference,
        pin,
        net_name,
        label_type,
        stub_length_mm,
        allow_hidden_power,
        label_placement=label_placement,
        label_clearance_mm=label_clearance_mm,
        connection_style=connection_style,
    )
    result["direction"] = direction
    return result


def connect_pins(
    schematic: KiCadSchematic,
    schematic_path: str,
    ref_a: str,
    pin_a: str,
    ref_b: str,
    pin_b: str,
    *,
    net_name: str | None = None,
    style: str = "auto",
) -> dict[str, Any]:
    """Connect two symbol pins by assigning both pins to the same named net."""
    resolved_net = net_name or _auto_net_name(ref_a, pin_a, ref_b, pin_b)
    return {
        "net_name": resolved_net,
        "style": style,
        "connections": [
            connect_pin_to_net(
                schematic,
                schematic_path,
                ref_a,
                pin_a,
                resolved_net,
            ),
            connect_pin_to_net(
                schematic,
                schematic_path,
                ref_b,
                pin_b,
                resolved_net,
            ),
        ],
    }


def apply_connection_plan_v2(
    schematic_path: str,
    connections: list[dict[str, Any]],
    no_connects: list[dict[str, Any]] | None = None,
    *,
    verify_native_netlist: bool = True,
    run_erc: bool = True,
    auto_snap: bool = True,
    rollback_on_failure: bool = True,
    fail_on_erc_violations: bool = False,
) -> dict[str, Any]:
    """Apply normalized electrical-intent connections transactionally."""
    no_connects = no_connects or []
    normalized = normalize_connections(connections)
    normalized_no_connects = normalize_no_connects(no_connects)
    if normalized["failed_connections"]:
        return _failure_response(
            schematic_path,
            normalized["failed_connections"],
            "Connection plan contains unsupported or malformed entries",
            rolled_back=False,
        )
    if normalized_no_connects["failed_no_connects"]:
        return _failure_response(
            schematic_path,
            normalized_no_connects["failed_no_connects"],
            "No-connect plan contains unsupported or malformed entries",
            rolled_back=False,
        )
    sanity = validate_normalized_plan(normalized["connections"])
    if not sanity["success"]:
        return _failure_response(
            schematic_path,
            sanity.get("failed_connections", []),
            "Connection plan failed preflight sanity checks",
            rolled_back=False,
            debug={"plan_sanity": sanity},
        )

    applied_connections: list[dict[str, Any]] = []
    applied_no_connects: list[dict[str, Any]] = []
    snap_summary: dict[str, Any] = {}

    def mutate(schematic: KiCadSchematic) -> dict[str, Any]:
        nonlocal snap_summary
        if auto_snap:
            snap_summary = snap_schematic_to_grid_model(schematic)
        for connection in normalized["connections"]:
            applied = _apply_normalized_connection(schematic, schematic_path, connection)
            applied_connections.append(applied)
        for marker in normalized_no_connects["no_connects"]:
            applied_no_connects.append(
                add_no_connect_to_pin(
                    schematic,
                    schematic_path,
                    marker["ref"],
                    marker["pin"],
                    allow_hidden_no_connect=marker.get("allow_hidden_no_connect", False),
                )
            )
        return {
            "connections": applied_connections,
            "applied_connections": normalized["connections"],
            "no_connects": applied_no_connects,
            "plan_summary": {
                "connection_count": len(normalized["connections"]),
                "required_connection_count": sum(
                    1 for connection in normalized["connections"] if connection.get("required", True)
                ),
                "optional_connection_count": sum(
                    1 for connection in normalized["connections"] if not connection.get("required", True)
                ),
                "no_connect_count": len(applied_no_connects),
            },
            "snap": snap_summary,
        }

    def post_write(path: str) -> dict[str, Any]:
        return verify_connection_plan_v2(
            path,
            normalized["connections"],
            verify_native_netlist=verify_native_netlist,
            run_erc=run_erc,
            fail_on_erc_violations=fail_on_erc_violations,
        )

    result = apply_transactional_schematic_edit(
        schematic_path,
        mutate,
        run_cli_validation=True,
        post_write_validator=post_write if rollback_on_failure else None,
    )

    if result.get("success"):
        native_verification = result.get("validation", {}).get("post_write", {}).get(
            "native_verification",
            {"success": True, "skipped": not verify_native_netlist, "missing": []},
        )
        erc = result.get("validation", {}).get("post_write", {}).get(
            "erc",
            {"success": True, "skipped": not run_erc},
        )
        if not rollback_on_failure:
            verification = verify_connection_plan_v2(
                schematic_path,
                normalized["connections"],
                verify_native_netlist=verify_native_netlist,
                run_erc=run_erc,
                fail_on_erc_violations=fail_on_erc_violations,
            )
            result.setdefault("validation", {})["post_write"] = verification
            native_verification = verification.get("native_verification", native_verification)
            erc = verification.get("erc", erc)
        return {
            **result,
            "tool": "schematic_apply_connection_plan",
            "stage": "schematic_wiring",
            "changed": True,
            "applied_connections": normalized["connections"],
            "failed_connections": [],
            "native_verification": native_verification,
            "erc": erc,
            "warnings": _verification_warnings(native_verification, erc),
            "recommended_next_tool": "schematic_quality_report",
            "recommended_next_arguments": {"project_path": schematic_path},
        }

    failed = _missing_from_failed_transaction(schematic_path, normalized["connections"])
    return {
        **result,
        "tool": "schematic_apply_connection_plan",
        "stage": "schematic_wiring",
        "changed": False,
        "failed_connections": failed or normalized["connections"],
        "recommended_next_tool": "schematic_quality_report",
        "recommended_next_arguments": {"project_path": schematic_path},
        "recoverable": True,
    }


def normalize_connections(connections: list[dict[str, Any]]) -> dict[str, Any]:
    """Normalize v1 and v2 connection formats into pin_to_net operations."""
    normalized = []
    failed = []
    for index, connection in enumerate(connections):
        try:
            normalized.extend(_normalize_connection(connection))
        except Exception as exc:
            failed.append({"index": index, "connection": connection, "reason": str(exc)})
    return {"connections": normalized, "failed_connections": failed}


def normalize_no_connects(no_connects: list[Any]) -> dict[str, Any]:
    """Normalize dict and tuple/list no-connect declarations."""
    normalized: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for index, marker in enumerate(no_connects):
        if isinstance(marker, list | tuple) and len(marker) >= 2:
            normalized.append({"ref": str(marker[0]), "pin": str(marker[1])})
        elif isinstance(marker, dict) and marker.get("ref") and marker.get("pin"):
            normalized.append(
                {
                    "ref": str(marker["ref"]),
                    "pin": str(marker["pin"]),
                    "allow_hidden_power": bool(marker.get("allow_hidden_power", False)),
                    "allow_hidden_no_connect": bool(marker.get("allow_hidden_no_connect", False)),
                }
            )
        else:
            failed.append({"index": index, "no_connect": marker, "reason": "Expected [ref, pin] or {ref, pin}"})
    return {"no_connects": normalized, "failed_no_connects": failed}


def validate_normalized_plan(connections: list[dict[str, Any]]) -> dict[str, Any]:
    """Catch malformed and conflicting pin-net intent before writing."""
    malformed = []
    conflicts = []
    seen: dict[tuple[str, str], str] = {}
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
                    "connection": connection,
                    "reason": f"{ref}.{pin} is assigned to both {seen[key]} and {net}",
                }
            )
        seen[key] = str(net)
    return {
        "success": not malformed and not conflicts,
        "malformed": malformed,
        "conflicts": conflicts,
        "failed_connections": malformed + conflicts,
    }


def verify_connection_plan_v2(
    schematic_path: str,
    connections: list[dict[str, Any]],
    *,
    verify_native_netlist: bool = True,
    run_erc: bool = True,
    fail_on_erc_violations: bool = True,
) -> dict[str, Any]:
    """Verify planned connections with native netlist and optional ERC."""
    native_result = (
        verify_native_memberships(schematic_path, connections)
        if verify_native_netlist
        else {"success": True, "skipped": True, "missing": []}
    )
    erc_result = run_erc_via_cli(schematic_path) if run_erc else {"success": True, "skipped": True}
    erc_blocking = bool(
        erc_result.get("success")
        and fail_on_erc_violations
        and erc_result.get("total_violations", 0) > 0
    )
    success = bool(native_result.get("success")) and not erc_blocking
    if run_erc and not erc_result.get("success"):
        # ERC unavailable is reported but not treated as destructive-edit failure;
        # native netlist verification remains the rollback gate.
        success = bool(native_result.get("success"))
    return {
        "success": success,
        "native_verification": native_result,
        "erc": {
            "success": erc_result.get("success"),
            "total_violations": erc_result.get("total_violations", 0),
            "violation_categories": erc_result.get("violation_categories", {}),
            "blocking_categories": erc_result.get("violation_categories", {})
            if erc_blocking
            else {},
            "error": erc_result.get("error"),
            "skipped": erc_result.get("skipped", False),
        },
        "reason": _verification_reason(native_result, erc_result, erc_blocking),
    }


def verify_native_memberships(
    schematic_path: str, connections: list[dict[str, Any]]
) -> dict[str, Any]:
    """Verify every required connection against KiCad's native netlist."""
    native = export_native_netlist(schematic_path)
    if not native.get("success"):
        return {"success": False, "reason": native.get("error"), "missing": [], "native_netlist": native}
    missing = []
    optional_missing = []
    checked_count = 0
    for connection in connections:
        if str(connection["ref"]).startswith("#"):
            continue
        checked_count += 1
        if not _membership_from_native(native, connection["ref"], connection["pin"], connection["net"]):
            missing_item = {
                "connection": connection,
                "reason": f"Pin {connection['ref']}.{connection['pin']} could not be verified on net {connection['net']}",
            }
            if connection.get("required", True):
                missing.append(missing_item)
            else:
                optional_missing.append(missing_item)
    return {
        "success": not missing,
        "reason": "all required planned connections verified"
        if not missing
        else "missing required native netlist memberships",
        "missing": missing,
        "optional_missing": optional_missing,
        "checked_count": checked_count,
        "native_netlist": {
            "success": native.get("success"),
            "component_count": native.get("component_count"),
            "net_count": native.get("net_count"),
            "connectivity_complete": native.get("connectivity_complete"),
            "netlist_quality": native.get("netlist_quality"),
        },
    }


def snap_schematic_to_grid_model(
    schematic: KiCadSchematic,
    grid_mm: float = SCHEMATIC_GRID_MM,
    *,
    include_symbols: bool = True,
    include_labels: bool = True,
    include_wires: bool = True,
) -> dict[str, Any]:
    """Snap schematic model coordinates in-place."""
    changed: list[dict[str, Any]] = []
    label_heads = {"label", "global_label", "hierarchical_label"}
    for item in schematic.root.items:
        if not isinstance(item, SExprList):
            continue
        head = item.head()
        if include_symbols and head == "symbol":
            changed.extend(_snap_at(item, grid_mm, "symbol", _symbol_reference(item)))
            for prop in item.child_lists("property"):
                changed.extend(_snap_at(prop, grid_mm, "property", _property_name(prop)))
        elif include_labels and head in label_heads:
            changed.extend(_snap_at(item, grid_mm, "label", _label_text(item)))
        elif include_wires and head == "wire":
            pts = item.first_child("pts")
            if pts is not None:
                for point in pts.child_lists("xy"):
                    changed.extend(_snap_xy(point, grid_mm, "wire", _uuid(item)))
        elif include_labels and head == "no_connect":
            changed.extend(_snap_at(item, grid_mm, "no_connect", _uuid(item)))
    return {
        "grid_mm": grid_mm,
        "changed_count": len(changed),
        "changed_items": changed,
    }


def _normalize_connection(connection: dict[str, Any]) -> list[dict[str, Any]]:
    ctype = connection.get("type")
    if not ctype and {"ref", "pin", "net"}.issubset(connection):
        ctype = "pin_to_net"
    if ctype == "pin_to_net":
        return [_pin_to_net(connection, connection["ref"], connection["pin"], connection["net"])]
    if ctype == "pin_to_ground":
        return [_pin_to_net(connection, connection["ref"], connection["pin"], connection.get("net", "GND"))]
    if ctype == "pin_to_power":
        return [_pin_to_net(connection, connection["ref"], connection["pin"], connection["net"])]
    if ctype == "pin_to_pin":
        start = connection.get("from") or connection.get("a")
        end = connection.get("to") or connection.get("b")
        if not isinstance(start, dict) or not isinstance(end, dict):
            raise ValueError("pin_to_pin requires from/to objects")
        net_name = connection.get("net") or _auto_net_name(
            str(start["ref"]), str(start["pin"]), str(end["ref"]), str(end["pin"])
        )
        return [
            _pin_to_net(connection, start["ref"], start["pin"], net_name),
            _pin_to_net(connection, end["ref"], end["pin"], net_name),
        ]
    raise ValueError(f"Unsupported connection type: {ctype or '<missing>'}")


def _pin_to_net(source: dict[str, Any], ref: Any, pin: Any, net: Any) -> dict[str, Any]:
    return {
        "type": "pin_to_net",
        "ref": str(ref),
        "pin": str(pin),
        "net": str(net),
        "label_type": source.get("label_type", "global"),
        "stub_length_mm": float(source.get("stub_length_mm", 5.08)),
        "label_placement": source.get("label_placement", "pin_anchor"),
        "label_clearance_mm": float(source.get("label_clearance_mm", 5.08)),
        "connection_style": source.get("connection_style", "label"),
        "allow_hidden_power": bool(source.get("allow_hidden_power", False)),
        "required": bool(source.get("required", True)),
        "source": source,
    }


def _apply_normalized_connection(
    schematic: KiCadSchematic, schematic_path: str, connection: dict[str, Any]
) -> dict[str, Any]:
    return connect_pin_to_net(
        schematic,
        schematic_path,
        connection["ref"],
        connection["pin"],
        connection["net"],
        label_type=connection.get("label_type", "global"),
        stub_length_mm=connection.get("stub_length_mm", 5.08),
        allow_hidden_power=connection.get("allow_hidden_power", False),
        label_placement=connection.get("label_placement", "pin_anchor"),
        label_clearance_mm=connection.get("label_clearance_mm", 5.08),
        connection_style=connection.get("connection_style", "label"),
    )


def _membership_from_native(native: dict[str, Any], reference: str, pin: str, net_name: str) -> bool:
    net = native.get("nets", {}).get(net_name)
    if not net:
        return False
    for node in net.get("nodes", []):
        pinfunction = node.get("pinfunction", "")
        if node.get("ref") == reference and (
            node.get("pin") == pin or pinfunction == pin or pinfunction.startswith(f"{pin}_")
        ):
            return True
    return False


def _snap_at(node: SExprList, grid_mm: float, item_type: str, item_id: str) -> list[dict[str, Any]]:
    at_expr = node.first_child("at")
    if at_expr is None or len(at_expr.items) < 3:
        return []
    old_x = _atom_float(at_expr.items[1])
    old_y = _atom_float(at_expr.items[2])
    if old_x is None or old_y is None:
        return []
    new_x = _snap(old_x, grid_mm)
    new_y = _snap(old_y, grid_mm)
    if new_x == old_x and new_y == old_y:
        return []
    at_expr.items[1] = SExprAtom(_format_number(new_x))
    at_expr.items[2] = SExprAtom(_format_number(new_y))
    return [
        {
            "type": item_type,
            "id": item_id,
            "from": {"x": old_x, "y": old_y},
            "to": {"x": new_x, "y": new_y},
        }
    ]


def _snap_xy(node: SExprList, grid_mm: float, item_type: str, item_id: str) -> list[dict[str, Any]]:
    if len(node.items) < 3:
        return []
    old_x = _atom_float(node.items[1])
    old_y = _atom_float(node.items[2])
    if old_x is None or old_y is None:
        return []
    new_x = _snap(old_x, grid_mm)
    new_y = _snap(old_y, grid_mm)
    if new_x == old_x and new_y == old_y:
        return []
    node.items[1] = SExprAtom(_format_number(new_x))
    node.items[2] = SExprAtom(_format_number(new_y))
    return [
        {
            "type": item_type,
            "id": item_id,
            "from": {"x": old_x, "y": old_y},
            "to": {"x": new_x, "y": new_y},
        }
    ]


def _atom_float(node: Any) -> float | None:
    value = getattr(node, "value", None)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _format_number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _snap(value: float, grid: float = SCHEMATIC_GRID_MM) -> float:
    return round(round(value / grid) * grid, 6)


def _auto_net_name(ref_a: str, pin_a: str, ref_b: str, pin_b: str) -> str:
    safe = "_".join([ref_a, pin_a, ref_b, pin_b])
    return "".join(char if char.isalnum() else "_" for char in safe).upper()


def _uuid(node: SExprList) -> str:
    child = node.first_child("uuid")
    if child is not None and len(child.items) >= 2:
        return getattr(child.items[1], "value", "")
    return ""


def _symbol_reference(node: SExprList) -> str:
    for prop in node.child_lists("property"):
        if _property_name(prop) == "Reference" and len(prop.items) >= 3:
            return getattr(prop.items[2], "value", "")
    return _uuid(node)


def _property_name(node: SExprList) -> str:
    if len(node.items) >= 2:
        return getattr(node.items[1], "value", "")
    return ""


def _label_text(node: SExprList) -> str:
    if len(node.items) >= 2:
        return getattr(node.items[1], "value", "")
    return _uuid(node)


def _verification_reason(
    native: dict[str, Any], erc: dict[str, Any], erc_blocking: bool
) -> str:
    if not native.get("success"):
        return str(native.get("reason", "native verification failed"))
    if erc_blocking:
        return "ERC reported blocking violations"
    if erc.get("success") is False:
        return str(erc.get("error", "ERC unavailable"))
    return "native netlist and ERC checks passed"


def _verification_warnings(native: dict[str, Any], erc: dict[str, Any]) -> list[str]:
    warnings = []
    if native.get("skipped"):
        warnings.append("Native netlist verification was skipped")
    if erc.get("skipped"):
        warnings.append("ERC was skipped")
    if erc.get("success") is False:
        warnings.append(f"ERC could not run: {erc.get('error')}")
    if erc.get("total_violations", 0):
        warnings.append(f"ERC reported {erc.get('total_violations')} violation(s)")
    return warnings


def _missing_from_failed_transaction(
    schematic_path: str, connections: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    native = verify_native_memberships(schematic_path, connections)
    if native.get("missing"):
        return list(native["missing"])
    return []


def _failure_response(
    schematic_path: str,
    failed_connections: list[dict[str, Any]],
    error: str,
    *,
    rolled_back: bool,
    debug: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "success": False,
        "tool": "schematic_apply_connection_plan",
        "stage": "schematic_wiring",
        "schematic_path": schematic_path,
        "error": error,
        "rolled_back": rolled_back,
        "recoverable": True,
        "failed_connections": failed_connections,
        "recommended_next_tool": "schematic_quality_report",
        "recommended_next_arguments": {"project_path": schematic_path},
        "debug": debug or {},
    }
