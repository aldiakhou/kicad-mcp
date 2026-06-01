"""Generated schematic validation MCP tools."""

import json
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

import kicad_mcp.tools.creation_tools as ct


def register_schematic_validation_tools(mcp: FastMCP) -> None:
    """Register generated schematic validation tools."""

    @mcp.tool()
    def schematic_validate_generated_schematic(
        project_path: str | None = None,
        schematic_path: str | None = None,
        expected_netlist_path: str | None = None,
        run_erc: bool = True,
        run_visual_lint: bool = True,
        path: str | None = None,
    ) -> dict[str, Any]:
        """Validate a generated schematic against expected netlist, ERC, and visual lint."""
        resolved_project = ct._resolve_project_alias(project_path, schematic_path, path)
        try:
            from kicad_mcp.schematic_engine.expected_netlist import (
                check_power_net_sanity,
                compare_netlists,
                load_expected_netlist,
                parse_kicad_netlist,
            )
            from kicad_mcp.schematic_engine.kicad_cli_verifier import KicadCliVerifier

            sch_path = schematic_path or ct.get_project_files(resolved_project).get("schematic")
            if not sch_path:
                return {"success": False, "error": "Schematic file not found"}

            result: dict[str, Any] = {
                "success": True,
                "tool": "schematic_validate_generated_schematic",
                "project_path": resolved_project,
            }

            verifier = KicadCliVerifier()
            verify_result = verifier.verify(sch_path, run_erc=run_erc, export_svg=False)
            result["erc"] = {
                "errors": verify_result.erc_errors,
                "warnings": verify_result.erc_warnings,
                "total": verify_result.erc_total,
            }
            if verify_result.erc_errors > 0:
                result["success"] = False

            if expected_netlist_path and verify_result.netlist_path:
                expected = load_expected_netlist(expected_netlist_path)
                actual = parse_kicad_netlist(verify_result.netlist_path)
                compare_result = compare_netlists(expected, actual)
                result["netlist_compare"] = {
                    "success": compare_result.success,
                    "missing_endpoints": compare_result.missing_endpoints[:10],
                    "extra_endpoints": compare_result.extra_endpoints[:10],
                    "missing_endpoint_count": len(compare_result.missing_endpoints),
                    "extra_endpoint_count": len(compare_result.extra_endpoints),
                }
                power_sanity = check_power_net_sanity(expected, actual)
                result["power_net_sanity"] = power_sanity
                if not compare_result.success:
                    result["success"] = False
                if not power_sanity.get("success", True):
                    result["success"] = False

            if run_visual_lint:
                artifact_dir = ct.os.path.join(
                    ct.os.path.dirname(ct.os.path.abspath(resolved_project)),
                    ".kicad_mcp",
                    "engine_artifacts",
                )
                netlist_json = ct.os.path.join(artifact_dir, "expected_netlist.json")
                if ct.os.path.exists(netlist_json):
                    with open(netlist_json, encoding="utf-8") as handle:
                        netlist_data = ct.json.load(handle)
                    result["visual_lint"] = {
                        "note": "Visual lint requires design intent to reconstruct canonical circuit. Use the apply pipeline for full lint.",
                        "stored_metadata": netlist_data.get("metadata", {}),
                    }
                else:
                    result["visual_lint"] = {
                        "note": "Visual lint skipped: no engine artifacts found. Run schematic_start_design_intent_job for full validation.",
                    }

            return result
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    @mcp.tool()
    def schematic_compare_netlists(
        expected_netlist_path: str,
        actual_netlist_path: str,
        compare_mode: str = "permissive",
        output_path: str | None = None,
    ) -> dict[str, Any]:
        """Compare an expected JSON netlist with a KiCad S-expression netlist."""
        try:
            from kicad_mcp.schematic_engine.expected_netlist import (
                check_power_net_sanity,
                compare_netlists,
                load_expected_netlist,
                parse_kicad_netlist,
            )

            expected = load_expected_netlist(expected_netlist_path)
            actual = parse_kicad_netlist(actual_netlist_path)
            compare_result = compare_netlists(expected, actual, compare_mode=compare_mode)
            power_sanity = check_power_net_sanity(expected, actual)
            payload = {
                "success": compare_result.success and power_sanity.get("success", True),
                "tool": "schematic_compare_netlists",
                "expected_netlist_path": expected_netlist_path,
                "actual_netlist_path": actual_netlist_path,
                "compare_mode": compare_mode,
                "netlist_compare": {
                    "success": compare_result.success,
                    "missing_endpoints": compare_result.missing_endpoints,
                    "extra_endpoints": compare_result.extra_endpoints,
                    "mismatched_nets": compare_result.mismatched_nets,
                    "expected_net_count": compare_result.expected_net_count,
                    "actual_net_count": compare_result.actual_net_count,
                },
                "power_net_sanity": power_sanity,
            }
            diff_path = Path(output_path) if output_path else (
                Path(actual_netlist_path).with_name("netlist_compare.diff.json")
            )
            diff_path.parent.mkdir(parents=True, exist_ok=True)
            diff_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            payload["diff_path"] = str(diff_path)
            return payload
        except Exception as exc:
            return {
                "success": False,
                "tool": "schematic_compare_netlists",
                "expected_netlist_path": expected_netlist_path,
                "actual_netlist_path": actual_netlist_path,
                "error": str(exc),
            }
