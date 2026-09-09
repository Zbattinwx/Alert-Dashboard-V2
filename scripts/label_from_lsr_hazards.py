"""Per-hazard labels from SPC Local Storm Reports: hail size and wind speed.

WHY WARNING LABELS CANNOT DO THIS
---------------------------------
The existing labels come from NWS warning polygons, and a severe thunderstorm
warning does not say WHICH hazard it was issued for. So `p_severe` can only ever
mean "a forecaster would warn this storm" -- useful, and honest, but it cannot
answer "how big will the hail be" or "will this produce damaging wind", because
the label it learns from does not contain that information.

Storm reports do. Each carries a hazard type and a magnitude: hail diameter in
hundredths of an inch, wind gust in knots. That makes them the right ground
truth for hazard-specific probabilities, and it is what ProbSevere and its
descendants train on for the same reason.

WHAT IS AND IS NOT CLAIMED
--------------------------
These produce PROBABILITIES OF EXCEEDING A THRESHOLD, not estimates of a value.
  hail_1in   -- P(a report of >= 1.00" hail)   (the NWS severe criterion)
  hail_2in   -- P(a report of >= 2.00" hail)   (significant severe)
  wind_severe-- P(a report of >= 50 kt gust)   (the NWS severe criterion)
  wind_sig   -- P(a report of >= 65 kt gust)   (significant severe)

Not a predicted hail size and not a predicted gust in mph. For hail, MESH is
already the calibrated radar estimate of size and should be shown for that; for
wind there is NO published conversion from radar-measured velocity aloft to a
surface gust, so a number would be invented. A probability of exceeding a
threshold is the strongest honest claim the data supports.

THE BIAS THAT COMES WITH LSRs, STATED UP FRONT
----------------------------------------------
Storm reports are population-biased: the same storm produces reports over a town
and none over farmland, and hail reports cluster on roads and daylight hours.
A model trained on these learns "severe AND someone was there to see it". That
is a real limitation of the label, not of the storm, and it argues for reading
these as relative rankings rather than absolute frequencies. Warning-based
labels have the opposite bias (a forecaster's judgement, available everywhere),
which is why both are kept rather than one replacing the other.

Labels are written under `hazard_labels` so they sit alongside, and never
overwrite, the warning-based `label` field.

Usage:
    python scripts/label_from_lsr_hazards.py --days 30
    python scripts/label_from_lsr_hazards.py --days 7 --dry-run
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRAINING_DATA = PROJECT_ROOT / "data" / "training_data.jsonl"

# A report is attributed to a cell within this distance and time. Tighter than
# the warning matcher's 30 minutes because a report is a POINT EVENT with a
# known time, not a polygon valid for an hour -- widening it would credit a
# storm for hail that fell before it arrived.
MATCH_RADIUS_KM = 20.0
MATCH_WINDOW_MIN = 15.0

# NWS severe criteria. Hail in inches, wind in knots.
HAIL_SEVERE_IN = 1.00
HAIL_SIG_IN = 2.00
WIND_SEVERE_KT = 50.0
WIND_SIG_KT = 65.0

HAZARDS = ("hail_1in", "hail_2in", "wind_severe", "wind_sig")


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "TBF-Hub/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def fetch_reports(day: datetime) -> list[dict]:
    """Hail and wind reports for one CONVECTIVE day (12Z -> 12Z).

    SPC files are named for the convective day, and rows with HHMM < 1200
    belong to the NEXT calendar day -- the same rule the app's event recap
    uses. Getting this wrong shifts overnight reports by 24 hours, which is
    exactly the population of reports a matcher would then silently miss.
    """
    # The FILTERED file, deliberately: SPC has already deduplicated it and cut
    # it to severe criteria, so every hail row is >= 1.00" and every wind row
    # >= 50 kt -- exactly the thresholds below. Verified on 2026-09-06: 35 hail
    # reports 1.00-3.25", 21 wind reports 58-73 kt. The unfiltered `_rpts.csv`
    # carries sub-severe and duplicate entries that would need cleaning first.
    datestr = day.strftime("%y%m%d")
    url = f"https://www.spc.noaa.gov/climo/reports/{datestr}_rpts_filtered.csv"
    try:
        text = _fetch(url)
    except Exception as e:                           # noqa: BLE001
        print(f"  reports unavailable ({e})")
        return []

    # ONE file holds all three hazards, split by repeated header rows -- the
    # tornado section, then wind, then hail. There is no separate _hail.csv;
    # asking for one returns a 404 page with a 200-shaped body. The hazard is
    # identified by which magnitude column its header carries: "Speed" for wind
    # (knots), "Size" for hail (hundredths of an inch), "F_Scale" for tornado.
    out: list[dict] = []
    kind: str | None = None
    header: list[str] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        cols = next(csv.reader([raw]))
        if cols and cols[0] == "Time":
            header = cols
            kind = ("wind" if "Speed" in cols
                    else "hail" if "Size" in cols
                    else None)          # tornado section: handled elsewhere
            continue
        if kind is None or not header:
            continue
        row = dict(zip(header, cols))
        tm = (row.get("Time") or "").strip()
        if not tm.isdigit() or len(tm) != 4:
            continue
        hh, mm = int(tm[:2]), int(tm[2:])
        when = day.replace(hour=hh, minute=mm, second=0, microsecond=0,
                           tzinfo=timezone.utc)
        # The file is a CONVECTIVE day, 12Z to 12Z, so a time before 1200
        # belongs to the NEXT calendar day. Without this, every overnight
        # report -- which is most of a nocturnal MCS -- lands 24 hours out and
        # silently matches nothing.
        if hh < 12:
            when += timedelta(days=1)
        try:
            lat = float(row.get("Lat") or "")
            lon = float(row.get("Lon") or "")
            mag = float(row.get("Size" if kind == "hail" else "Speed") or "")
        except ValueError:
            continue
        out.append({
            "kind": kind, "lat": lat, "lon": lon,
            # Hail "Size" is hundredths of an inch (175 = 1.75"); wind is knots.
            "value": mag / 100.0 if kind == "hail" else mag,
            "epoch": when.timestamp(),
        })
    return out


def hazards_for(reports: list[dict]) -> dict[str, int]:
    """Which thresholds this set of matched reports crosses."""
    hail = max((r["value"] for r in reports if r["kind"] == "hail"), default=0.0)
    wind = max((r["value"] for r in reports if r["kind"] == "wind"), default=0.0)
    return {
        "hail_1in": int(hail >= HAIL_SEVERE_IN),
        "hail_2in": int(hail >= HAIL_SIG_IN),
        "wind_severe": int(wind >= WIND_SEVERE_KT),
        "wind_sig": int(wind >= WIND_SIG_KT),
    }


def archive_days(path: Path) -> list:
    """The distinct CONVECTIVE days present in a training archive.

    A convective day runs 12Z to 12Z, and SPC names its report files that way,
    so a scan at 03Z belongs to the PREVIOUS day's file. Deriving the day list
    from the data means one request per day that can actually match something.
    """
    from datetime import date
    seen = set()
    with path.open(encoding="utf-8") as f:
        for line in f:
            i = line.find('"ts"')
            if i < 0:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = rec.get("ts") or ""
            try:
                dt = datetime.fromisoformat(ts)
            except ValueError:
                continue
            seen.add((dt - timedelta(hours=12)).date())
    return sorted(seen)


def label_file(path: Path, reports: list[dict], dry_run: bool) -> dict:
    """Attach hazard labels in place. Returns counts."""
    if not reports:
        return {"rows": 0, "matched": 0}
    counts = {h: 0 for h in HAZARDS}
    rows = matched = 0
    tmp = path.with_suffix(".hazlabel.tmp")
    out = None if dry_run else tmp.open("w", encoding="utf-8")
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    if out:
                        out.write(line + "\n")
                    continue
                rows += 1
                lat, lon, ts = rec.get("lat"), rec.get("lon"), rec.get("ts")
                if lat is not None and lon is not None and ts:
                    try:
                        t = datetime.fromisoformat(ts).timestamp()
                    except ValueError:
                        t = None
                    if t is not None:
                        near = [
                            r for r in reports
                            if abs(r["epoch"] - t) <= MATCH_WINDOW_MIN * 60.0
                            and haversine_km(lat, lon, r["lat"], r["lon"]) <= MATCH_RADIUS_KM
                        ]
                        # Absence of a report is NOT proof of absence of hail --
                        # see the bias note in the module docstring -- but for a
                        # cell on a day we DID collect reports for, it is the
                        # best negative available, and every hazard model needs
                        # negatives. Rows on days with no report file at all are
                        # left untouched rather than labelled zero.
                        haz = hazards_for(near)
                        rec["hazard_labels"] = haz
                        if near:
                            matched += 1
                        for h, v in haz.items():
                            counts[h] += v
                if out:
                    out.write(json.dumps(rec) + "\n")
    finally:
        if out:
            out.close()
    if not dry_run:
        tmp.replace(path)
    return {"rows": rows, "matched": matched, **counts}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--from-archive", action="store_true",
                    help="Fetch reports only for the CONVECTIVE DAYS the archive "
                         "actually contains, instead of every day back from "
                         "today. The archive spans 2019-2026 but holds only a "
                         "few hundred days; --days 2660 would be 2660 requests "
                         "for a couple of hundred useful files.")
    ap.add_argument("--data", default=str(TRAINING_DATA))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    path = Path(args.data)
    if not path.exists():
        print(f"No training archive at {path}")
        return 1

    if args.from_archive:
        days = archive_days(path)
        print(f"{len(days)} convective days present in the archive")
    else:
        today = datetime.now(timezone.utc)
        days = [(today - timedelta(days=i)).date() for i in range(args.days)]

    reports: list[dict] = []
    for i, d in enumerate(days, 1):
        day = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        got = fetch_reports(day)
        if got or not args.from_archive:
            print(f"[{i}/{len(days)}] {day:%Y-%m-%d}: {len(got)} hail/wind reports")
        reports.extend(got)

    if not reports:
        print("No reports fetched - nothing to label.")
        return 0

    res = label_file(path, reports, args.dry_run)
    print(f"\n{'(dry run) ' if args.dry_run else ''}{res['rows']:,} rows, "
          f"{res['matched']:,} matched a report")
    for h in HAZARDS:
        print(f"  {h:12s} positives: {res.get(h, 0):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
