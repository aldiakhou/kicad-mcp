"""Runtime detection and bootstrap helpers for KiCad's pcbnew Python module."""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import os
from pathlib import Path
import platform
import sys
from types import ModuleType
from typing import Any

_DLL_HANDLES: list[Any] = []
_PCBNEW_MODULE: ModuleType | None = None
_PCBNEW_ERROR: str | None = None


@dataclass(frozen=True)
class PcbnewRuntimePaths:
    """Resolved paths needed to import KiCad's pcbnew module."""

    kicad_bin: Path | None
    site_packages: Path | None


class PcbnewRuntimeError(RuntimeError):
    """Raised when pcbnew cannot be imported or configured."""


def pcbnew_runtime_status(force_refresh: bool = False) -> dict[str, Any]:
    """Return structured status for the direct pcbnew runtime."""
    global _PCBNEW_MODULE, _PCBNEW_ERROR
    if force_refresh:
        _PCBNEW_MODULE = None
        _PCBNEW_ERROR = None

    paths = detect_pcbnew_runtime_paths()
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    python_abi = f"python{sys.version_info.major}{sys.version_info.minor}"
    status: dict[str, Any] = {
        "success": False,
        "available": False,
        "backend": "pcbnew",
        "python_version": python_version,
        "python_executable": sys.executable,
        "python_abi": python_abi,
        "runtime_supported": sys.version_info[:2] == (3, 11),
        "kicad_bin": str(paths.kicad_bin) if paths.kicad_bin else None,
        "site_packages": str(paths.site_packages) if paths.site_packages else None,
    }
    try:
        pcbnew = get_pcbnew(required=True, force_refresh=force_refresh)
    except Exception as exc:
        status["error"] = str(exc)
        if not status["runtime_supported"]:
            status["reason"] = (
                "KiCad 10 pcbnew on this machine is compiled for python311.dll; "
                f"current runtime is {python_abi}."
            )
        return status

    version = ""
    try:
        version = str(pcbnew.GetBuildVersion())
    except Exception:
        version = "unknown"
    status.update(
        {
            "success": True,
            "available": True,
            "kicad_version": version,
            "runtime_supported": True,
        }
    )
    return status


def get_pcbnew(*, required: bool = True, force_refresh: bool = False) -> ModuleType | None:
    """Import and return KiCad's pcbnew module."""
    global _PCBNEW_MODULE, _PCBNEW_ERROR
    if _PCBNEW_MODULE is not None and not force_refresh:
        return _PCBNEW_MODULE

    paths = detect_pcbnew_runtime_paths()
    try:
        _configure_import_paths(paths)
        _PCBNEW_MODULE = importlib.import_module("pcbnew")
        _PCBNEW_ERROR = None
        return _PCBNEW_MODULE
    except Exception as exc:
        _PCBNEW_MODULE = None
        _PCBNEW_ERROR = str(exc)
        if required:
            raise PcbnewRuntimeError(_runtime_error_message(paths, exc)) from exc
        return None


def detect_pcbnew_runtime_paths() -> PcbnewRuntimePaths:
    """Find KiCad's bin and bundled pcbnew site-packages directory."""
    env_site = _existing_path(os.getenv("KICAD_PCBNEW_SITE_PACKAGES"))
    env_bin = _existing_path(os.getenv("KICAD_BIN_PATH"))
    if env_site:
        inferred_bin = env_bin
        if inferred_bin is None:
            for parent in env_site.parents:
                if (parent / "kicad-cli.exe").exists() or (parent / "kicad-cli").exists():
                    inferred_bin = parent
                    break
        return PcbnewRuntimePaths(inferred_bin, env_site)

    candidates: list[Path] = []
    cli_path = _existing_path(os.getenv("KICAD_CLI_PATH"))
    if cli_path:
        candidates.append(cli_path.parent)

    app_path = _existing_path(os.getenv("KICAD_APP_PATH"))
    if app_path:
        candidates.extend(_bin_candidates_from_app_path(app_path))

    candidates.extend(_common_kicad_bin_candidates())
    for kicad_bin in _unique_existing_dirs(candidates):
        site_packages = kicad_bin / "Lib" / "site-packages"
        if (site_packages / "pcbnew.py").exists():
            return PcbnewRuntimePaths(kicad_bin, site_packages)
    return PcbnewRuntimePaths(None, None)


def _configure_import_paths(paths: PcbnewRuntimePaths) -> None:
    if paths.kicad_bin is None or paths.site_packages is None:
        raise PcbnewRuntimeError("KiCad pcbnew Python paths were not found")
    if platform.system() == "Windows" and hasattr(os, "add_dll_directory"):
        handle = os.add_dll_directory(str(paths.kicad_bin))
        _DLL_HANDLES.append(handle)
    path_text = str(paths.kicad_bin)
    os.environ["PATH"] = path_text + os.pathsep + os.environ.get("PATH", "")
    site_text = str(paths.site_packages)
    if site_text not in sys.path:
        sys.path.insert(0, site_text)


def _runtime_error_message(paths: PcbnewRuntimePaths, exc: Exception) -> str:
    message = str(exc)
    if "python311.dll" in message.lower() and sys.version_info[:2] != (3, 11):
        return (
            "pcbnew import failed because KiCad's module requires Python 3.11 "
            f"but the MCP server is running Python {sys.version_info.major}.{sys.version_info.minor}. "
            "Recreate the project environment with `uv sync --python 3.11`."
        )
    if paths.site_packages is None:
        return (
            "pcbnew import failed because KiCad's bundled Python module path was not found. "
            "Set KICAD_CLI_PATH, KICAD_APP_PATH, KICAD_BIN_PATH, or KICAD_PCBNEW_SITE_PACKAGES."
        )
    return f"pcbnew import failed: {message}"


def _bin_candidates_from_app_path(app_path: Path) -> list[Path]:
    if platform.system() == "Darwin":
        return [
            app_path / "Contents" / "Frameworks" / "Python.framework" / "Versions",
            app_path / "Contents" / "MacOS",
        ]
    return [app_path / "bin", *sorted(app_path.glob("*/bin"), reverse=True)]


def _common_kicad_bin_candidates() -> list[Path]:
    system = platform.system()
    if system == "Windows":
        roots = [Path(r"C:\Program Files\KiCad"), Path(r"C:\Program Files (x86)\KiCad")]
        candidates: list[Path] = []
        for root in roots:
            candidates.append(root / "bin")
            if root.exists():
                candidates.extend(sorted(root.glob("*/bin"), reverse=True))
        return candidates
    if system == "Darwin":
        return [
            Path("/Applications/KiCad/KiCad.app/Contents/MacOS"),
            Path("/Applications/KiCad/kicad.app/Contents/MacOS"),
        ]
    return [
        Path("/usr/lib/kicad"),
        Path("/usr/lib/python3/dist-packages"),
        Path("/usr/share/kicad"),
    ]


def _existing_path(raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(os.path.expanduser(raw)).resolve()
    return path if path.exists() else None


def _unique_existing_dirs(candidates: list[Path]) -> list[Path]:
    seen: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.exists() and resolved.is_dir() and resolved not in seen:
            seen.append(resolved)
    return seen
