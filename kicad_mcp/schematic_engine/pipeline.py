"""Main schematic engine pipeline.

Orchestrates the full netlist-first workflow:
  1. Normalize design intent
  2. Resolve symbols and footprints
  3. Compile canonical circuit
  4. Compile SKiDL circuit and expected netlist
  5. Plan schematic sheets and symbol placement
  6. Write schematic to a temporary project copy
  7. Export KiCad CLI netlist from generated schematic
  8. Compare expected netlist vs KiCad exported netlist
  9. Run ERC
  10. Export SVG preview
  11. Visual lint
  12. Commit temp result atomically or rollback
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from kicad_mcp.schematic_engine.expected_netlist import (
    compare_netlists,
    parse_kicad_netlist,
)
from kicad_mcp.schematic_engine.intent_state import (
    prepare_intent_for_action,
    save_committed_intent,
)
from kicad_mcp.schematic_engine.kicad_cli_verifier import KicadCliVerifier
from kicad_mcp.schematic_engine.normalize import normalize_design_intent
from kicad_mcp.schematic_engine.result import EngineResult
from kicad_mcp.schematic_engine.schematic_writer import SchematicWriter
from kicad_mcp.schematic_engine.sheet_planner import plan_sheets
from kicad_mcp.schematic_engine.skidl_compiler import SkidlCompiler
from kicad_mcp.schematic_engine.transaction import SchematicBuildTransaction
from kicad_mcp.schematic_engine.visual_lint import visual_lint

logger = logging.getLogger(__name__)


def apply_design_intent_netlist_first(
    project_path: str,
    intent: dict[str, Any],
    *,
    mode: str = "replace",
    atomic: bool = True,
    visual_style: str = "professional_blocks",
    run_erc: bool = True,
    export_svg: bool = True,
    max_inline_bytes: int = 50_000,
    strict: bool = True,
    require_netlist_match: bool = True,
    require_kicad_cli_verification: bool = True,
    job_id: str | None = None,
    cancel_check: Any | None = None,
) -> dict[str, Any]:
    """Apply design intent using the netlist-first pipeline.

    The live project is NOT modified until all verification passes.

    Args:
        project_path: Path to the KiCad project file (.kicad_pro).
        intent: Design intent dictionary.
        mode: Internal build mode. The public MCP tool always uses replace semantics.
        atomic: If True, use transaction model (default).
        visual_style: Layout style for schematic generation.
        run_erc: Whether to run KiCad CLI ERC.
        export_svg: Whether to export SVG preview.
        max_inline_bytes: Max bytes for inline SVG in response.
        strict: If True, any ERC error, netlist mismatch, or blocking generation issue
            blocks commit.
        require_netlist_match: If True, netlist mismatch always blocks commit.
        require_kicad_cli_verification: If True, KiCad CLI netlist export must
            succeed for commit to proceed.
        job_id: Optional job ID for progress tracking.
        cancel_check: Optional callable that returns True if job is cancelled.

    Returns:
        Dict with success status, verification results, and artifacts.
    """
    start_time = time.monotonic()
    result = EngineResult(
        success=False,
        project_path=project_path,
        engine="skidl_kiutils_kicad_cli",
    )

    def _is_cancelled() -> bool:
        if cancel_check and callable(cancel_check):
            return cancel_check()
        return False

    try:
        # --- Stage 1: Normalize intent ---
        result.stage = "normalizing"
        result.progress = {"step": 1, "step_count": 12, "message": "Normalizing design intent"}

        try:
            effective_intent, intent_action = prepare_intent_for_action(project_path, intent)
            result.intent_action = intent_action
            canonical = normalize_design_intent(project_path, effective_intent)
        except (ValueError, KeyError) as e:
            result.error = f"Intent normalization failed: {e}"
            result.stage = "normalize_failed"
            return result.to_dict()

        result.part_count = len(canonical.parts)
        result.endpoint_count = len(canonical.endpoints)

        if _is_cancelled():
            return _cancelled_result(result, "after_normalize")

        # --- Stage 2-3: Compile canonical circuit ---
        result.stage = "compiling"
        result.progress = {"step": 2, "step_count": 12, "message": "Compiling circuit"}

        # Determine artifact directory
        project_dir = os.path.dirname(os.path.abspath(project_path))
        artifact_dir = os.path.join(project_dir, ".kicad_mcp", "engine_artifacts")
        if job_id:
            artifact_dir = os.path.join(artifact_dir, job_id)
        os.makedirs(artifact_dir, exist_ok=True)
        result.artifact_dir = artifact_dir

        # --- Stage 4: SKiDL compile + expected netlist ---
        result.stage = "skidl_compile"
        result.progress = {
            "step": 4,
            "step_count": 12,
            "message": "Compiling SKiDL circuit and generating expected netlist",
        }

        compiler = SkidlCompiler(artifact_dir=artifact_dir)
        try:
            compile_result = compiler.compile(canonical)
        except RuntimeError as e:
            result.error = str(e)
            result.stage = "engine_not_ready"
            result.rolled_back = True
            return result.to_dict()

        if not compile_result.success:
            result.error = compile_result.error or "SKiDL compilation failed"
            result.stage = "skidl_compile_failed"
            return result.to_dict()

        result.expected_netlist_path = compile_result.expected_netlist_path
        result.net_count = compile_result.net_count

        if _is_cancelled():
            return _cancelled_result(result, "after_skidl_compile")

        # --- Stage 5: Sheet planning ---
        result.stage = "planning_sheets"
        result.progress = {
            "step": 5,
            "step_count": 12,
            "message": "Planning schematic sheets and placement",
        }

        sheet_plan = plan_sheets(canonical, style=visual_style)
        result.sheets = list(sheet_plan.sheets.keys())

        if _is_cancelled():
            return _cancelled_result(result, "after_planning")

        # --- Stage 6: Visual lint (pre-write) ---
        result.stage = "visual_lint_prewrite"
        result.progress = {
            "step": 6,
            "step_count": 12,
            "message": "Running pre-write visual lint",
        }

        lint_result = visual_lint(canonical, sheet_plan)
        result.visual_lint = {
            "blocking_count": lint_result.blocking_count,
            "warning_count": lint_result.warning_count,
            "issues": [
                {
                    "category": _visual_issue_category(issue.type, issue.severity),
                    "type": issue.type,
                    "ref": issue.ref,
                    "message": issue.message,
                    "severity": issue.severity,
                }
                for issue in lint_result.issues[:20]  # Limit to 20 issues
            ],
        }

        if lint_result.blocking_count > 0 and strict:
            result.error = (
                f"Visual lint found {lint_result.blocking_count} blocking issues"
            )
            result.stage = "visual_lint_failed"
            return result.to_dict()

        if _is_cancelled():
            return _cancelled_result(result, "after_visual_lint")

        # --- Stage 7: Write schematic (in transaction) ---
        result.stage = "writing_schematic"
        result.progress = {
            "step": 7,
            "step_count": 12,
            "message": "Writing schematic to temporary project",
        }

        with SchematicBuildTransaction(project_path, job_id=job_id) as tx:
            temp_dir = tx.create_worktree()
            project_name = os.path.splitext(os.path.basename(project_path))[0]

            writer = SchematicWriter(temp_dir, project_name)
            write_result = writer.write(canonical, sheet_plan)

            if not write_result.get("success"):
                result.error = write_result.get("error", "Schematic write failed")
                result.stage = "write_failed"
                tx.rollback()
                result.rolled_back = True
                return result.to_dict()

            generated_symbol_count = _count_symbols_in_paths(tx.list_generated_schematics())
            result.output_symbol_count = generated_symbol_count
            if len(canonical.parts) > 0 and generated_symbol_count < len(canonical.parts):
                result.error = (
                    "Generated schematic persistence check failed: "
                    f"expected at least {len(canonical.parts)} symbol(s), "
                    f"found {generated_symbol_count}"
                )
                result.stage = "persistence_verification_failed"
                tx.rollback()
                result.rolled_back = True
                return result.to_dict()

            if _is_cancelled():
                tx.rollback()
                return _cancelled_result(result, "after_write")

            # --- Stage 8: KiCad CLI verification ---
            root_schematic = tx.get_worktree_schematic()

            cli_verification_success = True
            kicad_cli_available = True
            cli_failure_stage = "kicad_cli_verification_failed"
            if run_erc or export_svg or require_kicad_cli_verification:
                result.stage = "kicad_cli_verification"
                result.progress = {
                    "step": 8,
                    "step_count": 12,
                    "message": "Running KiCad CLI verification",
                }

                verifier = KicadCliVerifier(output_dir=artifact_dir)
                try:
                    verify_result = verifier.verify(
                        root_schematic,
                        run_netlist=True,
                        run_erc=run_erc,
                        export_svg=export_svg,
                    )

                    result.erc = {
                        "errors": verify_result.erc_errors,
                        "warnings": verify_result.erc_warnings,
                        "total": verify_result.erc_total,
                    }
                    result.erc_path = verify_result.erc_path
                    result.svg_dir = verify_result.svg_dir
                    result.kicad_netlist_path = verify_result.netlist_path

                    if verify_result.erc_errors > 0 and strict:
                        cli_verification_success = False
                        cli_failure_stage = "erc_failed"
                        result.error = (
                            f"ERC found {verify_result.erc_errors} error(s)"
                        )
                except Exception as e:
                    # KiCad CLI may not be available in all environments
                    logger.warning("KiCad CLI verification failed: %s", e)
                    kicad_cli_available = False
                    result.erc = {"errors": 0, "warnings": 0, "note": str(e)}

                    # If safe mode requires CLI verification, block commit
                    if require_kicad_cli_verification:
                        cli_verification_success = False
                        result.error = (
                            f"KiCad CLI verification required but failed: {e}"
                        )

            if _is_cancelled():
                tx.rollback()
                return _cancelled_result(result, "after_cli_verification")

            # --- Stage 9: Netlist comparison ---
            netlist_match = True
            if compile_result.expected_netlist and result.kicad_netlist_path:
                result.stage = "netlist_comparison"
                result.progress = {
                    "step": 9,
                    "step_count": 12,
                    "message": "Comparing expected vs KiCad netlist",
                }

                actual_netlist = parse_kicad_netlist(result.kicad_netlist_path)
                compare_result = compare_netlists(
                    compile_result.expected_netlist,
                    actual_netlist,
                    ignore_no_connects=canonical.no_connects,
                )

                result.netlist_compare = {
                    "success": compare_result.success,
                    "missing_endpoints": compare_result.missing_endpoints[:10],
                    "extra_endpoints": compare_result.extra_endpoints[:10],
                    "expected_net_count": compare_result.expected_net_count,
                    "actual_net_count": compare_result.actual_net_count,
                }
                result.expected_netlist_match = compare_result.success
                netlist_match = compare_result.success

                if not compare_result.success and (strict or require_netlist_match):
                    result.error = (
                        f"Netlist mismatch: {len(compare_result.missing_endpoints)} "
                        f"missing endpoints"
                    )
            elif require_kicad_cli_verification and not result.kicad_netlist_path:
                # Safe mode requires netlist export to have succeeded
                if kicad_cli_available:
                    netlist_match = False
                    result.expected_netlist_match = False
                    result.error = (
                        "KiCad CLI netlist export did not produce output; "
                        "cannot verify connectivity"
                    )
                else:
                    result.expected_netlist_match = None  # CLI not available
            else:
                result.expected_netlist_match = None  # Could not compare

            # --- Stage 10-11: Commit or rollback decision ---
            result.stage = "commit_decision"
            result.progress = {
                "step": 11,
                "step_count": 12,
                "message": "Making commit/rollback decision",
            }

            should_commit = True
            if not netlist_match and (strict or require_netlist_match):
                should_commit = False
                result.stage = "netlist_mismatch"
            if not cli_verification_success and (strict or require_kicad_cli_verification):
                should_commit = False
                result.stage = cli_failure_stage
            if lint_result.blocking_count > 0 and strict:
                should_commit = False
                result.stage = "visual_lint_failed"

            if not should_commit:
                tx.rollback()
                result.success = False
                result.changed = False
                result.rolled_back = True
                return result.to_dict()

            # --- Stage 12: Commit ---
            result.stage = "committing"
            result.progress = {
                "step": 12,
                "step_count": 12,
                "message": "Committing schematic to project",
            }

            if atomic:
                commit_result = tx.commit()
                if not commit_result.get("success"):
                    result.error = commit_result.get("error", "Commit failed")
                    result.stage = "commit_failed"
                    result.rolled_back = True
                    return result.to_dict()
            else:
                tx.commit()

            committed_symbol_count = _count_symbols_in_project(project_path)
            result.output_symbol_count = committed_symbol_count
            if len(canonical.parts) > 0 and committed_symbol_count < len(canonical.parts):
                result.success = False
                result.changed = True
                result.error = (
                    "Committed schematic persistence check failed: "
                    f"expected at least {len(canonical.parts)} symbol(s), "
                    f"found {committed_symbol_count}"
                )
                result.stage = "persistence_verification_failed"
                return result.to_dict()

            result.intent_state_path = save_committed_intent(
                project_path,
                effective_intent,
                action=intent_action,
            )
            result.success = True
            result.changed = True
            result.stage = "schematic_committed"
            result.recommended_next_tool = "pcb_complete_from_schematic"

    except Exception as e:
        result.error = f"Engine pipeline failed: {e}"
        result.stage = "pipeline_exception"
        result.rolled_back = True
        logger.exception("Schematic engine pipeline failed")

    elapsed = time.monotonic() - start_time
    result.progress["elapsed_seconds"] = elapsed

    return result.to_dict()


def _cancelled_result(result: EngineResult, stage: str) -> dict[str, Any]:
    """Return a cancelled result."""
    result.success = False
    result.changed = False
    result.rolled_back = True
    result.stage = f"cancelled:{stage}"
    result.error = f"Job cancelled at stage: {stage}"
    return result.to_dict()


def _visual_issue_category(issue_type: str, severity: str) -> str:
    if severity == "blocking" and issue_type == "unplaced_symbol":
        return "blocking_generation_issue"
    if severity == "blocking":
        return "blocking_connectivity_issue"
    return "visual_warning"


def _count_symbols_in_project(project_path: str) -> int:
    project_dir = os.path.dirname(os.path.abspath(project_path))
    return _count_symbols_in_paths(
        [
            os.path.join(root, filename)
            for root, _dirs, files in os.walk(project_dir)
            if ".kicad_mcp" not in root.split(os.sep)
            for filename in files
            if filename.endswith(".kicad_sch")
        ]
    )


def _count_symbols_in_paths(paths: list[str]) -> int:
    from kicad_mcp.utils.kicad_s_expr import KiCadSchematic

    count = 0
    for path in paths:
        try:
            schematic = KiCadSchematic.from_file(path)
            count += len(schematic.list_symbols())
        except Exception:
            continue
    return count
