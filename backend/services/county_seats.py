"""
County-seat lookup.

Maps a county UGC code (e.g. "OHC017") to its county seat city and coordinates,
loaded from backend/data/us_county_seats.csv (built by build_county_seats.py
from GeoNames PPLA2/PPLA admin-center data).

Used by the broadcast graphic to label each affected county with its seat.
"""
import csv
import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CSV = Path(__file__).resolve().parent.parent / "data" / "us_county_seats.csv"


@lru_cache(maxsize=1)
def _seats() -> dict:
    """(state, county_fips) -> (seat_name, lat, lon). Cached on first use."""
    out: dict[tuple[str, str], tuple[str, float, float]] = {}
    try:
        with open(_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    out[(r["state"], r["county_fips"])] = (
                        r["seat"], float(r["lat"]), float(r["lng"])
                    )
                except (ValueError, KeyError):
                    continue
    except FileNotFoundError:
        logger.warning(
            "us_county_seats.csv not found — run backend/data/build_county_seats.py"
        )
    return out


def get_county_seat(ugc: str) -> Optional[tuple[str, float, float]]:
    """Return (seat_name, lat, lon) for a county UGC like 'OHC017', else None.

    Only county-type UGCs ("C") map to FIPS counties; forecast-zone UGCs ("Z")
    return None (the caller falls back to the largest town in the polygon).
    """
    if not ugc or len(ugc) < 6:
        return None
    state = ugc[:2].upper()
    if ugc[2].upper() != "C":
        return None
    fips = ugc[3:6]
    return _seats().get((state, fips))
