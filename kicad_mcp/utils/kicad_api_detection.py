"""
Utility functions for detecting and selecting available KiCad API approaches.
"""

import logging

from kicad_mcp.utils.kicad_cli import is_kicad_cli_available

logger = logging.getLogger(__name__)


def check_for_cli_api() -> bool:
    """Check if KiCad CLI API is available.

    Returns:
        True if KiCad CLI is available, False otherwise
    """
    try:
        available = is_kicad_cli_available()
        if available:
            logger.info("KiCad CLI API is available")
        else:
            logger.info("KiCad CLI API is not available")
        return available
    except Exception as e:
        logger.exception("Error checking for KiCad CLI API: %s", e)
        return False
