"""Public MCP tool registration for the simplified design-intent workflow."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from kicad_mcp.tools import creation_tools as ct


def register_design_intent_tools(mcp: FastMCP) -> None:
    """Register the public design-intent tool surface."""

    @mcp.tool()
    def schematic_preview_design_intent(
        project_path: str,
        intent: dict[str, Any],
    ) -> dict[str, Any]:
        """Preview schematic generation readiness from high-level design intent."""
        resolved_project = ct._resolve_project_alias(project_path, None, None)
        return ct._preview_design_intent_netlist_first(
            resolved_project,
            intent or {},
            visual_style="professional_blocks",
        )

    @mcp.tool()
    def schematic_apply_design_intent(
        project_path: str,
        intent: dict[str, Any],
    ) -> dict[str, Any]:
        """Create or update a schematic from high-level design intent."""
        resolved_project = ct._resolve_project_alias(project_path, None, None)
        return ct._apply_via_netlist_first_engine(
            resolved_project,
            intent or {},
            mode="update",
            strict=True,
            visual_style="professional_blocks",
            allow_partial_write=False,
            atomic=True,
            require_netlist_match=True,
            require_kicad_cli_verification=True,
        )

    @mcp.tool()
    def schematic_apply_expanded_spec(
        project_path: str | None = None,
        expanded_spec_path: str | None = None,
        spec: dict[str, Any] | None = None,
        mode: str = "update",
        strict: bool = False,
        detail: str = "compact",
        quick_apply: bool = False,
        include_preview: bool = False,
        run_quality_report: bool = False,
        run_native_validation: bool = False,
        run_cli_validation: bool = True,
        unsafe_fast_apply: bool = False,
        schematic_path: str | None = None,
        visual_layout: bool = True,
        allow_partial_write: bool = False,
        path: str | None = None,
    ) -> dict[str, Any]:
        """Apply a previously compiled design-intent expanded v2 spec without recompiling."""
        resolved_project = ct._resolve_project_alias(project_path, schematic_path, path)
        return ct._run_with_project_mutation_lock(
            resolved_project,
            "schematic_apply_expanded_spec",
            lambda: ct._schematic_apply_expanded_spec_response(
                resolved_project,
                expanded_spec_path=expanded_spec_path,
                spec=spec,
                mode=mode,
                strict=strict,
                detail=detail,
                quick_apply=quick_apply,
                include_preview=include_preview,
                run_quality_report=run_quality_report,
                run_native_validation=run_native_validation,
                run_cli_validation=run_cli_validation,
                unsafe_fast_apply=unsafe_fast_apply,
                visual_layout=visual_layout,
                allow_partial_write=allow_partial_write,
            ),
        )

    @mcp.tool()
    def schematic_engine_status() -> dict[str, Any]:
        """Report readiness of the required schematic-generation runtime."""
        kicad_cli_available = False
        try:
            cli_path = ct.get_kicad_cli_path(required=False)
            kicad_cli_available = cli_path is not None
        except Exception:
            pass

        skidl_available = False
        try:
            from kicad_mcp.schematic_engine.skidl_compiler import _SKIDL_AVAILABLE

            skidl_available = _SKIDL_AVAILABLE
        except Exception:
            pass

        kiutils_available = False
        kicad_skip_available = False
        try:
            from kicad_mcp.schematic_engine.schematic_writer import (
                _KICAD_SKIP_AVAILABLE,
                _KIUTILS_AVAILABLE,
            )

            kiutils_available = _KIUTILS_AVAILABLE
            kicad_skip_available = _KICAD_SKIP_AVAILABLE
        except Exception:
            pass

        ready = (
            kicad_cli_available and skidl_available and kiutils_available and kicad_skip_available
        )
        return {
            "engine": "skidl_kiutils_kicad_cli",
            "skidl": "installed" if skidl_available else "missing",
            "kiutils": "installed" if kiutils_available else "missing",
            "kicad_skip": "installed" if kicad_skip_available else "missing",
            "kicad_cli_available": kicad_cli_available,
            "ready": ready,
        }

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
                }
                if not compare_result.success:
                    result["success"] = False

            if run_visual_lint:
                try:
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
                            "note": "Visual lint requires design intent to reconstruct canonical circuit. Use pipeline for full lint.",
                            "stored_metadata": netlist_data.get("metadata", {}),
                        }
                    else:
                        result["visual_lint"] = {
                            "note": "Visual lint skipped: no engine artifacts found. Run schematic_apply_design_intent for full validation.",
                        }
                except Exception:
                    result["visual_lint"] = {
                        "note": "Visual lint dependencies not available",
                    }

            return result
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    @mcp.tool()
    def schematic_rebuild_from_canonical_netlist(
        project_path: str | None = None,
        intent: dict[str, Any] | None = None,
        visual_style: str = "professional_blocks",
        strict: bool = True,
        path: str | None = None,
    ) -> dict[str, Any]:
        """Rebuild a schematic from scratch using the netlist-first engine."""
        resolved_project = ct._resolve_project_alias(project_path, None, path)
        if not intent:
            return {"success": False, "error": "intent is required"}
        return ct._apply_via_netlist_first_engine(
            resolved_project,
            intent,
            mode="replace",
            strict=strict,
            visual_style=visual_style,
            allow_partial_write=False,
            atomic=True,
        )

    @mcp.tool()
    def schematic_design_intent_schema(section: str = "all") -> dict[str, Any]:
        """Return compact schema examples for schematic_apply_design_intent."""
        return ct.design_intent_schema(section)