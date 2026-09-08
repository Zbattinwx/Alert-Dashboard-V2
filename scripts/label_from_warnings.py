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

# A cell counts as "under" a warning when its scan time falls inside the
# warning's validity, widened at the front by PRE_WARNING_MIN and at the back by
# POST_EXPIRY_MIN.  The front widening is the entire point of this dataset: we
# want the storm state in the half hour BEFORE the forecaster acted, because
# that is what a pre-warning detector has to fire on.
PRE_WARNING_MIN    = 30.0
POST_EXPIRY_MIN    = 10.0
MATCH_WINDOW_MIN   = PRE_WARNING_MIN   # back-compat alias for label_from_lsr.py

# Defensive clamp on a single polygon's validity.  A genuine TO.W runs ~30-60
# min; a multi-day span means we are looking at a mis-typed product, and it
# must not hold a positive window open over a point for days.
MAX_WARNING_MIN    = 180.0

# Label strengths (0–1, stored as "label_strength" alongside label)
LABEL_TORNADO_WARNING = 1.0    # TO.W: forecaster explicitly saw rotation
LABEL_SVR_WARNING     = 0.4    # SV.W: organized storm, rotation possible
LABEL_NEGATIVE        = 0.0

NEWLINE = b"\n"


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def point_in_polygon(lat, lon, polygon_coords) -> bool:
    """Ray-casting point-in-polygon test.

    `polygon_coords` is a list of **(lat, lon)** pairs — the ordering that
    `fetch_iem_warnings` produces when it flips GeoJSON's [lon, lat].

    That ordering is why this unpacks as `yi, xi`.  It previously unpacked
    `xi, yi` while the query point was set up as `x, y = lon, lat`, so the
    crossing test compared each ring vertex's LONGITUDE against the point's
    LATITUDE.  For a warning polygon anywhere in CONUS (lat ~25-49, lon ~-125
    to -67) no vertex can ever straddle the ray, so the function returned False
    for every real polygon and the caller silently fell through to its 5 km
    centroid-distance fallback.  Every positive in the archive was produced by
    that fallback rather than by containment.
    """
    x, y = lon, lat
    n = len(polygon_coords)
    inside = False
    j = n - 1
    for i in range(n):
        yi, xi = polygon_coords[i]
        yj, xj = polygon_coords[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def fetch_iem_warnings(start_dt: datetime, end_dt: datetime,
                       phenomena: list[str]) -> list[dict]:
    """
    Fetch NWS storm-based warnings from IEM GeoJSON API.

    IEM endpoint: /geojson/sbw.py — params sts / ets bound the window.

    **IEM IGNORES the `phenomena` and `significance` parameters on this
    endpoint.**  A request for phenomena=TO&significance=W returns every storm
    based warning active in the window: measured on 2026-05-19→21, 643 features
    came back of which 25 (3.9%) were actually TO.W — the rest were SV.W, flood
    advisories (FA.Y), flood warnings (FL.W), marine warnings (MA.W), flash
    flood (FF.W) and even dust storm (DS.W).

    The previous version trusted the request and stamped every returned feature
    with the phenomenon it had ASKED for, so a 21-day Flood Warning entered the
    training set as a tornado warning at full label strength.  It also issued
    one request per phenomenon, and since each returns the identical unfiltered
    payload, every warning was added TWICE with contradictory strengths (1.0 as
    "TO", 0.4 as "SV").

    So: fetch once, and filter on what the feature actually says it is.

    Returns list of {wtype, issued, expires, coords, centroid_lat, centroid_lon}
    where `issued`/`expires` bound the POLYGON's validity.
    """
    want = {(p, "W") for p in phenomena}
    strength = {"TO": LABEL_TORNADO_WARNING, "SV": LABEL_SVR_WARNING}

    base_url = "https://mesonet.agron.iastate.edu/geojson/sbw.py"
    params = {
        "sts": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ets": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    url = base_url + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AlertDashboardV2/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  IEM fetch FAILED ({start_dt.date()} to {end_dt.date()}): {e}")
        return []

    results = []
    seen_types = {}
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        geom = feat.get("geometry") or {}

        key = (props.get("phenomena"), props.get("significance"))
        seen_types[key] = seen_types.get(key, 0) + 1
        if key not in want:
            continue
        wtype = key[0]

        # Prefer the polygon's own validity over the event's issue/expire: a
        # storm-based warning is re-polygoned by each SVS, and issue→expire
        # spans the whole event rather than the polygon we are testing against.
        def _dt(*keys):
            for k in keys:
                v = props.get(k)
                if v:
                    try:
                        return datetime.fromisoformat(v.replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        pass
            return None

        issued = _dt("polygon_begin", "issue")
        expires = _dt("polygon_end", "expire")
        origin = _dt("issue", "polygon_begin")
        if issued is None or expires is None or expires <= issued:
            continue

        # Defensive clamp.  A genuine TO.W runs ~30-60 min (median 33 measured);
        # anything far longer is a data artefact and must not open a multi-day
        # window over a point on the map.
        if (expires - issued) > timedelta(minutes=MAX_WARNING_MIN):
            expires = issued + timedelta(minutes=MAX_WARNING_MIN)

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

        c_lat = sum(c[0] for c in coords) / len(coords)
        c_lon = sum(c[1] for c in coords) / len(coords)

        results.append({
            "wtype": wtype,
            "issued": issued,
            "expires": expires,
            "origin": origin or issued,
            "coords": coords,
            "centroid_lat": c_lat,
            "centroid_lon": c_lon,
            "label_strength": strength.get(wtype, LABEL_SVR_WARNING),
        })

    kept = {}
    for r in results:
        kept[r["wtype"]] = kept.get(r["wtype"], 0) + 1
    dropped = sum(v for k, v in seen_types.items() if k not in want)
    print(f"  kept {len(results)} ("
          + ", ".join(f"{v}x{k}.W" for k, v in sorted(kept.items()))
          + f"); dropped {dropped} other product(s)")
    return results


def auto_label(training_path: Path, warnings: list[dict],
               dry_run: bool, overwrite: bool,
               strict_tornado: bool = False,
               window_start=None, window_end=None) -> tuple[int, int, int]:
    """
    Apply warning-based labels to unlabeled cells.
    Returns (n_positive, n_negative, n_skipped).

    `window_start` / `window_end` bound the period the `warnings` list actually
    covers.  Records outside that period are LEFT UNTOUCHED: we hold no evidence
    either way for them.  Without this guard the "no warning near this cell"
    branch fires on every record outside the fetched range and manufactures
    false negatives across the entire archive — running with the default
    `--days 7` would relabel four months of history as quiet.

    The file is streamed record-by-record to a temp file and swapped in at the
    end, so peak memory is one record rather than the whole 475 MB archive, and
    a crash mid-run cannot truncate the training data.

    When `strict_tornado` is True, only **tornado** warnings (TO.W) count as
    positives — SVR-only matches become *ambiguous* (label left as None →
    excluded from training).  This sharpens the rotation signal: cells under
    SVR-only warnings are typically intense convection without confirmed
    rotation, so training on them as positives teaches a severe-storm
    classifier rather than a rotation classifier.
    """
    import os
    import tempfile

    import numpy as np

    pos_changed = neg_changed = skipped = 0
    out_of_window = 0

    ws_unix = window_start.timestamp() if window_start else None
    we_unix = window_end.timestamp() if window_end else None

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

    def classify(rec) -> None:
        """Label ONE record in place.  A bare `return` means: move to the next
        record.  Every record is written by the caller either way, so an early
        exit here can never drop a row from the archive."""
        nonlocal pos_changed, neg_changed, skipped, out_of_window

        rec_ts_str = rec.get("ts") or ""
        rec_lat = rec.get("lat") or 0
        rec_lon = rec.get("lon") or 0
        if not rec_lat or not rec_lon:
            skipped += 1
            return
        try:
            rec_ts = datetime.fromisoformat(rec_ts_str)
        except (ValueError, TypeError):
            skipped += 1
            return
        rec_ts_unix = rec_ts.timestamp()

        # Outside the period the fetched warnings cover we have no evidence at
        # all, so we must not touch this record AT ALL.  The negative branches
        # below would otherwise fire on every out-of-range record and quietly
        # convert months of untouched history into "no warning in area".
        #
        # This guard sits BEFORE the --overwrite wipe on purpose: wiping first
        # would strip labels from records this run cannot regenerate, so
        # `--overwrite --days 7` would silently destroy every older label.
        if ((ws_unix is not None and rec_ts_unix < ws_unix)
                or (we_unix is not None and rec_ts_unix > we_unix)):
            out_of_window += 1
            return

        # --overwrite clears existing label fields so that a record the new
        # evaluation considers ambiguous ends up genuinely unlabeled instead of
        # silently keeping its stale label.
        if overwrite:
            for _k in ("label", "label_strength", "label_source", "label_issued"):
                rec.pop(_k, None)

        if rec.get("label") is not None and not overwrite:
            skipped += 1
            return

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
            return

        # Stage-2 temporal filter on the spatially-near subset.
        #
        # The scan must fall inside the warning's validity, widened by
        # PRE_WARNING_MIN at the front and POST_EXPIRY_MIN at the back.
        #
        # This replaces
        #     (|rec - issued| <= 30) | ((rec - expires) <= 10)
        # whose second clause had NO LOWER BOUND — it is satisfied by any scan
        # earlier than the expiry, including one months earlier.  Every cell
        # that ever sat inside a warning polygon therefore matched it whenever
        # that warning was issued, and 15,100 of the archive's 16,449 positives
        # were labelled by a warning issued AFTER the scan (worst case: 69 days
        # after).  The classifier was learning "does this place get tornado
        # warnings", not "is this cell rotating now".
        time_ok = (
            (rec_ts_unix >= w_issued_unix - PRE_WARNING_MIN * 60.0)
            & (rec_ts_unix <= w_expires_unix + POST_EXPIRY_MIN * 60.0)
        )
        candidate_mask = near_mask & time_ok
        candidate_idx = np.where(candidate_mask)[0]

        if candidate_idx.size == 0:
            # Warnings nearby but none in the time window — negative.
            rec["label"]          = False
            rec["label_strength"] = LABEL_NEGATIVE
            rec["label_source"]   = "no_warning_in_area"
            neg_changed += 1
            return

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
                return
            rec["label"]          = True
            rec["label_strength"] = best_match["label_strength"]
            rec["label_source"]   = f"{best_match['wtype']}.W"
            rec["label_issued"]   = best_match.get("origin", best_match["issued"]).isoformat()
            pos_changed += 1
        elif min_dist_to_any > CLEAR_RADIUS_KM:
            rec["label"]          = False
            rec["label_strength"] = LABEL_NEGATIVE
            rec["label_source"]   = "no_warning_in_area"
            neg_changed += 1

    # ── Stream the archive through classify() ──────────────────────────────
    # One record resident at a time, written to a sibling temp file that is
    # atomically swapped in at the end.  The previous version held all 426k
    # records in RAM (~3 GB) and rewrote the file in place with mode "w", so an
    # interruption mid-write truncated the training archive irrecoverably.
    # The live QA reporter appends to this same file while the dashboard runs
    # (`log_file.open("a")` once per scan batch).  Snapshot the size up front,
    # relabel exactly that prefix, then copy back anything that landed while we
    # worked — otherwise the swap silently discards a live storm's scans.
    n = 0
    size0 = training_path.stat().st_size
    tmp_path = training_path.with_suffix(training_path.suffix + ".tmp")
    out = None if dry_run else tmp_path.open("wb")
    consumed = 0
    try:
        with training_path.open("rb") as f:
            for raw in f:
                # Stop once a line STARTS at or past the snapshot: everything
                # from here is the collector's, and belongs in the tail copy.
                if consumed >= size0:
                    break
                consumed += len(raw)
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    if out is not None:
                        out.write(line + b"\n")   # pass malformed rows through untouched
                    continue
                n += 1
                classify(rec)
                if out is not None:
                    out.write(json.dumps(rec).encode("utf-8") + b"\n")
                if n % 100000 == 0:
                    print(f"  {n:,} records ({pos_changed} pos, {neg_changed} neg, "
                          f"{out_of_window} outside window)")

        # Carry over rows appended by the live collector during this run.
        # `consumed` — not size0 — is the resume point: it is exactly how many
        # bytes we processed and wrote, so a line that straddled the snapshot
        # boundary is neither duplicated nor lost.
        if out is not None:
            with training_path.open("rb") as f:
                f.seek(consumed)
                tail = f.read()
            if tail:
                if not tail.endswith(b"\n"):
                    tail += b"\n"
                out.write(tail)
                print(f"  carried over {tail.count(NEWLINE)} row(s) appended during the run")
    finally:
        if out is not None:
            out.close()

    if not dry_run:
        os.replace(tmp_path, training_path)
        print(f"Labeled: {pos_changed} positive, {neg_changed} negative "
              f"({skipped} already labeled / skipped, "
              f"{out_of_window} outside the fetched warning window)")
    else:
        tmp_path.unlink(missing_ok=True)
        print(f"DRY RUN: would label {pos_changed} positive, {neg_changed} negative "
              f"({out_of_window} outside the fetched warning window)")

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


def data_time_range(path: Path):
    """(earliest, latest) record timestamp in the archive, or (None, None)."""
    lo = hi = None
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ts = json.loads(line).get("ts")
            except json.JSONDecodeError:
                continue
            if not ts:
                continue
            if lo is None or ts < lo:
                lo = ts
            if hi is None or ts > hi:
                hi = ts
    to_dt = lambda s: datetime.fromisoformat(s) if s else None
    return to_dt(lo), to_dt(hi)


def data_day_blocks(path: Path, pad_days: int = 1) -> list[tuple]:
    """Contiguous [start, end] windows covering the days the archive actually has.

    A backfilled archive is SPARSE: replaying 2019-05-27, 2024-05-07 and two 2026
    days spans seven years but contains four days of data.  Fetching the whole
    span in 7-day chunks would be ~370 IEM requests to label 4 days, and every
    one of those requests is a chance for a chunk to fail and silently look like
    a quiet period.  So cluster the timestamps and fetch only what is covered.
    """
    days = set()
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ts = json.loads(line).get("ts")
            except json.JSONDecodeError:
                continue
            if ts:
                days.add(ts[:10])
    if not days:
        return []

    ordered = sorted(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                     for d in days)
    blocks = []
    lo = hi = ordered[0]
    for d in ordered[1:]:
        # Merge days that are adjacent or nearly so; anything further apart is a
        # separate event and deserves its own request.
        if (d - hi).days <= (pad_days * 2 + 1):
            hi = d
        else:
            blocks.append((lo, hi))
            lo = hi = d
    blocks.append((lo, hi))
    pad = timedelta(days=pad_days)
    return [(a - pad, b + pad + timedelta(days=1)) for a, b in blocks]


def fetch_warnings_range(start_dt: datetime, end_dt: datetime,
                         phenomena: list[str], chunk_days: int = 14) -> list[dict]:
    """Fetch IEM warnings across an arbitrary span, in chunks.

    IEM will time out or truncate on a multi-month single query, and a silently
    short warning list is indistinguishable from a quiet period — which is
    exactly the failure that produces false negatives.  Chunking keeps each
    request small and lets one failed chunk be visible rather than fatal.
    """
    all_w = []
    cur = start_dt
    while cur < end_dt:
        nxt = min(cur + timedelta(days=chunk_days), end_dt)
        print(f"  {cur.date()} -> {nxt.date()}")
        all_w.extend(fetch_iem_warnings(cur, nxt, phenomena))
        cur = nxt
    print(f"  TOTAL: {len(all_w)} warnings across {(end_dt - start_dt).days} days")
    return all_w


def main():
    parser = argparse.ArgumentParser(
        description="Label training data using NWS tornado/SVR warning polygons via IEM"
    )
    parser.add_argument("--data",      default=str(TRAINING_DATA))
    parser.add_argument("--days",      type=int, default=7)
    parser.add_argument("--all",       action="store_true",
                        help="Cover the archive's ENTIRE time span (derived from the "
                             "data itself) instead of the trailing --days window.")
    parser.add_argument("--phenomena", nargs="+", default=["TO", "SV"],
                        help="Warning types: TO=tornado, SV=severe thunderstorm")
    parser.add_argument("--dry-run",   action="store_true")
    parser.add_argument("--overwrite", action="store_true",
                        help="Re-label already-labeled records")
    parser.add_argument("--strict-tornado", action="store_true",
                        help="Only count tornado warnings as positives — SVR-only "
                             "matches are left unlabeled (ambiguous).  Sharpens "
                             "the rotation signal at the cost of fewer positives.")
    parser.add_argument("--chunk-days", type=int, default=7,
                        help="Fetch IEM in chunks this many days wide. The endpoint "
                             "returns every product type, so a wide chunk is a large "
                             "payload, and a truncated one looks like a quiet period.")
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
    warnings = []
    if args.all:
        blocks = data_day_blocks(data_path)
        if not blocks:
            print("No timestamped records in the archive.")
            sys.exit(0)
        lo, hi = data_time_range(data_path)
        print(f"Archive spans {lo.isoformat()} -> {hi.isoformat()} "
              f"across {len(blocks)} block(s) of actual data")
        for a, b in blocks:
            b = min(b, now)
            print(f"Fetching NWS warnings from IEM: {a.date()} -> {b.date()}")
            warnings.extend(fetch_warnings_range(a, b, args.phenomena,
                                                 chunk_days=args.chunk_days))
        # The window guard must span every block, or records in the later blocks
        # get treated as out-of-range and left unlabelled.
        start_dt = min(a for a, _ in blocks)
        end_dt   = min(max(b for _, b in blocks), now)
    else:
        start_dt = now - timedelta(days=args.days)
        end_dt   = now
        print(f"Fetching NWS warnings from IEM: {start_dt.date()} -> {end_dt.date()}")
        warnings = fetch_warnings_range(start_dt, end_dt, args.phenomena,
                                        chunk_days=args.chunk_days)

    if not warnings:
        print("No warnings fetched. Check IEM connectivity or try a longer --days range.")
        sys.exit(0)

    if args.strict_tornado:
        print("Strict mode: only TO.W counts as positive; SVR-only matches left ambiguous")
    print(f"Applying labels to {data_path} ...")
    auto_label(data_path, warnings, dry_run=args.dry_run,
               overwrite=args.overwrite, strict_tornado=args.strict_tornado,
               window_start=start_dt, window_end=end_dt)


if __name__ == "__main__":
    main()
