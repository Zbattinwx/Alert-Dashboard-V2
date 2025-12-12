"""
UGC (Universal Geographic Code) Service for Alert Dashboard V2.

This module handles mapping UGC codes to human-readable county/zone names.
UGC codes include:
- County codes (e.g., OHC049 = Franklin County, OH)
- Zone codes (e.g., OHZ049 = Central Ohio zone)

The mapping data comes from ugc_map.json which was generated from NWS data.
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Global UGC mapping dictionary
_ugc_map: dict[str, str] = {}


def load_ugc_map(data_path: Optional[Path] = None) -> bool:
    """
    Load the UGC to name mapping from JSON file.

    Args:
        data_path: Path to ugc_map.json (default: backend/data/ugc_map.json)

    Returns:
        True if loaded successfully, False otherwise
    """
    global _ugc_map

    if data_path is None:
        # Default path relative to this file
        data_path = Path(__file__).parent.parent / "data" / "ugc_map.json"

    try:
        if not data_path.exists():
            logger.warning(f"UGC map file not found: {data_path}")
            return False

        with open(data_path, "r", encoding="utf-8") as f:
            _ugc_map = json.load(f)

        logger.info(f"Loaded {len(_ugc_map)} UGC codes from {data_path}")
        return True

    except Exception as e:
        logger.error(f"Error loading UGC map: {e}")
        return False


def get_ugc_name(ugc_code: str) -> str:
    """
    Get the human-readable name for a UGC code.

    Args:
        ugc_code: UGC code (e.g., "OHC049", "OHZ049", "MDZ509")

    Returns:
        Human-readable name (e.g., "Franklin County, OH") or the code if not found
    """
    if not _ugc_map:
        load_ugc_map()

    # Try direct lookup first
    if ugc_code in _ugc_map:
        return _ugc_map[ugc_code]

    # Some codes may be stored without the middle letter (legacy format)
    # e.g., "MD509" instead of "MDZ509"
    if len(ugc_code) == 6:
        short_code = ugc_code[:2] + ugc_code[3:]
        if short_code in _ugc_map:
            return _ugc_map[short_code]

    # Return the original code if not found
    return ugc_code


def get_display_locations(ugc_codes: list[str], max_display: int = 5) -> str:
    """
    Convert a list of UGC codes to a display-friendly string.

    Args:
        ugc_codes: List of UGC codes
        max_display: Maximum number of locations to show before truncating

    Returns:
        Formatted string like "Franklin County, OH; Delaware County, OH; +3 more"
    """
    if not ugc_codes:
        return ""

    # Get names for all codes
    names = []
    seen = set()  # Avoid duplicates

    for code in ugc_codes:
        name = get_ugc_name(code)
        if name not in seen:
            names.append(name)
            seen.add(name)

    if len(names) <= max_display:
        return "; ".join(names)
    else:
        displayed = "; ".join(names[:max_display])
        remaining = len(names) - max_display
        return f"{displayed}; +{remaining} more"


def get_county_names_list(ugc_codes: list[str]) -> list[str]:
    """
    Convert a list of UGC codes to a list of county/zone names.

    Args:
        ugc_codes: List of UGC codes

    Returns:
        List of human-readable names (deduplicated)
    """
    if not ugc_codes:
        return []

    names = []
    seen = set()

    for code in ugc_codes:
        name = get_ugc_name(code)
        if name not in seen:
            names.append(name)
            seen.add(name)

    return names


def is_ugc_map_loaded() -> bool:
    """Check if the UGC map has been loaded."""
    return len(_ugc_map) > 0


def get_ugc_map_size() -> int:
    """Get the number of UGC codes in the map."""
    return len(_ugc_map)
