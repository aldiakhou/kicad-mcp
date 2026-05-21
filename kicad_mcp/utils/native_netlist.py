"""
Native KiCad CLI netlist and ERC helpers.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from kicad_mcp.config import TIMEOUT_CONSTANTS
from kicad_mcp.utils.kicad_cli import KiCadCLIError, get_kicad_cli_path
from kicad_mcp.utils.kicad_s_expr import SExprAtom, SExprList, parse_s_expression


def export_native_netlist(
    schematic_path: str, timeout_seconds: float | None = None
) -> dict[str, Any]:
    """Export and parse KiCad's native schematic netlist."""
    resolved_timeout = _resolve_timeout(timeout_seconds)
    result: dict[str, Any] = {
        "success": False,
        "schematic_path": schematic_path,
        "method": "kicad-cli",
        "timeout_seconds": resolved_timeout,
    }
    if not Path(schematic_path).exists():
        result["error"] = f"Schematic file not found: {schematic_path}"
        return result
    try:
        kicad_cli = get_kicad_cli_path()
    except KiCadCLIError as exc:
        result["error"] = str(exc)
        return result

    with tempfile.TemporaryDirectory() as temp_dir:
        output_file = os.path.join(temp_dir, "netlist.kicad_net")
        cmd = [
            kicad_cli,
            "sch",
            "export",
            "netlist",
            "--format",
            "kicadsexpr",
            "--output",
            output_file,
            schematic_path,
        ]
        try:
            process = subprocess.run(
                cmd, capture_output=True, text=True, timeout=resolved_timeout
            )
        except subprocess.TimeoutExpired:
            result["error"] = (
                f"KiCad CLI netlist export timed out after {resolved_timeout:g} seconds."
            )
            return result
        if process.returncode != 0:
            result["error"] = process.stderr.strip() or "KiCad CLI netlist export failed"
            result["stdout"] = process.stdout
            result["stderr"] = process.stderr
            return result
        if not os.path.exists(output_file):
            result["error"] = "KiCad CLI did not create a netlist file"
            return result
        parsed = parse_native_netlist(Path(output_file).read_text(encoding="utf-8"))
        parsed.update(
            {
                "success": True,
                "schematic_path": schematic_path,
                "method": "kicad-cli",
                "timeout_seconds": resolved_timeout,
                "netlist_quality": "native",
                "connectivity_complete": True,
            }
        )
        return parsed


def run_erc_via_cli(
    schematic_path: str, timeout_seconds: float | None = None
) -> dict[str, Any]:
    """Run KiCad ERC and return parsed JSON results."""
    resolved_timeout = _resolve_timeout(timeout_seconds)
    result: dict[str, Any] = {
        "success": False,
        "schematic_path": schematic_path,
        "method": "kicad-cli",
        "timeout_seconds": resolved_timeout,
    }
    if not Path(schematic_path).exists():
        result["error"] = f"Schematic file not found: {schematic_path}"
        return result
    try:
        kicad_cli = get_kicad_cli_path()
    except KiCadCLIError as exc:
        result["error"] = str(exc)
        return result

    with tempfile.TemporaryDirectory() as temp_dir:
        output_file = os.path.join(temp_dir, "erc_report.json")
        cmd = [
            kicad_cli,
            "sch",
            "erc",
            "--format",
            "json",
            "--output",
            output_file,
            schematic_path,
        ]
        try:
            process = subprocess.run(
                cmd, capture_output=True, text=True, timeout=resolved_timeout
            )
        except subprocess.TimeoutExpired:
            result["error"] = (
                f"KiCad CLI ERC timed out after {resolved_timeout:g} seconds."
            )
            return result
        if process.returncode != 0 and not os.path.exists(output_file):
            result["error"] = process.stderr.strip() or "KiCad CLI ERC failed"
            result["stdout"] = process.stdout
            result["stderr"] = process.stderr
            return result
        if not os.path.exists(output_file):
            result["error"] = "KiCad CLI did not create an ERC report"
            return result
        try:
            report = json.loads(Path(output_file).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            result["error"] = f"Failed to parse ERC report JSON: {exc}"
            return result

    violations = _erc_violations(report)
    categories: dict[str, int] = {}
    severities: dict[str, int] = {}
    for violation in violations:
        categories[violation.get("type", "unknown")] = (
            categories.get(violation.get("type", "unknown"), 0) + 1
        )
        severities[violation.get("severity", "unknown")] = (
            severities.get(violation.get("severity", "unknown"), 0) + 1
        )
    return {
        "success": True,
        "schematic_path": schematic_path,
        "method": "kicad-cli",
        "timeout_seconds": resolved_timeout,
        "total_violations": len(violations),
        "violation_categories": categories,
        "severity_counts": severities,
        "violations": violations,
        "report": report,
    }


def parse_native_netlist(content: str) -> dict[str, Any]:
    """Parse KiCad's native S-expression netlist export."""
    root = parse_s_expression(content)
    components: dict[str, dict[str, Any]] = {}
    components_expr = _first_child(root, "components")
    if components_expr is not None:
        for comp in components_expr.child_lists("comp"):
            ref = _child_text(comp, "ref")
            if not ref:
                continue
            component = {
                "reference": ref,
                "value": _child_text(comp, "value") or "",
                "footprint": _child_text(comp, "footprint") or "",
                "datasheet": _child_text(comp, "datasheet") or "",
            }
            libsource = _first_child(comp, "libsource")
            if libsource is not None:
                component["libsource"] = {
                    "lib": _child_text(libsource, "lib") or "",
                    "part": _child_text(libsource, "part") or "",
                }
            components[ref] = component

    nets: dict[str, dict[str, Any]] = {}
    nets_expr = _first_child(root, "nets")
    if nets_expr is not None:
        for net in nets_expr.child_lists("net"):
            name = _child_text(net, "name")
            if name is None:
                continue
            nodes = []
            for node in net.child_lists("node"):
                nodes.append(
                    {
                        "ref": _child_text(node, "ref") or "",
                        "pin": _child_text(node, "pin") or "",
                        "pinfunction": _child_text(node, "pinfunction") or "",
                        "pintype": _child_text(node, "pintype") or "",
                    }
                )
            nets[name] = {
                "code": _child_text(net, "code") or "",
                "name": name,
                "class": _child_text(net, "class") or "",
                "nodes": nodes,
            }

    return {
        "components": components,
        "nets": nets,
        "component_count": len(components),
        "net_count": len(nets),
    }


def _resolve_timeout(timeout_seconds: float | None) -> float:
    if timeout_seconds is not None and timeout_seconds > 0:
        return float(timeout_seconds)
    return float(TIMEOUT_CONSTANTS.get("kicad_cli_export", 30.0))


def _erc_violations(report: dict[str, Any]) -> list[dict[str, Any]]:
    violations = list(report.get("violations", []))
    for sheet in report.get("sheets", []):
        violations.extend(sheet.get("violations", []))
    return violations


def _first_child(expr: SExprList, head: str) -> SExprList | None:
    return expr.first_child(head)


def _child_text(expr: SExprList, head: str) -> str | None:
    child = expr.first_child(head)
    if child is None or len(child.items) < 2:
        return None
    atom = child.items[1]
    return atom.value if isinstance(atom, SExprAtom) else None
