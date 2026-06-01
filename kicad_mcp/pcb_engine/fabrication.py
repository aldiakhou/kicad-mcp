"""PCB fabrication export helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
import zipfile

from kicad_mcp.tools import creation_tools as ct
from kicad_mcp.utils.path_validator import PathValidator
from kicad_mcp.utils.secure_subprocess import SecureSubprocessRunner

DEFAULT_GERBER_LAYERS = ",".join(
    [
        "F.Cu",
        "B.Cu",
        "F.Paste",
        "B.Paste",
        "F.SilkS",
        "B.SilkS",
        "F.Mask",
        "B.Mask",
        "Edge.Cuts",
    ]
)


def export_fabrication_package(
    project_path: str,
    *,
    output_dir: str | None = None,
    include_step: bool = False,
    include_ipc2581: bool = False,
    run_drc: bool = True,
) -> dict[str, Any]:
    """Export Gerber, drill, position, and optional 3D/fabrication artifacts."""
    try:
        validated_project = ct.validate_local_path(project_path, "project", must_exist=True)
        files = ct.get_project_files(validated_project)
        if "pcb" not in files:
            return {
                "success": False,
                "tool": "pcb_export_fabrication_package",
                "project_path": validated_project,
                "stage": "missing_pcb",
                "error": "PCB file not found.",
            }
        pcb_path = files["pcb"]
        project = Path(validated_project)
        project_dir = project.parent
        package_dir = (
            Path(output_dir).expanduser().resolve()
            if output_dir
            else project_dir / "fabrication" / project.stem
        )
        package_dir.mkdir(parents=True, exist_ok=True)
        gerber_dir = package_dir / "gerbers"
        drill_dir = package_dir / "drill"
        gerber_dir.mkdir(parents=True, exist_ok=True)
        drill_dir.mkdir(parents=True, exist_ok=True)

        drc = {"success": True, "skipped": True, "reason": "run_drc=False"}
        if run_drc:
            drc = ct._run_pcb_drc_sync(pcb_path)

        runner = SecureSubprocessRunner(
            path_validator=PathValidator(
                trusted_roots={str(project_dir.resolve()), str(package_dir.resolve())}
            )
        )
        commands = [
            _run_export(
                runner,
                pcb_path,
                package_dir,
                [
                    "pcb",
                    "export",
                    "gerbers",
                    "--output",
                    str(gerber_dir),
                    "--layers",
                    DEFAULT_GERBER_LAYERS,
                    "--subtract-soldermask",
                    pcb_path,
                ],
                "gerbers",
            ),
            _run_export(
                runner,
                pcb_path,
                package_dir,
                [
                    "pcb",
                    "export",
                    "drill",
                    "--output",
                    str(drill_dir),
                    "--format",
                    "excellon",
                    "--excellon-units",
                    "mm",
                    "--generate-map",
                    "--map-format",
                    "pdf",
                    pcb_path,
                ],
                "drill",
            ),
            _run_export(
                runner,
                pcb_path,
                package_dir,
                [
                    "pcb",
                    "export",
                    "pos",
                    "--output",
                    str(package_dir / f"{project.stem}_positions.csv"),
                    "--format",
                    "csv",
                    "--units",
                    "mm",
                    "--side",
                    "both",
                    pcb_path,
                ],
                "positions",
            ),
        ]
        if include_step:
            commands.append(
                _run_export(
                    runner,
                    pcb_path,
                    package_dir,
                    [
                        "pcb",
                        "export",
                        "step",
                        "--output",
                        str(package_dir / f"{project.stem}.step"),
                        "--force",
                        pcb_path,
                    ],
                    "step",
                )
            )
        if include_ipc2581:
            commands.append(
                _run_export(
                    runner,
                    pcb_path,
                    package_dir,
                    [
                        "pcb",
                        "export",
                        "ipc2581",
                        "--output",
                        str(package_dir / f"{project.stem}.xml"),
                        pcb_path,
                    ],
                    "ipc2581",
                )
            )

        artifacts = _collect_artifacts(package_dir)
        zip_path = package_dir.with_suffix(".zip")
        _write_zip(zip_path, artifacts, package_dir)
        failed = [command for command in commands if not command["success"]]
        return {
            "success": not failed,
            "tool": "pcb_export_fabrication_package",
            "project_path": validated_project,
            "pcb_path": pcb_path,
            "stage": "fabrication_exported" if not failed else "fabrication_export_failed",
            "output_dir": str(package_dir),
            "zip_path": str(zip_path),
            "artifact_count": len(artifacts),
            "artifacts": [str(path) for path in artifacts],
            "commands": commands,
            "drc": drc,
            "warnings": _fabrication_warnings(drc),
            "error": failed[0].get("error") if failed else None,
        }
    except Exception as exc:
        return {
            "success": False,
            "tool": "pcb_export_fabrication_package",
            "project_path": project_path,
            "stage": "fabrication_exception",
            "error": str(exc),
        }


def _run_export(
    runner: SecureSubprocessRunner,
    pcb_path: str,
    package_dir: Path,
    args: list[str],
    name: str,
) -> dict[str, Any]:
    pcb_dir = os.path.dirname(os.path.realpath(os.path.expanduser(pcb_path))) or os.getcwd()
    try:
        process = runner.run_kicad_command(
            args,
            input_files=[pcb_path],
            working_dir=pcb_dir,
            timeout=120,
        )
        return {
            "name": name,
            "success": process.returncode == 0,
            "returncode": process.returncode,
            "stdout": process.stdout,
            "stderr": process.stderr,
        }
    except Exception as exc:
        return {
            "name": name,
            "success": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "error": str(exc),
            "output_dir": str(package_dir),
        }


def _collect_artifacts(package_dir: Path) -> list[Path]:
    artifacts: list[Path] = []
    for path in package_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() != ".zip":
            artifacts.append(path)
    return sorted(artifacts)


def _write_zip(zip_path: Path, artifacts: list[Path], package_dir: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for artifact in artifacts:
            archive.write(artifact, artifact.relative_to(package_dir))


def _fabrication_warnings(drc: dict[str, Any]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if drc.get("skipped"):
        warnings.append({"type": "drc_skipped", "message": drc.get("reason", "DRC was skipped")})
    elif drc.get("total_violations", 0):
        warnings.append(
            {
                "type": "drc_violations",
                "message": f"DRC reported {drc.get('total_violations')} violation(s)",
            }
        )
    return warnings
