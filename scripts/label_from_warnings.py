"""
Auto-label training data using NWS Warning Polygons (IEM SBW Archive)
=======================================================================
This is the CORRECT approach for pre-warning storm detection training data.

LSR-based labeling (label_from_lsr.py) only catches storms that produced
an actual tornado touchdown.  That misses:
  - Confirmed mesocyclones that didn't produce a tornado
  - Strongly rotating supercells under tornado warnings that didn't touch down
  - Every significant storm where a forecaster saw rotation and acted on it

NWS tornado warnings are issued when forecasters SEE ROTATION on radar —
even when no tornado touches down.  These are the ground truth labels we want
for a system designed to detect rotation before alerts are issued.

SVR (severe thunderstorm) warnings give a "significant organized convection"
label that is useful for distinguishing organized storms from ordinary rain.

Ground truth hierarchy:
  1. Tornado Warning (TO.W): STRONG positive — forecaster confirmed rotation
  2. Tornado Watch (TO.A):  WEAK positive — environmental conditions favorable
  3. SVR Warning (SV.W):    MARGINAL positive — organized strong convection
  4. No warning within CLEAR_RADIUS_KM: NEGATIVE (confirmed non-event)

Data source: Iowa Environmental Mesonet (IEM) Storm Based Warning archive
  https://mesonet.agron.iastate.edu/request/gis/watchwarn.phtml
  Archive available since November 2005, free, no authentication required.

Usage:
    python scripts/label_from_warnings.py                   # last 7 days
    python scripts/label_from_warnings.py --days 30         # last 30 days
    python scripts/label_from_warnings.py --stats           # show label counts
    python scripts/label_from_warnings.py --dry-run         # preview, no changes
"""

import argparse
import json
import math
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRAINING_DATA = PROJECT_ROOT / "data" / "training_data.jsonl"

# Matching parameters
MATCH_RADIUS_KM    = 5.0    # cell must be inside warning polygon (point-in-polygon is ideal,
                             # but distance fallback handles coordinate edge cases)
CLEAR_RADIUS_KM    = 30.0   # beyond this from ALL warnings → confirmed negative
MATCH_WINDOW_MIN   = 30.0   # warning issued within this many minutes of cell scan

# Label strengths (0–1, stored as "label_strength" alongside label)
LABEL_TORNADO_WARNING = 1.0    # TO.W: forecaster explicitly saw rotation
LABEL_SVR_WARNING     = 0.4    # SV.W: organized storm, rotation possible
LABEL_NEGATIVE        = 0.0


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def point_in_polygon(lat, lon, polygon_coords) -> bool:
    """Ray-casting point-in-polygon test."""
    x, y = lon, lat
    n = len(polygon_coords)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon_coords[i]
        xj, yj = polygon_coords[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def fetch_iem_warnings(start_dt: datetime, end_dt: datetime,
                       phenomena: list[str]) -> list[dict]:
    """
    Fetch NWS storm-based warnings from IEM GeoJSON API.

    IEM endpoint: /geojson/sbw.py
    Params: sts (start), ets (end), phenomena (e.g. 'TO', 'SV'), significance ('W')
    Returns list of {wtype, issued, expires, polygon_coords, centroid_lat, centroid_lon}
    """
    results = []
    base_url = "https://mesonet.agron.iastate.edu/geojson/sbw.py"

    for phenom in phenomena:
        params = {
            "sts": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ets": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "phenomena": phenom,
            "significance": "W",  # Warnings only (not Watches/Advisories)
        }
        url = base_url + "?" + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AlertDashboardV2/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"  IEM fetch failed for {phenom}.W ({start_dt.date()} to {end_dt.date()}): {e}")
            continue

        features = data.get("features") or []
        for feat in features:
            props = feat.get("properties") or {}
            geom = feat.get("geometry") or {}

            try:
                issued = datetime.fromisoformat(
                    props.get("issue", "").replace("Z", "+00:00")
                )
                expires = datetime.fromisoformat(
                    props.get("expire", "").replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                continue

            # Extract polygon coordinates
            coords = []
            if geom.get("type") == "MultiPolygon":
                for ring_group in geom.get("coordinates", []):
                    for ring in ring_group:
                        coords = [(c[1], c[0]) for c in ring]  # (lat, lon)
                        break
                    break
            elif geom.get("type") == "Polygon":
                outer = geom.get("coordinates", [[]])[0]
                coords = [(c[1], c[0]) for c in outer]

            if not coords:
                continue

            # Centroid for fast distance pre-filter
            c_lat = sum(c[0] for c in coords) / len(coords)
            c_lon = sum(c[1] for c in coords) / len(coords)

            results.append({
                "wtype": phenom,
                "issued": issued,
                "expires": expires,
                "coords": coords,
                "centroid_lat": c_lat,
                "centroid_lon": c_lon,
                "label_strength": LABEL_TORNADO_WARNING if phenom == "TO" else LABEL_SVR_WARNING,
            })

    print(f"  Fetched {len(results)} warnings ({', '.join(f'{phenomena.count(p)}×{p}.W' for p in set(phenomena))})")
    return results


def auto_label(training_path: Path, warnings: list[dict],
               dry_run: bool, overwrite: bool,
               strict_tornado: bool = False) -> tuple[int, int, int]:
    """
    Apply warning-based labels to unlabeled cells.
    Returns (n_positive, n_negative, n_skipped).

    When `strict_tornado` is True, only **tornado** warnings (TO.W) count as
    positives — SVR-only matches become *ambiguous* (label left as None →
    excluded from training).  This sharpens the rotation signal: cells under
    SVR-only warnings are typically intense convection without confirmed
    rotation, so training on them as positives teaches a severe-storm
    classifier rather than a rotation classifier.
    """
    import numpy as np

    records = []
    with training_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    pos_changed = neg_changed = skipped = 0

    # With --overwrite, wipe ALL existing label fields first.  Records that
    # the new evaluation classifies as ambiguous (e.g. in an SVR-only polygon
    # under --strict-tornado, or near-but-not-inside a TOR polygon) stay
    # unlabeled at the end.  Without this, an old TOR/SVR-positive label
    # would persist even when the new logic intends "no label".
    if overwrite:
        for rec in records:
            for k in ("label", "label_strength", "label_source", "label_issued"):
                rec.pop(k, None)

    # ── Pre-extract warning attributes into numpy arrays ────────────────────
    # The per-record loop below previously called haversine_km in pure Python
    # for every (record, warning) pair → 50k × 26k ≈ 1.3 B calls = hours.
    # Vectorising the centroid pre-filter as one numpy op per record drops
    # that to ~50k numpy ops total = seconds.
    n_warn = len(warnings)
    w_lat_arr = np.array([w["centroid_lat"] for w in warnings], dtype=float)
    w_lon_arr = np.array([w["centroid_lon"] for w in warnings], dtype=float)
    w_issued_unix = np.array(
        [w["issued"].timestamp() for w in warnings], dtype=float,
    )
    w_expires_unix = np.array(
        [w["expires"].timestamp() for w in warnings], dtype=float,
    )
    w_strength = np.array([w["label_strength"] for w in warnings], dtype=float)

    R_KM = 6371.0
    PRE_FILTER_KM = 300.0  # must match the original loop's early-exit radius

    progress_every = max(1, len(records) // 20)  # ~5% increments

    for ri, rec in enumerate(records):
        if ri % progress_every == 0:
            print(f"  Labeling {ri}/{len(records)} records "
                  f"({pos_changed} pos, {neg_changed} neg, {skipped} skipped)")

        if rec.get("label") is not None and not overwrite:
            skipped += 1
            continue

        rec_ts_str = rec.get("ts") or ""
        rec_lat = rec.get("lat") or 0
        rec_lon = rec.get("lon") or 0
        if not rec_lat or not rec_lon:
            skipped += 1
            continue
        try:
            rec_ts = datetime.fromisoformat(rec_ts_str)
        except (ValueError, TypeError):
            skipped += 1
            continue
        rec_ts_unix = rec_ts.timestamp()

        # Vectorised haversine: one numpy op against all warnings
        lat1_r = np.radians(rec_lat)
        lat2_r = np.radians(w_lat_arr)
        dlat = lat2_r - lat1_r
        dlon = np.radians(w_lon_arr - rec_lon)
        a = (np.sin(dlat / 2.0) ** 2
             + np.cos(lat1_r) * np.cos(lat2_r) * np.sin(dlon / 2.0) ** 2)
        dist_arr = R_KM * 2.0 * np.arcsin(np.sqrt(a))

        # Stage-1 spatial filter: drop warnings beyond 300 km centroid distance
        near_mask = dist_arr <= PRE_FILTER_KM
        if not near_mask.any():
            # No warning anywhere near this cell — definitively negative
            rec["label"]          = False
            rec["label_strength"] = LABEL_NEGATIVE
            rec["label_source"]   = "no_warning_in_area"
            neg_changed += 1
            continue

        # Stage-2 temporal filter on the spatially-near subset
        dt_issued_min  = np.abs(rec_ts_unix - w_issued_unix) / 60.0
        dt_expires_min = (rec_ts_unix - w_expires_unix) / 60.0
        time_ok = (dt_issued_min <= MATCH_WINDOW_MIN) | (dt_expires_min <= 10.0)
        candidate_mask = near_mask & time_ok
        candidate_idx = np.where(candidate_mask)[0]

        if candidate_idx.size == 0:
            # Warnings nearby but none in the time window — negative.
            rec["label"]          = False
            rec["label_strength"] = LABEL_NEGATIVE
            rec["label_source"]   = "no_warning_in_area"
            neg_changed += 1
            continue

        best_match = None
        min_dist_to_any = float("inf")

        # Stage-3 point-in-polygon for each remaining time-windowed candidate.
        # `min_dist_to_any` becomes 0 once we find any polygon match (or a
        # close-enough fuzzy boundary match); otherwise it tracks the nearest
        # *time-windowed* warning distance.  The downstream branch labels
        # the record negative only if that nearest distance exceeds
        # CLEAR_RADIUS_KM, otherwise leaves it unlabeled (ambiguous).
        for ci in candidate_idx:
            w = warnings[int(ci)]
            d = float(dist_arr[ci])
            inside = point_in_polygon(rec_lat, rec_lon, w["coords"])
            if not inside:
                if d > MATCH_RADIUS_KM:
                    min_dist_to_any = min(min_dist_to_any, d)
                    continue
                # Close enough — counts as fuzzy boundary match.
            min_dist_to_any = 0
            if best_match is None or w["label_strength"] > best_match["label_strength"]:
                best_match = w

        if best_match is not None:
            if strict_tornado and best_match["wtype"] != "TO":
                # Strict mode: SVR-only matches are ambiguous, not positive.
                # Don't write a label — the record stays unlabeled and is
                # excluded from training.  This prevents the classifier
                # from learning "severe storm" instead of "rotation."
                skipped += 1
                continue
            rec["label"]          = True
            rec["label_strength"] = best_match["label_strength"]
            rec["label_source"]   = f"{best_match['wtype']}.W"
            rec["label_issued"]   = best_match["issued"].isoformat()
            pos_changed += 1
        elif min_dist_to_any > CLEAR_RADIUS_KM:
            rec["label"]          = False
            rec["label_strength"] = LABEL_NEGATIVE
            rec["label_source"]   = "no_warning_in_area"
            neg_changed += 1

    if not dry_run:
        with training_path.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
        print(f"Labeled: {pos_changed} positive, {neg_changed} negative "
              f"({skipped} already labeled / skipped)")
    else:
        print(f"DRY RUN: would label {pos_changed} positive, {neg_changed} negative")

    return pos_changed, neg_changed, skipped


def label_stats(path: Path):
    total = labeled = pos = neg = pos_tor = pos_svr = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += 1
            if rec.get("label") is not None:
                labeled += 1
                if rec["label"]:
                    pos += 1
                    src = rec.get("label_source", "")
                    if "TO" in src:
                        pos_tor += 1
                    elif "SV" in src:
                        pos_svr += 1
                else:
                    neg += 1
    print(f"Records: {total} total | {labeled} labeled | {total - labeled} unlabeled")
    print(f"  Positives: {pos}  (Tornado warning: {pos_tor}, SVR warning: {pos_svr})")
    print(f"  Negatives: {neg}")
    if labeled > 0:
        print(f"  Class balance: {pos/labeled*100:.0f}% positive")


def main():
    parser = argparse.ArgumentParser(
        description="Label training data using NWS tornado/SVR warning polygons via IEM"
    )
    parser.add_argument("--data",      default=str(TRAINING_DATA))
    parser.add_argument("--days",      type=int, default=7)
    parser.add_argument("--phenomena", nargs="+", default=["TO", "SV"],
                        help="Warning types: TO=tornado, SV=severe thunderstorm")
    parser.add_argument("--dry-run",   action="store_true")
    parser.add_argument("--overwrite", action="store_true",
                        help="Re-label already-labeled records")
    parser.add_argument("--strict-tornado", action="store_true",
                        help="Only count tornado warnings as positives — SVR-only "
                             "matches are left unlabeled (ambiguous).  Sharpens "
                             "the rotation signal at the cost of fewer positives.")
    parser.add_argument("--stats",     action="store_true")
    args = parser.parse_args()

    data_path = Path(args.data)
    if not data_path.exists():
        print(f"No training data at {data_path}. Run:  python live_qa.py --log")
        sys.exit(0)

    if args.stats:
        label_stats(data_path)
        return

    now = datetime.now(timezone.utc)
    start_dt = now - timedelta(days=args.days)

    print(f"Fetching NWS warnings from IEM: {start_dt.date()} → {now.date()}")
    warnings = fetch_iem_warnings(start_dt, now, args.phenomena)

    if not warnings:
        print("No warnings fetched. Check IEM connectivity or try a longer --days range.")
        sys.exit(0)

    if args.strict_tornado:
        print("Strict mode: only TO.W counts as positive; SVR-only matches left ambiguous")
    print(f"Applying labels to {data_path} ...")
    auto_label(data_path, warnings, dry_run=args.dry_run,
               overwrite=args.overwrite, strict_tornado=args.strict_tornado)


if __name__ == "__main__":
    main()
