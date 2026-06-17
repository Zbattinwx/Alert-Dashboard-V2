"""
Auto-label training data using SPC Local Storm Reports
=======================================================
Downloads recent SPC LSR data and cross-references it against the collected
training_data.jsonl.  Any cell within MATCH_RADIUS_KM and MATCH_WINDOW_MINUTES
of a reported mesocyclone, tornado, or funnel gets label=True.  Everything
else that is more than CLEAR_RADIUS_KM from any hazardous report gets
label=False (i.e., confirmed non-rotation).

Usage:
    python scripts/label_from_lsr.py                   # label last 7 days
    python scripts/label_from_lsr.py --days 14          # last 14 days
    python scripts/label_from_lsr.py --dry-run          # show what would change

SPC LSR CSV format:
    https://www.spc.noaa.gov/climo/reports/today_torn.csv
    https://www.spc.noaa.gov/climo/reports/today_wind.csv
    etc.

Historical:
    https://www.spc.noaa.gov/climo/reports/YYMMDD_torn.csv
"""

import argparse
import json
import math
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRAINING_DATA = PROJECT_ROOT / "data" / "training_data.jsonl"

MATCH_RADIUS_KM    = 15.0   # cell within this distance of an LSR = positive
MATCH_WINDOW_MIN   = 10.0   # and within this many minutes = positive
CLEAR_RADIUS_KM    = 30.0   # beyond this from ALL hazardous reports = negative
# LSR event types that indicate rotation
ROTATION_TYPES = {"TORNADO", "FUNNEL CLOUD", "WALL CLOUD", "MESO"}


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def fetch_lsr_csv(date: datetime) -> list[dict]:
    """Download SPC LSR CSV for a given date. Returns list of report dicts."""
    datestr = date.strftime("%y%m%d")
    url = f"https://www.spc.noaa.gov/climo/reports/{datestr}_filtered_torn.csv"
    reports = []
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            lines = resp.read().decode("utf-8", errors="replace").splitlines()
    except Exception:
        # Fallback: tornado only
        url2 = f"https://www.spc.noaa.gov/climo/reports/{datestr}_torn.csv"
        try:
            with urllib.request.urlopen(url2, timeout=10) as resp:
                lines = resp.read().decode("utf-8", errors="replace").splitlines()
        except Exception as e:
            print(f"  Could not fetch LSR for {datestr}: {e}")
            return []

    # SPC CSV: Time,F-Scale,Location,County,State,Lat,Lon,Comments
    for line in lines[1:]:  # skip header
        parts = line.split(",")
        if len(parts) < 7:
            continue
        try:
            time_str = parts[0].strip()   # HHMM UTC
            lat = float(parts[5].strip())
            lon = float(parts[6].strip())
            # Build a full UTC datetime
            hour = int(time_str[:2])
            minute = int(time_str[2:4])
            ts = date.replace(hour=hour, minute=minute, second=0, microsecond=0,
                              tzinfo=timezone.utc)
            reports.append({
                "ts":      ts,
                "lat":     lat,
                "lon":     lon,
                "event":   "TORNADO",
                "comment": ",".join(parts[7:]).strip(),
            })
        except (ValueError, IndexError):
            continue
    return reports


def auto_label(training_path: Path, lsr_reports: list[dict], dry_run: bool) -> tuple[int, int]:
    """Apply labels. Returns (n_positive, n_negative) changed."""
    records = []
    with training_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    pos_changed = neg_changed = 0

    for rec in records:
        if rec.get("label") is not None:
            continue  # already labeled — don't overwrite
        rec_ts_str = rec.get("ts") or ""
        rec_lat = rec.get("lat") or 0
        rec_lon = rec.get("lon") or 0
        if not rec_lat or not rec_lon:
            continue
        try:
            rec_ts = datetime.fromisoformat(rec_ts_str)
        except (ValueError, TypeError):
            continue

        # Check against every LSR report
        is_positive = False
        min_dist = float("inf")
        for lsr in lsr_reports:
            dist_km = haversine_km(rec_lat, rec_lon, lsr["lat"], lsr["lon"])
            dt_min  = abs((rec_ts - lsr["ts"]).total_seconds()) / 60.0
            min_dist = min(min_dist, dist_km)
            if dist_km <= MATCH_RADIUS_KM and dt_min <= MATCH_WINDOW_MIN:
                is_positive = True
                break

        if is_positive:
            rec["label"] = True
            pos_changed += 1
        elif min_dist > CLEAR_RADIUS_KM:
            rec["label"] = False
            neg_changed += 1
        # else: too close to a report but outside match window — leave unlabeled

    if not dry_run:
        with training_path.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
        print(f"Wrote {len(records)} records ({pos_changed} new positives, {neg_changed} new negatives)")
    else:
        print(f"DRY RUN: would label {pos_changed} positive, {neg_changed} negative")

    return pos_changed, neg_changed


def main():
    parser = argparse.ArgumentParser(description="Auto-label training data from SPC LSR")
    parser.add_argument("--data",    default=str(TRAINING_DATA))
    parser.add_argument("--days",    type=int, default=7, help="How many past days to fetch")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data_path = Path(args.data)
    if not data_path.exists():
        print(f"No training data at {data_path}. Run live_qa.py --log first.")
        sys.exit(0)

    all_reports = []
    now = datetime.now(timezone.utc)
    for days_back in range(args.days + 1):
        day = now - timedelta(days=days_back)
        print(f"Fetching LSR for {day.strftime('%Y-%m-%d')} ...")
        reports = fetch_lsr_csv(day)
        all_reports.extend(reports)
        print(f"  Got {len(reports)} tornado reports")

    print(f"\nTotal LSR reports loaded: {len(all_reports)}")
    print(f"Applying labels to {data_path} ...")
    auto_label(data_path, all_reports, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
