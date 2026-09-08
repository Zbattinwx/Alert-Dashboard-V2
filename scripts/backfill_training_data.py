"""
Rebuild training data from ARCHIVED severe-weather days.
=======================================================

Replays past NEXRAD Level 2 volumes through the live storm-tracking pipeline and
writes the same training rows the live collector would have written, so a season
of ground truth can be assembled in an afternoon instead of waiting for the next
one.  Archive coverage goes back to 1991; IEM warning polygons to 2005.

## The one rule that matters

**Offline features must come out byte-identical to live features.**  If the
backfill computes them any other way, the model trains on one distribution and
infers on another, and nothing will raise — it will just be quietly wrong.

So this script does NOT reimplement anything.  It builds the same
`VolumeScanData` the live path builds and hands it to the same
`StormTrackingService._process_sync`, then calls the same
`live_qa_service.build_training_record`.  The grid comes from
`NexradService._create_grid` and the dealiasing from
`NexradService.dealias_radar_in_place`, both borrowed from the live service
rather than copied.  `tests/scripts/test_backfill_parity.py` asserts the row
shape matches.

## Known, deliberate gap: MRMS features

`mrms_rotation_track_30min` and `mrms_azshear_0_2km` are sampled from the LIVE
MRMS cache, which holds nothing for a historical date.  They come out 0.0 here,
exactly as they did for every row collected before that feature was wired in.
Fill them afterwards with `scripts/backfill_mrms_features.py`, which does the
historical S3 lookup, or leave them at 0.0 and accept that those two features
carry no signal for backfilled rows.

## Usage

    # Which days are even worth replaying?
    python scripts/backfill_training_data.py --list-days \
        --start 2026-04-01 --end 2026-08-31 --sites KILN KIND

    # Replay them (writes data/training_data.backfill.jsonl)
    python scripts/backfill_training_data.py \
        --start 2026-04-01 --end 2026-08-31 --sites KILN KIND

    # One specific day
    python scripts/backfill_training_data.py --days 2026-05-18 --sites KILN

Then label and merge:

    python scripts/label_from_warnings.py --data data/training_data.backfill.jsonl \
        --all --overwrite --strict-tornado
    cat data/training_data.backfill.jsonl >> data/training_data.jsonl

Volumes are processed in time order per (site, day) with a FRESH tracker, since
cell tracking is stateful and must not carry across a day boundary.  Progress is
checkpointed, so `--resume` picks up where an interrupted run stopped.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

ARCHIVE_BUCKET = "unidata-nexrad-level2"
DEFAULT_OUT = PROJECT_ROOT / "data" / "training_data.backfill.jsonl"
STATE_OUT = PROJECT_ROOT / "data" / "backfill_state.json"

# The dashboard reads progress from these, so a run started from a terminal is
# just as visible as one started from the UI.  Without them the Model page can
# only report on jobs it launched itself, which is exactly backwards: the long
# runs are the ones you kick off by hand and then want to watch.
JOB_LOG = PROJECT_ROOT / "data" / "backfill_job.log"
JOB_META = PROJECT_ROOT / "data" / "backfill_job.json"

# A warning this far from the radar is not usable ground truth for it: the cell
# would sit at the edge of, or beyond, the useful scan.
WARNING_NEAR_SITE_KM = 230.0

# Replaying a whole UTC day is ~250-350 volumes, most of them clear air at 3am.
# Only the window around the day's warnings carries both the positives and the
# negatives worth having (real storms that were NOT warned), so the default is
# to replay that window with padding rather than the day.
PAD_BEFORE_H = 2.0
PAD_AFTER_H = 1.0

# Tornado warnings on one (site, day) are clustered into separate windows when
# they are further apart than this.  The old "first warning to last warning"
# span was the single biggest waste in a season replay: 2024-08-05 KIWX had TWO
# tornado warnings ~20 h apart and got a 23.7 h window -- 356 volumes for 0.6
# warnings per 100.  Two padded clusters cover the same positives in ~60.
CLUSTER_GAP_H = 2.0

# Volumes downloaded ahead of the decoder.  Small on purpose: each is
# 10-30 MB on disk, and gridding is slow enough that a deep queue only
# buys temp-file bloat.
PREFETCH = 3

# Moments actually consumed: reflectivity (cell ID, dual-pol, LLSD), velocity
# (dealiased in place, rotation + LLSD), cross_correlation_ratio and
# differential_reflectivity (dual-pol / hail).  Verified against the tracker's
# radar.fields and grid.fields accesses.
READ_FIELDS = ("reflectivity", "velocity",
               "cross_correlation_ratio", "differential_reflectivity")


_JOB_LOG_FH = None


def _lower_priority() -> None:
    """Run this process below normal priority.

    A full-season replay is a day of work, and the point of it is to run WHILE
    the machine is used for something else. At normal priority six workers
    make the desktop sluggish enough to abandon the run; below normal, the
    foreground wins every contested core and the replay soaks up what is left.
    Throughput on an otherwise idle box is unchanged.
    """
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            # Declare the signatures.  Without restype, GetCurrentProcess()'s
            # pseudo-handle comes back as a 32-bit -1, which is NOT a valid
            # HANDLE on 64-bit Windows; SetPriorityClass then returns 0 and
            # nothing changes -- which is exactly what happened on the first
            # 6-worker run (all six stayed at Normal).
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            k32.SetPriorityClass.restype = wintypes.BOOL
            BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
            if not k32.SetPriorityClass(k32.GetCurrentProcess(),
                                        BELOW_NORMAL_PRIORITY_CLASS):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            os.nice(10)
    except Exception as e:
        print(f"  (could not lower priority: {e})", flush=True)


def _open_job_log(truncate: bool) -> None:
    """Mirror this process's log into the file the dashboard polls.

    The parent truncates (its log IS the current job's progress feed); workers
    append.  Three processes appending can in principle interleave a line, but
    the reader matches per line and simply ignores anything malformed, so the
    worst case is one missed progress tick.
    """
    global _JOB_LOG_FH
    try:
        JOB_LOG.parent.mkdir(parents=True, exist_ok=True)
        _JOB_LOG_FH = JOB_LOG.open("w" if truncate else "a", encoding="utf-8")
    except OSError:
        _JOB_LOG_FH = None


def _log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if _JOB_LOG_FH is not None:
        try:
            _JOB_LOG_FH.write(line + "\n")
            _JOB_LOG_FH.flush()
        except Exception:
            pass


# ── Day selection ─────────────────────────────────────────────────────────────

def cluster_windows(intervals, gap_h: float = CLUSTER_GAP_H) -> list:
    """Merge [start, end] intervals that fall within `gap_h` of each other."""
    out = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1] + timedelta(hours=gap_h):
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def find_severe_days(start: datetime, end: datetime, sites: list[str],
                     min_tor: int = 1, chunk_days: int = 7) -> dict:
    """Days with tornado warnings near the given radars.

    Returns {(day, site): {"tor": n, "svr": n, "first": dt, "last": dt}}.
    Uses the same IEM fetch the labeller uses, so the days we replay and the
    warnings we later label with come from one source of truth.
    """
    from scripts.label_from_warnings import fetch_warnings_range, haversine_km
    from scripts.train_rotation_model import convective_day
    from backend.services.nexrad_sites import NEXRAD_SITES

    coords = {}
    for s in sites:
        info = NEXRAD_SITES.get(s.upper())
        if not info:
            _log(f"  unknown site {s} - skipping")
            continue
        coords[s.upper()] = (info["lat"], info["lon"])
    if not coords:
        return {}

    warnings = fetch_warnings_range(start, end, ["TO", "SV"], chunk_days=chunk_days)
    out: dict = defaultdict(lambda: {"tor": 0, "svr": 0, "first": None, "last": None,
                                     "tor_intervals": []})
    for w in warnings:
        for site, (slat, slon) in coords.items():
            if haversine_km(slat, slon, w["centroid_lat"], w["centroid_lon"]) > WARNING_NEAR_SITE_KM:
                continue
            key = (convective_day(w["issued"].isoformat()), site)
            rec = out[key]
            rec["tor" if w["wtype"] == "TO" else "svr"] += 1
            if w["wtype"] == "TO":
                rec["tor_intervals"].append((w["issued"], w["expires"]))
            if rec["first"] is None or w["issued"] < rec["first"]:
                rec["first"] = w["issued"]
            if rec["last"] is None or w["expires"] > rec["last"]:
                rec["last"] = w["expires"]

    kept = {}
    for k, v in out.items():
        if v["tor"] < min_tor:
            continue
        # Windows are built from TORNADO warnings only.  SVR-only hours add
        # negatives, and negatives are the one thing the archive is not short
        # of.  A site-day that passes min_tor=0 with no TOR at all keeps the
        # whole first..last span as its single window, as before.
        v["windows"] = (cluster_windows(v.pop("tor_intervals"))
                        if v["tor"] else [(v["first"], v["last"])])
        kept[k] = v
    return kept


# ── Archive listing ───────────────────────────────────────────────────────────

def _s3():
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    return boto3.client("s3", config=Config(signature_version=UNSIGNED))


def list_archive_volumes(site: str, day: datetime, s3=None) -> list[tuple[str, datetime]]:
    """[(key, scan_time)] for one UTC day, oldest first.

    Keys look like  2026/05/18/KILN/KILN20260518_231413_V06.
    `_MDM` and other non-volume objects are filtered out.
    """
    s3 = s3 or _s3()
    prefix = f"{day.year:04d}/{day.month:02d}/{day.day:02d}/{site.upper()}/"
    out = []
    token = None
    while True:
        kw = {"Bucket": ARCHIVE_BUCKET, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            name = key.rsplit("/", 1)[-1]
            if name.endswith("_MDM") or len(name) < 20:
                continue
            try:
                ts = datetime.strptime(name[4:19], "%Y%m%d_%H%M%S").replace(
                    tzinfo=timezone.utc)
            except ValueError:
                continue
            out.append((key, ts))
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    out.sort(key=lambda t: t[1])
    return out


# ── Replay ────────────────────────────────────────────────────────────────────

class Replayer:
    """Turns archived volumes into training rows via the live code paths."""

    def __init__(self, log_min_dbz: float = 40.0, trim_fields: bool = True):
        from backend.services.nexrad_service import NexradService
        # Borrowed, not reimplemented: _create_grid and dealias_radar_in_place
        # must be the same ones the live path uses or the features diverge.
        self.nexrad = NexradService()
        self.log_min_dbz = log_min_dbz
        self.trim_fields = trim_fields
        self.s3 = _s3()

    def _fresh_tracker(self):
        from backend.services.storm_tracking_service import StormTrackingService
        # A new instance per (site, day): cell tracking is stateful, and cells
        # must not survive a day boundary or a site change.
        return StormTrackingService()

    def _volume_from_file(self, path: str, site: str, scan_ts: datetime):
        import pyart
        from backend.services.nexrad_service import VolumeScanData
        from backend.services.nexrad_sites import NEXRAD_SITES

        # Read only the moments anything downstream touches.  A NEXRAD volume
        # also carries spectrum_width, differential_phase and
        # clutter_filter_power_removed; nothing here reads them, and each is
        # ~100 MB resident for a super-res sweep set.  Skipping them cuts both
        # parse time and the per-worker footprint -- and worker count is capped
        # by RAM, so this buys parallelism, which is the real lever.
        radar = pyart.io.read_nexrad_archive(
            path, linear_interp=False,
            include_fields=list(READ_FIELDS) if self.trim_fields else None)
        try:
            self.nexrad.dealias_radar_in_place(radar)
        except Exception as e:
            _log(f"    dealias failed ({e}) - continuing with folded velocity")

        info = NEXRAD_SITES.get(site.upper(), {})
        site_lat = info.get("lat", float(radar.latitude["data"][0]))
        site_lon = info.get("lon", float(radar.longitude["data"][0]))

        # Grid only the fields the tracker reads.  Barnes2 is ~31% of the
        # per-volume cost and was interpolating 9 fields to serve 4.  Verified
        # to produce byte-identical training rows -- see the parity check in
        # tests/scripts/test_backfill_parity.py.
        grid = self.nexrad._create_grid(
            radar, site_lat, site_lon,
            fields=self.nexrad.TRACKING_GRID_FIELDS if self.trim_fields else None)

        range_m = self.nexrad._max_range_km * 1000
        deg = range_m / 111_000.0
        bounds = {"north": site_lat + deg, "south": site_lat - deg,
                  "east": site_lon + deg, "west": site_lon - deg}

        elev = []
        try:
            starts = radar.sweep_start_ray_index["data"]
            for i in range(radar.nsweeps):
                elev.append(float(radar.elevation["data"][starts[i]]))
        except Exception:
            pass

        return VolumeScanData(
            site=site.upper(), timestamp=scan_ts.isoformat(),
            radar_object=radar, grid=grid, bounds=bounds, elevation_angles=elev,
        )

    def replay_day(self, site: str, day: datetime, out_fh,
                   window=None, max_volumes: int = 0) -> dict:
        from backend.services.live_qa_service import (
            build_training_record, should_log_cell,
        )

        vols = list_archive_volumes(site, day, self.s3)
        if window:
            # One (lo, hi) or a list of them: a volume is kept if it falls in ANY.
            wins = window if isinstance(window, list) else [window]
            vols = [(k, t) for k, t in vols if any(lo <= t <= hi for lo, hi in wins)]
        if max_volumes:
            vols = vols[:max_volumes]
        if not vols:
            return {"volumes": 0, "rows": 0, "errors": 0}

        _log(f"  {site} {day:%Y-%m-%d}: {len(vols)} volume(s)")
        tracker = self._fresh_tracker()
        rows = errors = 0
        t0 = time.time()

        # Download ahead of the decoder.  Gridding is Barnes2 over the whole
        # domain and is the irreducible cost — it must stay exactly as the live
        # path does it or the features drift — but there is no reason to sit
        # idle on S3 latency for each 10-30 MB volume first.  Order is preserved
        # because the queue is filled by a single thread walking `vols` in
        # sequence, and tracking is stateful so it must stay in time order.
        fetched: "queue.Queue" = queue.Queue(maxsize=PREFETCH)
        stop = threading.Event()

        def _fetch():
            for k, t in vols:
                if stop.is_set():
                    break
                tmp_path = None
                try:
                    fd, tmp_path = tempfile.mkstemp(suffix="_V06")
                    os.close(fd)
                    self.s3.download_file(ARCHIVE_BUCKET, k, tmp_path)
                    fetched.put((k, t, tmp_path, None))
                except Exception as exc:
                    if tmp_path and os.path.exists(tmp_path):
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                    fetched.put((k, t, None, exc))
            fetched.put(None)

        pump = threading.Thread(target=_fetch, daemon=True)
        pump.start()

        i = 0
        while True:
            item = fetched.get()
            if item is None:
                break
            key, ts, tmp, fetch_err = item
            i += 1
            try:
                if fetch_err is not None:
                    raise fetch_err
                volume = self._volume_from_file(tmp, site, ts)
                cells = tracker._process_sync(volume)
                if cells:
                    scan_ts = volume.timestamp
                    for cell in cells:
                        d = cell.to_dict()
                        d.setdefault("site", site.upper())
                        if not should_log_cell(d, self.log_min_dbz):
                            continue
                        out_fh.write(json.dumps(
                            build_training_record(d, scan_ts)) + "\n")
                        rows += 1
            except Exception as e:
                errors += 1
                if errors <= 3:
                    _log(f"    {key}: {type(e).__name__}: {e}")
            finally:
                if tmp and os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

            if i % 20 == 0 or i == len(vols):
                rate = i / max(1e-6, time.time() - t0)
                _log(f"    {i}/{len(vols)} volumes, {rows} rows, "
                     f"{errors} err, {rate:.2f} vol/s")
            out_fh.flush()

        stop.set()
        # Drain anything the pump already fetched so a Ctrl-C or an early exit
        # cannot leave 30 MB temp files behind.
        while True:
            try:
                left = fetched.get_nowait()
            except queue.Empty:
                break
            if left and left[2] and os.path.exists(left[2]):
                try:
                    os.unlink(left[2])
                except OSError:
                    pass
        pump.join(timeout=5)

        return {"volumes": len(vols), "rows": rows, "errors": errors}


# ── Parallel driver ───────────────────────────────────────────────────────────

# Peak RSS per worker, measured. A single volume holds the Py-ART radar (every
# sweep, every moment — a 2019 super-res VCP is ~11k rays x 1832 gates) plus the
# Barnes2 grid built from it. Five workers on a 32 GB box with ~10 GB free all
# died with MemoryError on ~111 MB allocations: it was not one big request, it
# was five processes having already eaten the headroom.
WORKER_GB = 3.0
RESERVE_GB = 4.0   # leave the dashboard, the tracker and the OS room to breathe


def free_ram_gb():
    """Available physical RAM, or None.

    stdlib only — `psutil` is not a dependency here and adding one would force a
    re-freeze of the bundled backend for a single number.
    """
    try:
        if sys.platform == "win32":
            import ctypes

            class _MEM(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = _MEM()
            st.dwLength = ctypes.sizeof(st)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return None
            return st.ullAvailPhys / (1024 ** 3)
        return (os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
                / (1024 ** 3))
    except Exception:
        return None


def _cap_workers(requested: int) -> int:
    """Clamp worker count to what the machine can actually carry.

    TWO limits, and CPU is usually the binding one — it was unguarded at first,
    which is why a request for more workers could be granted and then simply not
    go faster.

    Measured on a 5900X (12 physical / 24 logical): 3 workers held 34% total
    load, 6 workers held 93%.  Per-worker throughput was FLAT across that range
    (0.065 -> 0.062 vol/s), so 6 was real 2x scaling; past saturation the extra
    workers would just divide the same cores and aggregate would stop rising.
    That works out to ~1.3 physical cores per worker, because NumPy/Py-ART
    already thread internally — hence logical // 4 rather than one per core.
    """
    if requested <= 1:
        return 1

    logical = os.cpu_count() or 4
    cpu_allowed = max(1, logical // 4)
    if cpu_allowed < requested:
        _log(f"Capping workers {requested} -> {cpu_allowed} "
             f"({logical} logical CPUs; each worker uses ~1.3 cores because "
             "NumPy threads internally)")
        requested = cpu_allowed

    free_gb = free_ram_gb()
    if free_gb is None:
        return requested
    allowed = max(1, int((free_gb - RESERVE_GB) / WORKER_GB))
    if allowed < requested:
        _log(f"Capping workers {requested} -> {allowed} "
             f"({free_gb:.1f} GB free, ~{WORKER_GB:.0f} GB each, "
             f"{RESERVE_GB:.0f} GB reserved)")
        return allowed
    return requested

def _replay_pair(job: dict) -> dict:
    """Replay ONE (site, day) into its own shard file.  Runs in a subprocess.

    Each (site, day) is independent — the tracker is rebuilt per pair — so these
    parallelise cleanly, and they have to: a full season is ~15k volumes at
    ~20 s each, which is 80+ hours in one process.  Gridding is single-threaded
    Barnes2, so throughput scales with workers until RAM or S3 gives out.

    Every worker writes its OWN shard.  Appending to one file from several
    processes is not safe on Windows, and interleaved partial lines would
    corrupt the archive.
    """
    _lower_priority()
    _open_job_log(truncate=False)
    site, day_s = job["site"], job["day"]
    shard = Path(job["shard"])
    window = None
    if job.get("windows"):
        window = [(datetime.fromisoformat(a), datetime.fromisoformat(b))
                  for a, b in job["windows"]]
    d0 = datetime.strptime(day_s, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    replayer = Replayer(log_min_dbz=job["log_min_dbz"])
    res = {"volumes": 0, "rows": 0, "errors": 0}
    with shard.open("w", encoding="utf-8") as fh:
        # Both UTC dates: a convective day starts at 12Z and runs past midnight.
        for utc_day in (d0, d0 + timedelta(days=1)):
            r = replayer.replay_day(site, utc_day, fh, window=window,
                                    max_volumes=job["max_volumes"])
            for k in res:
                res[k] += r[k]
    return {"site": site, "day": day_s, "shard": str(shard), **res}


# ── State ─────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        return json.loads(STATE_OUT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"done": []}


def _save_state(state: dict) -> None:
    try:
        STATE_OUT.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Rebuild training data by replaying archived severe days")
    ap.add_argument("--sites", nargs="+", required=True,
                    help="Radar sites, e.g. KILN KIND KCLE")
    ap.add_argument("--start", help="YYYY-MM-DD (with --end)")
    ap.add_argument("--end", help="YYYY-MM-DD")
    ap.add_argument("--days", nargs="+",
                    help="Explicit convective days YYYY-MM-DD (skips discovery)")
    ap.add_argument("--min-tor", type=int, default=1,
                    help="Minimum tornado warnings near the site to replay a day")
    ap.add_argument("--list-days", action="store_true",
                    help="Show the candidate days and exit")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--full-day", action="store_true",
                    help="Replay the whole UTC day instead of the warned window")
    ap.add_argument("--max-volumes", type=int, default=0,
                    help="Cap volumes per (site, day); 0 = no cap")
    ap.add_argument("--log-min-dbz", type=float, default=40.0)
    ap.add_argument("--resume", action="store_true",
                    help="Skip (site, day) pairs already recorded as done")
    ap.add_argument("--workers", type=int, default=1,
                    help="Replay this many (site, day) pairs in parallel. Each is "
                         "independent (fresh tracker), and a full season is 80+ "
                         "hours single-threaded. Budget ~1.5 GB RAM per worker; "
                         "4-6 is sensible on a 12-core / 32 GB box.")
    args = ap.parse_args()

    sites = [s.upper() for s in args.sites]

    if args.days:
        # Named days still need their warned window looked up, or every pair
        # replays a whole UTC day -- ~300 volumes instead of ~60, for the same
        # handful of positives.  min_tor applies PER SITE here: a site with no
        # tornado warnings on a named day contributes only negatives, and the
        # 2024 season run spent 22% of its volumes on 43 such pairs.
        targets = {}
        for d in args.days:
            day = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            _log(f"Looking up the warned window for {d}")
            found = find_severe_days(day, day + timedelta(days=2), sites,
                                     min_tor=args.min_tor, chunk_days=3)
            for s in sites:
                info = found.get((d, s))
                if info:
                    targets[(d, s)] = info
                else:
                    _log(f"  no warnings near {s} on {d} - skipping "
                         "(nothing to label there)")
        if not targets:
            _log("None of the requested (day, site) pairs had warnings.")
            return
    else:
        if not (args.start and args.end):
            ap.error("give either --days or both --start and --end")
        start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        _log(f"Finding severe days {args.start} -> {args.end} near {', '.join(sites)}")
        targets = find_severe_days(start, end, sites, min_tor=args.min_tor)

    if not targets:
        _log("No candidate days found.")
        return

    ordered = sorted(targets.items(), key=lambda kv: (kv[0][0], kv[0][1]))
    if not args.list_days:
        _lower_priority()
        # Open the dashboard feed HERE, before the pair list prints, so the
        # per-pair TOR/SVR/window lines land in it rather than only on stdout.
        _open_job_log(truncate=True)
    _log(f"{len(ordered)} (day, site) pair(s):")
    for (day, site), info in ordered:
        span = ""
        if info.get("first") and info.get("last"):
            span = f"  {info['first']:%H:%MZ}-{info['last']:%H:%MZ}"
        _log(f"   {day}  {site}  TOR={info['tor']} SVR={info['svr']}{span}")
    if args.list_days:
        return

    state = _load_state() if args.resume else {"done": []}
    done = set(tuple(x) for x in state.get("done", []))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    totals = {"volumes": 0, "rows": 0, "errors": 0}

    def _window_for(info):
        """Padded replay windows for one (site, day), or None for the whole day."""
        if args.full_day:
            return None
        wins = info.get("windows") or (
            [(info["first"], info["last"])] if info.get("first") and info.get("last") else [])
        if not wins:
            return None
        return [(a - timedelta(hours=PAD_BEFORE_H), b + timedelta(hours=PAD_AFTER_H))
                for a, b in wins]

    args.workers = _cap_workers(args.workers)

    # Publish this run so the dashboard can report on it, however it was
    # started. Written AFTER the worker cap so the count shown is the count
    # actually used, not the count requested.
    try:
        JOB_META.write_text(json.dumps({
            "started_at": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "days": sorted({d for d, _ in targets}),
            "sites": sorted({s for _, s in targets}),
            "workers": args.workers,
            "full_day": bool(args.full_day),
            "log_path": str(JOB_LOG),
        }, indent=2), encoding="utf-8")
    except OSError:
        pass
    _log(f"Job published for the dashboard ({len(ordered)} pair(s), "
         f"{args.workers} worker(s))")

    if args.workers > 1:
        import concurrent.futures as cf

        pending = [(d, s, i) for (d, s), i in ordered if (d, s) not in done]
        if not pending:
            _log("Nothing left to do.")
            return
        shard_dir = out_path.parent / "backfill_shards"
        shard_dir.mkdir(parents=True, exist_ok=True)
        jobs = []
        for day, site, info in pending:
            win = _window_for(info)
            jobs.append({
                "site": site, "day": day,
                "shard": str(shard_dir / f"{day}_{site}.jsonl"),
                "windows": [[a.isoformat(), b.isoformat()] for a, b in win] if win else None,
                "max_volumes": args.max_volumes,
                "log_min_dbz": args.log_min_dbz,
            })
        # Longest-processing-time-first.  With jobs submitted in date order the
        # biggest pairs land last and run alone while the other workers idle:
        # the 16-pair run finished KILN 2026-08-11 (291 volumes) and KIND
        # 2026-08-11 (259) dead last, and the measured aggregate over the whole
        # job was 0.18 vol/s against a steady-state 0.38 -- the tail cost about
        # half the throughput.  Warned-window span is a good proxy for volume
        # count and costs nothing, so start the big ones first.
        def _span_h(j):
            if not j.get("windows"):
                return 24.0        # full-day replays are the biggest of all
            return sum((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
                       for a, b in j["windows"]) / 3600.0

        jobs.sort(key=_span_h, reverse=True)
        _log(f"Replaying {len(jobs)} pair(s) on {args.workers} worker(s), "
             f"longest first ({_span_h(jobs[0]):.1f}h down to {_span_h(jobs[-1]):.1f}h)")
        with out_path.open("a", encoding="utf-8") as fh, \
                cf.ProcessPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(_replay_pair, j): j for j in jobs}
            for n, fut in enumerate(cf.as_completed(futs), 1):
                j = futs[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    _log(f"  {j['site']} {j['day']}: FAILED {type(e).__name__}: {e}")
                    continue
                # Fold the shard in only once the worker finished cleanly, so a
                # crashed pair leaves no half-written rows in the archive.
                sh = Path(res["shard"])
                if sh.exists():
                    with sh.open(encoding="utf-8") as src:
                        for line in src:
                            fh.write(line)
                    fh.flush()
                    sh.unlink(missing_ok=True)
                for k in totals:
                    totals[k] += res[k]
                done.add((res["day"], res["site"]))
                state["done"] = [list(x) for x in done]
                _save_state(state)
                _log(f"  [{n}/{len(jobs)}] {res['site']} {res['day']}: "
                     f"{res['rows']} rows, {res['volumes']} volumes, "
                     f"{res['errors']} errors")
        try:
            shard_dir.rmdir()
        except OSError:
            pass
        _log(f"DONE: {totals['rows']} rows from {totals['volumes']} volumes "
             f"({totals['errors']} errors) -> {out_path}")
        _log("Next:  python scripts/label_from_warnings.py "
             f"--data {out_path} --all --overwrite --strict-tornado")
        return

    replayer = Replayer(log_min_dbz=args.log_min_dbz)
    with out_path.open("a", encoding="utf-8") as fh:
        for (day, site), info in ordered:
            if (day, site) in done:
                _log(f"  {site} {day}: already done, skipping")
                continue
            d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            window = _window_for(info)

            # A convective day starts at 12Z and runs into the next UTC date, so
            # both UTC prefixes have to be listed or an evening event loses
            # everything after midnight.
            res = {"volumes": 0, "rows": 0, "errors": 0}
            for utc_day in (d0, d0 + timedelta(days=1)):
                r = replayer.replay_day(site, utc_day, fh,
                                        window=window, max_volumes=args.max_volumes)
                for k in res:
                    res[k] += r[k]

            for k in totals:
                totals[k] += res[k]
            _log(f"  {site} {day}: {res['rows']} rows from {res['volumes']} volumes "
                 f"({res['errors']} errors)")
            done.add((day, site))
            state["done"] = [list(x) for x in done]
            _save_state(state)

    _log(f"DONE: {totals['rows']} rows from {totals['volumes']} volumes "
         f"({totals['errors']} errors) -> {out_path}")
    _log("Next:  python scripts/label_from_warnings.py "
         f"--data {out_path} --all --overwrite --strict-tornado")


if __name__ == "__main__":
    main()
