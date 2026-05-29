"""KiCad CLI verifier.

Uses the KiCad CLI to export netlist, run ERC, and export SVG from
generated schematics. All commands are executed through SecureSubprocessRunner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class VerifierResult:
    """Result of KiCad CLI verification."""

    success: bool
    error: str | None = None
    netlist_path: str | None = None
    erc_path: str | None = None
    svg_dir: str | None = None
    erc_total: int = 0
    erc_errors: int = 0
    erc_warnings: int = 0
    warnings: list[str] = field(default_factory=list)


class KicadCliVerifier:
    """Verifies generated schematics using KiCad CLI.

    Runs:
    - kicad-cli sch export netlist: exports the schematic netlist
    - kicad-cli sch erc: runs electrical rules check
    - kicad-cli sch export svg: exports SVG preview
    """

    def __init__(self, output_dir: str | None = None):
        """Initialize verifier.

        Args:
            output_dir: Directory for output artifacts. If None, creates
                       a subdirectory next to the schematic.
        """
        self.output_dir = output_dir

    def verify(
        self,
        schematic_path: str,
        *,
        run_netlist: bool = True,
        run_erc: bool = True,
        export_svg: bool = True,
    ) -> VerifierResult:
        """Run KiCad CLI verification on a schematic.

        Args:
            schematic_path: Path to the root .kicad_sch file.
            run_netlist: Whether to export netlist.
            run_erc: Whether to run ERC.
            export_svg: Whether to export SVG.

        Returns:
            VerifierResult with all verification outputs.
        """
        from kicad_mcp.utils.secure_subprocess import get_subprocess_runner

        runner = get_subprocess_runner()
        output_dir = self._get_output_dir(schematic_path)
        os.makedirs(output_dir, exist_ok=True)

        result = VerifierResult(success=True)

        # Export netlist
        if run_netlist:
            netlist_result = self._export_netlist(runner, schematic_path, output_dir)
            result.netlist_path = netlist_result.get("path")
            if not netlist_result.get("success"):
                result.warnings.append(
                    f"Netlist export failed: {netlist_result.get('error', 'unknown')}"
                )

        # Run ERC
        if run_erc:
            erc_result = self._run_erc(runner, schematic_path, output_dir)
            result.erc_path = erc_result.get("path")
            result.erc_total = erc_result.get("total", 0)
            result.erc_errors = erc_result.get("errors", 0)
            result.erc_warnings = erc_result.get("warnings", 0)
            if erc_result.get("errors", 0) > 0:
                result.success = False
                result.error = f"ERC found {erc_result['errors']} error(s)"
            if not erc_result.get("success"):
                result.warnings.append(
                    f"ERC execution issue: {erc_result.get('error', 'unknown')}"
                )

        # Export SVG
        if export_svg:
            svg_result = self._export_svg(runner, schematic_path, output_dir)
            result.svg_dir = svg_result.get("path")
            if not svg_result.get("success"):
                result.warnings.append(
                    f"SVG export failed: {svg_result.get('error', 'unknown')}"
                )

        return result

    def _export_netlist(
        self,
        runner: Any,
        schematic_path: str,
        output_dir: str,
    ) -> dict[str, Any]:
        """Export netlist using kicad-cli sch export netlist."""
        netlist_path = os.path.join(output_dir, "generated.net")
        try:
            result = runner.run_kicad_command(
                [
                    "sch", "export", "netlist",
                    "--format", "kicadsexpr",
                    "-o", netlist_path,
                    schematic_path,
                ],
                input_files=[schematic_path],
                output_files=[netlist_path],
                timeout=60,
            )
            if result.returncode == 0 and os.path.exists(netlist_path):
                return {"success": True, "path": netlist_path}
            return {
                "success": False,
                "error": result.stderr if result.stderr else f"exit code {result.returncode}",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _run_erc(
        self,
        runner: Any,
        schematic_path: str,
        output_dir: str,
    ) -> dict[str, Any]:
        """Run ERC using kicad-cli sch erc."""
        erc_path = os.path.join(output_dir, "erc.json")
        try:
            result = runner.run_kicad_command(
                [
                    "sch", "erc",
                    "--format", "json",
                    "-o", erc_path,
                    schematic_path,
                ],
                input_files=[schematic_path],
                output_files=[erc_path],
                timeout=120,
            )
            # ERC may return non-zero if there are violations
            if os.path.exists(erc_path):
                return self._parse_erc_result(erc_path)
            if result.returncode != 0:
                return {
                    "success": False,
                    "error": result.stderr if result.stderr else f"exit code {result.returncode}",
                    "total": 0,
                    "errors": 0,
                    "warnings": 0,
                }
            return {"success": True, "path": erc_path, "total": 0, "errors": 0, "warnings": 0}
        except Exception as e:
            return {"success": False, "error": str(e), "total": 0, "errors": 0, "warnings": 0}

    def _parse_erc_result(self, erc_path: str) -> dict[str, Any]:
        """Parse ERC JSON result file."""
        try:
            with open(erc_path, encoding="utf-8") as f:
                data = json.load(f)

            violations = data.get("violations", [])
            errors = sum(1 for v in violations if v.get("severity", "") == "error")
            warnings = sum(1 for v in violations if v.get("severity", "") == "warning")

            return {
                "success": True,
                "path": erc_path,
                "total": len(violations),
                "errors": errors,
                "warnings": warnings,
            }
        except (json.JSONDecodeError, OSError) as e:
            return {
                "success": False,
                "error": f"Failed to parse ERC result: {e}",
                "path": erc_path,
                "total": 0,
                "errors": 0,
                "warnings": 0,
            }

    def _export_svg(
        self,
        runner: Any,
        schematic_path: str,
        output_dir: str,
    ) -> dict[str, Any]:
        """Export SVG using kicad-cli sch export svg."""
        svg_dir = os.path.join(output_dir, "svg")
        os.makedirs(svg_dir, exist_ok=True)
        try:
            result = runner.run_kicad_command(
                [
                    "sch", "export", "svg",
                    "-o", svg_dir,
                    schematic_path,
                ],
                input_files=[schematic_path],
                output_files=[svg_dir],
                timeout=60,
            )
            if result.returncode == 0:
                return {"success": True, "path": svg_dir}
            return {
                "success": False,
                "error": result.stderr if result.stderr else f"exit code {result.returncode}",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _get_output_dir(self, schematic_path: str) -> str:
        """Determine output directory for verification artifacts."""
        if self.output_dir:
            return self.output_dir
        project_dir = os.path.dirname(schematic_path)
        return os.path.join(project_dir, ".kicad_mcp", "verification")
