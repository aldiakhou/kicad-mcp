"""JSON-safe preview metadata helpers."""

from pathlib import Path
from typing import Any

SVG_MIME_TYPE = "image/svg+xml"


def svg_preview_metadata(svg_path: str, *, kind: str = "svg") -> dict[str, Any]:
    """Return serializable metadata for an SVG preview file."""
    path = Path(svg_path)
    return {
        "kind": kind,
        "path": str(path),
        "mime_type": SVG_MIME_TYPE,
        "file_size": path.stat().st_size,
    }
