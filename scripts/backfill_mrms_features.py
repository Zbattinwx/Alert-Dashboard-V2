"""
Backfill MRMS rotation features into existing training data.

Walks `data/training_data.jsonl` and, for each row, queries the historical
MRMS RotationTrack30min and MergedAzShear0to2kmAGL products at the row's
timestamp + lat/lon.  Adds the sampled values to the row's `features` dict.

The expensive part is the GRIB2 downloads (each ~3-5 MB, ~5000-15000 unique
2-minute bins to cover 50k rows).  We:

1. Group rows by (product, 2-min time bin) so each MRMS file is fetched ONCE
   and used to sample every row in that bin.
2. Run S3 downloads in parallel via a ThreadPoolExecutor (network-bound, not
   CPU-bound, so threads are fine and faster than asyncio's executor).
3. Drop any row whose MRMS lookup fails — features stay at 0.0 (consistent
   with the live wiring's degradation).

Usage:
    python scripts/backfill_mrms_features.py
    python scripts/backfill_mrms_features.py --workers 16    # default 8
    python scripts/backfill_mrms_features.py --limit 1000    # test run
    python scripts/backfill_mrms_features.py --output data/training_data.mrms.jsonl

Requires `eccodes` (same as live MRMS services).
"""

import argparse
import gzip
import json
import logging
import os
import sys
import tempfile
import time as time_mod
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.mrms_rotation_service import (  # noqa: E402
    GRID_NORTH, GRID_SOUTH, GRID_WEST, GRID_EAST,
    MRMS_BUCKET,
    PRODUCT_AZSHEAR,
    PRODUCT_ROTATION_TRACK,
    PRODUCT_ROTATION_TRACK_PRIMARY,
    MRMSRotationService,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill-mrms")


TRAINING_DATA = PROJECT_ROOT / "data" / "training_data.jsonl"

# Round to 2-min bins.  Rows in the same bin share an MRMS file.
BIN_SECONDS = 120


def _bin_dt(dt: datetime) -> datetime:
    """Snap to the nearest 2-min boundary."""
    epoch = dt.timestamp()
    return datetime.fromtimestamp(
        round(epoch / BIN_SECONDS) * BIN_SECONDS, tz=timezone.utc,
    )


def _sample_grid(grid: np.ndarray, ni: int, nj: int, lat: float, lon: float) -> Optional[float]:
    if not (GRID_SOUTH <= lat <= GRID_NORTH and GRID_WEST <= lon <= GRID_EAST):
        return None
    lat_span = GRID_NORTH - GRID_SOUTH
    lon_span = GRID_EAST - GRID_WEST
    row = int(round((GRID_NORTH - lat) / lat_span * (nj - 1)))
    col = int(round((lon - GRID_WEST) / lon_span * (ni - 1)))
    row = max(0, min(nj - 1, row))
    col = max(0, min(ni - 1, col))
    v = float(grid[row, col])
    if not np.isfinite(v):
        return None
    return v


def fetch_one(svc: MRMSRotationService, product: str, target_dt: datetime):
    """Worker: fetch and parse one MRMS file.  Returns (bin_key, grid_tuple) or (bin_key, None)."""
    bin_key = (product, target_dt.isoformat())
    result = svc.fetch_grid_at_time(product, target_dt)
    return bin_key, result


def main():
    parser = argparse.ArgumentParser(description="Backfill MRMS rotation features")
    parser.add_argument("--input",   default=str(TRAINING_DATA))
    parser.add_argument("--output",  default=None,
                        help="Output JSONL path.  Defaults to overwriting input "
                             "(makes a .bak first).")
    parser.add_argument("--workers", type=int, default=8,
                        help="Parallel S3 download threads (default 8)")
    parser.add_argument("--limit",   type=int, default=0,
                        help="Process only the first N rows (test mode)")
    parser.add_argument("--products", nargs="+",
                        default=["rotation_track", "azshear"],
                        help="Which features to backfill")
    args = parser.parse_args()

    in_path  = Path(args.input)
    out_path = Path(args.output) if args.output else in_path
    if not in_path.exists():
        logger.error(f"No training data at {in_path}")
        sys.exit(1)

    # ── Load all rows ───────────────────────────────────────────────────
    logger.info(f"Loading {in_path} ...")
    rows = []
    with in_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    logger.info(f"Loaded {len(rows)} rows")
    if args.limit:
        rows = rows[: args.limit]
        logger.info(f"Limited to first {len(rows)} rows (test mode)")

    # ── Group rows by 2-min bin per product ─────────────────────────────
    # Each bin's rows will sample from the same MRMS file.
    products_to_run = []
    if "rotation_track" in args.products:
        products_to_run.append(PRODUCT_ROTATION_TRACK_PRIMARY)  # try 30min first
    if "azshear" in args.products:
        products_to_run.append(PRODUCT_AZSHEAR)

    # bins: {(product, bin_iso): [row_idx, ...]}
    bins: dict[tuple[str, str], list[int]] = {}
    for i, row in enumerate(rows):
        ts_str = row.get("ts")
        lat = row.get("lat")
        lon = row.get("lon")
        if not ts_str or lat is None or lon is None:
            continue
        try:
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        bin_dt = _bin_dt(dt)
        for product in products_to_run:
            bins.setdefault((product, bin_dt.isoformat()), []).append(i)
    logger.info(
        f"Grouped into {len(bins)} (product, 2-min bin) combinations "
        f"across {len(products_to_run)} products"
    )

    # ── Backup before writing ───────────────────────────────────────────
    if out_path == in_path:
        bak = in_path.with_suffix(in_path.suffix + ".pre_mrms_backfill")
        if not bak.exists():
            logger.info(f"Backing up to {bak}")
            bak.write_bytes(in_path.read_bytes())

    # ── Parallel fetch ──────────────────────────────────────────────────
    svc = MRMSRotationService()
    rotation_track_used = PRODUCT_ROTATION_TRACK_PRIMARY  # may be reset to fallback below
    rotation_track_fallback_tested = False
    n_bins = len(bins)
    n_done = 0
    n_fail = 0
    t0 = time_mod.perf_counter()

    # Per-bin sampled values: bin_key → {row_idx → sampled_value}
    samples: dict[tuple[str, str], dict[int, float]] = {}

    bin_items = list(bins.items())

    def _process(bin_key: tuple[str, str]) -> tuple[tuple[str, str], Optional[dict[int, float]]]:
        product, bin_iso = bin_key
        target_dt = datetime.fromisoformat(bin_iso)
        result = svc.fetch_grid_at_time(product, target_dt)
        if result is None:
            return bin_key, None
        grid, ni, nj, _ = result
        row_samples: dict[int, float] = {}
        for ri in bins[bin_key]:
            row = rows[ri]
            v = _sample_grid(grid, ni, nj, float(row["lat"]), float(row["lon"]))
            if v is not None:
                row_samples[ri] = v
        return bin_key, row_samples

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(_process, bk): bk for bk, _ in bin_items
        }
        for fut in as_completed(futures):
            bk = futures[fut]
            try:
                _, sampled = fut.result()
            except Exception as e:
                logger.warning(f"Bin {bk} raised: {e}")
                sampled = None
            n_done += 1
            if sampled is None:
                n_fail += 1
            else:
                samples[bk] = sampled
            if n_done % 50 == 0 or n_done == n_bins:
                elapsed = time_mod.perf_counter() - t0
                rate = n_done / max(elapsed, 0.001)
                eta = (n_bins - n_done) / max(rate, 0.001)
                logger.info(
                    f"  {n_done}/{n_bins} bins  ({n_fail} fail)  "
                    f"rate={rate:.1f}/s  ETA={eta/60:.1f} min"
                )

    # If 30-min rotation track returned nothing for >80% of bins, retry with
    # the fallback product (1440-min) which has wider date coverage.
    rot_bins = [bk for bk in bins if bk[0] == PRODUCT_ROTATION_TRACK_PRIMARY]
    rot_filled = sum(1 for bk in rot_bins if bk in samples)
    if rot_bins and rot_filled / len(rot_bins) < 0.2:
        logger.warning(
            f"30-min rotation track only covered {rot_filled}/{len(rot_bins)} bins; "
            f"retrying with fallback product"
        )
        rotation_track_used = PRODUCT_ROTATION_TRACK
        retry_bins = []
        for bk in rot_bins:
            if bk in samples:
                continue
            target_dt = datetime.fromisoformat(bk[1])
            new_bk = (PRODUCT_ROTATION_TRACK, bk[1])
            bins[new_bk] = bins[bk]
            retry_bins.append(new_bk)
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(_process, bk): bk for bk in retry_bins}
            for fut in as_completed(futures):
                bk = futures[fut]
                try:
                    _, sampled = fut.result()
                except Exception:
                    sampled = None
                if sampled is not None:
                    samples[bk] = sampled

    # ── Write enriched rows ─────────────────────────────────────────────
    logger.info(f"Writing {out_path} ...")
    n_rot_filled = n_az_filled = 0
    out_lines = []
    for i, row in enumerate(rows):
        feats = row.setdefault("features", {})
        # Determine bin for this row
        ts_str = row.get("ts")
        if not ts_str:
            out_lines.append(json.dumps(row) + "\n")
            continue
        try:
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            out_lines.append(json.dumps(row) + "\n")
            continue
        bin_iso = _bin_dt(dt).isoformat()
        # Rotation track
        for product, feat_name in (
            (rotation_track_used, "mrms_rotation_track_30min"),
            (PRODUCT_AZSHEAR,     "mrms_azshear_0_2km"),
        ):
            bk = (product, bin_iso)
            row_samples = samples.get(bk)
            if row_samples is not None and i in row_samples:
                feats[feat_name] = float(row_samples[i])
                if feat_name == "mrms_rotation_track_30min":
                    n_rot_filled += 1
                else:
                    n_az_filled += 1
            else:
                feats.setdefault(feat_name, 0.0)
        out_lines.append(json.dumps(row) + "\n")

    out_path.write_text("".join(out_lines), encoding="utf-8")
    logger.info(
        f"Backfill complete: rotation_track filled {n_rot_filled}/{len(rows)}, "
        f"azshear filled {n_az_filled}/{len(rows)}"
    )


if __name__ == "__main__":
    main()
