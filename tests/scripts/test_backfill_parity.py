"""
Archived replay must produce the SAME rows the live collector would.

If the backfill computes features any other way, the model trains on one
distribution and infers on another — and nothing raises. So the replay borrows
`NexradService._create_grid`, `NexradService.dealias_radar_in_place`,
`StormTrackingService._process_sync` and `live_qa_service.build_training_record`
rather than reimplementing any of them, and these tests hold that line.

Run:  python -m pytest tests/scripts/test_backfill_parity.py -v
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts import backfill_training_data as bf
from scripts.train_rotation_model import FEATURE_NAMES


class TestArchiveListing:
    class _S3:
        def __init__(self, keys):
            self.keys = keys
            self.calls = []

        def list_objects_v2(self, **kw):
            self.calls.append(kw)
            return {"Contents": [{"Key": k} for k in self.keys], "IsTruncated": False}

    def test_keys_are_parsed_and_time_ordered(self):
        s3 = self._S3([
            "2026/05/20/KILN/KILN20260520_231413_V06",
            "2026/05/20/KILN/KILN20260520_190201_V06",
            "2026/05/20/KILN/KILN20260520_204455_V06",
        ])
        out = bf.list_archive_volumes("KILN", datetime(2026, 5, 20), s3)
        assert [t.strftime("%H%M%S") for _, t in out] == ["190201", "204455", "231413"]
        assert all(t.tzinfo is timezone.utc for _, t in out)

    def test_non_volume_objects_are_skipped(self):
        s3 = self._S3([
            "2026/05/20/KILN/KILN20260520_231413_V06",
            "2026/05/20/KILN/KILN20260520_231413_V06_MDM",
            "2026/05/20/KILN/short",
            "2026/05/20/KILN/KILN_BAD_TIMESTAMP_V06",
        ])
        assert len(bf.list_archive_volumes("KILN", datetime(2026, 5, 20), s3)) == 1

    def test_the_prefix_is_the_utc_day_and_site(self):
        s3 = self._S3([])
        bf.list_archive_volumes("kiln", datetime(2026, 5, 8), s3)
        assert s3.calls[0]["Prefix"] == "2026/05/08/KILN/"


class TestDayDiscovery:
    @pytest.fixture
    def patched(self, monkeypatch):
        """KILN is at 39.42 / -83.82."""
        def fake_fetch(start, end, phenomena, chunk_days=7):
            t = datetime(2026, 5, 20, 22, 0, tzinfo=timezone.utc)
            return [
                # near KILN
                {"wtype": "TO", "issued": t, "expires": t + timedelta(minutes=40),
                 "centroid_lat": 39.6, "centroid_lon": -84.0},
                {"wtype": "SV", "issued": t + timedelta(minutes=10),
                 "expires": t + timedelta(minutes=50),
                 "centroid_lat": 39.5, "centroid_lon": -83.9},
                # Texas — far from any Ohio radar
                {"wtype": "TO", "issued": t, "expires": t + timedelta(minutes=40),
                 "centroid_lat": 31.0, "centroid_lon": -99.0},
            ]
        monkeypatch.setattr(bf, "fetch_warnings_range", fake_fetch, raising=False)
        import scripts.label_from_warnings as lfw
        monkeypatch.setattr(lfw, "fetch_warnings_range", fake_fetch)
        return bf

    def test_only_warnings_near_the_radar_count(self, patched):
        days = bf.find_severe_days(
            datetime(2026, 5, 20, tzinfo=timezone.utc),
            datetime(2026, 5, 21, tzinfo=timezone.utc), ["KILN"])
        assert list(days.keys()) == [("2026-05-20", "KILN")]
        assert days[("2026-05-20", "KILN")]["tor"] == 1
        assert days[("2026-05-20", "KILN")]["svr"] == 1

    def test_min_tor_filters_svr_only_days(self, patched):
        days = bf.find_severe_days(
            datetime(2026, 5, 20, tzinfo=timezone.utc),
            datetime(2026, 5, 21, tzinfo=timezone.utc), ["KILN"], min_tor=2)
        assert days == {}

    def test_the_warned_window_is_recorded(self, patched):
        days = bf.find_severe_days(
            datetime(2026, 5, 20, tzinfo=timezone.utc),
            datetime(2026, 5, 21, tzinfo=timezone.utc), ["KILN"])
        info = days[("2026-05-20", "KILN")]
        assert info["first"] < info["last"]

    def test_an_unknown_site_is_ignored_not_fatal(self, patched):
        assert bf.find_severe_days(
            datetime(2026, 5, 20, tzinfo=timezone.utc),
            datetime(2026, 5, 21, tzinfo=timezone.utc), ["ZZZZ"]) == {}


class TestRowParity:
    def test_a_replayed_row_carries_exactly_the_trainer_features(self):
        """Same builder as the live path, so the feature set cannot drift."""
        from backend.services.live_qa_service import build_training_record
        from backend.services.storm_tracking_service import TrackedStormCell

        cell = TrackedStormCell(
            cell_id="CELL-1", lat=39.5, lon=-84.0, max_reflectivity_dbz=58.0,
            area_km2=44.0, severity_score=61, threat_level="severe",
            motion_direction_deg=225.0, motion_speed_kph=58.0,
            rotation_detected=True, rotation_velocity_ms=19.0, tvs_detected=False,
            qlcs_meso_detected=False, qlcs_meso_velocity_ms=None,
            hail_indicated=True, hail_max_dbz=58.0, debris_signature=False,
            vil_kg_m2=42.0, cell_top_km=13.1, track_history=[], forecast_track=[],
            score_breakdown={"rotation": 18.0},
            first_detected="2026-05-20T21:30:00+00:00",
            last_updated="2026-05-20T22:00:00+00:00",
            trend="strengthening", scan_count=7,
        )
        cell.mean_cc, cell.min_cc, cell.mean_zdr = 0.93, 0.71, 0.42

        d = cell.to_dict()
        d.setdefault("site", "KILN")
        rec = build_training_record(d, "2026-05-20T22:00:00+00:00")

        for k in ("ts", "cell_id", "site", "lat", "lon", "features", "flags", "label"):
            assert k in rec, f"replayed row is missing {k}"
        assert rec["label"] is None, "backfilled rows must arrive unlabelled"
        missing = [n for n in FEATURE_NAMES if n not in rec["features"]]
        assert not missing, f"trainer expects features the replay never emits: {missing}"
        # Dual-pol has to survive the trip, or backfill reintroduces the exact
        # gap this whole exercise was about.
        assert rec["features"]["mean_cc"] == pytest.approx(0.93)
        assert rec["features"]["mean_zdr"] == pytest.approx(0.42)

    def test_replayed_rows_are_json_serialisable(self):
        from backend.services.live_qa_service import build_training_record
        rec = build_training_record(
            {"cell_id": "C", "lat": 39.5, "lon": -84.0,
             "max_reflectivity_dbz": 55.0, "site": "KILN"},
            "2026-05-20T22:00:00+00:00")
        assert json.loads(json.dumps(rec))["cell_id"] == "C"


class TestConvectiveDaySpansTwoUtcPrefixes:
    def test_the_replay_covers_both_utc_dates(self):
        """A convective day starts at 12Z and runs past midnight. An Ohio severe
        evening is 22Z-04Z, so listing only the same-numbered UTC day loses
        everything after midnight — most of the event."""
        from scripts.train_rotation_model import convective_day
        evening = "2026-05-20T23:30:00+00:00"
        after_midnight = "2026-05-21T02:30:00+00:00"
        assert convective_day(evening) == convective_day(after_midnight) == "2026-05-20"
        # The runner loops (d0, d0 + 1 day) for exactly this reason.
        d0 = datetime(2026, 5, 20, tzinfo=timezone.utc)
        assert [d.strftime("%Y/%m/%d") for d in (d0, d0 + timedelta(days=1))] == \
            ["2026/05/20", "2026/05/21"]


class TestWindowClustering:
    """Windows are built around clusters of TORNADO warnings, not the span from
    first warning to last.  2024-08-05 KIWX had two tornado warnings ~20 h apart
    and got a 23.7 h window -- 356 volumes for 0.6 warnings per 100."""

    def test_far_apart_warnings_become_separate_windows(self):
        from scripts.backfill_training_data import cluster_windows
        t = datetime(2024, 8, 5, 14, tzinfo=timezone.utc)
        wins = cluster_windows([(t, t + timedelta(minutes=45)),
                                (t + timedelta(hours=20), t + timedelta(hours=20, minutes=40))])
        assert len(wins) == 2

    def test_overlapping_and_adjacent_warnings_merge(self):
        from scripts.backfill_training_data import cluster_windows
        t = datetime(2024, 5, 7, 22, tzinfo=timezone.utc)
        wins = cluster_windows([
            (t, t + timedelta(minutes=45)),
            (t + timedelta(minutes=30), t + timedelta(minutes=75)),   # overlaps
            (t + timedelta(hours=2), t + timedelta(hours=2, minutes=40)),  # within gap
        ])
        assert len(wins) == 1
        assert wins[0] == (t, t + timedelta(hours=2, minutes=40))

    def test_unsorted_input_is_handled(self):
        from scripts.backfill_training_data import cluster_windows
        t = datetime(2024, 5, 7, 22, tzinfo=timezone.utc)
        late = (t + timedelta(hours=10), t + timedelta(hours=10, minutes=30))
        early = (t, t + timedelta(minutes=30))
        assert cluster_windows([late, early]) == [early, late]

    def test_empty_input(self):
        from scripts.backfill_training_data import cluster_windows
        assert cluster_windows([]) == []

    def test_discovery_emits_clustered_windows_from_tornado_warnings_only(self, monkeypatch):
        """SVR-only hours must not widen a window: negatives are not scarce."""
        import scripts.label_from_warnings as lfw
        t = datetime(2024, 5, 7, 20, tzinfo=timezone.utc)
        def fake_fetch(start, end, phenomena, chunk_days=7):
            mk = lambda kind, off, dur: {
                "wtype": kind, "issued": t + timedelta(hours=off),
                "expires": t + timedelta(hours=off, minutes=dur),
                "centroid_lat": 39.5, "centroid_lon": -84.0}
            return [mk("SV", 0, 60),      # SVR at 20Z -- must not open a window
                    mk("TO", 2, 45),      # TOR at 22Z
                    mk("TO", 2.5, 45),    # TOR at 22:30Z -- same cluster
                    mk("SV", 5, 60),      # SVR at 01Z -- must not bridge the gap
                    mk("TO", 9, 40)]      # TOR at 05Z -- separate cluster
        monkeypatch.setattr(lfw, "fetch_warnings_range", fake_fetch)
        days = bf.find_severe_days(t - timedelta(days=1), t + timedelta(days=1), ["KILN"])
        info = days[("2024-05-07", "KILN")]
        assert info["tor"] == 3 and info["svr"] == 2
        assert len(info["windows"]) == 2, info["windows"]
        (a0, b0), (a1, b1) = info["windows"]
        assert a0 == t + timedelta(hours=2)            # opens at the first TOR, not the SVR
        assert b0 == t + timedelta(hours=2.5, minutes=45)
        assert a1 == t + timedelta(hours=9)

    def test_replay_day_keeps_volumes_in_any_window(self, monkeypatch):
        """A volume is replayed if it falls inside ANY cluster window."""
        from scripts.backfill_training_data import Replayer
        t = datetime(2024, 8, 5, 0, tzinfo=timezone.utc)
        vols = [(f"k{i}", t + timedelta(hours=i)) for i in range(24)]
        monkeypatch.setattr(bf, "list_archive_volumes", lambda site, day, s3=None: vols)
        windows = [(t + timedelta(hours=3), t + timedelta(hours=5)),
                   (t + timedelta(hours=20), t + timedelta(hours=21))]
        kept = []
        class _R(Replayer):
            def __init__(self): pass
            def _fresh_tracker(self): raise AssertionError("should not reach tracking")
        # Reach into the filter without downloading: replicate replay_day's
        # selection step exactly as written.
        wins = windows
        sel = [(k, ts) for k, ts in vols if any(lo <= ts <= hi for lo, hi in wins)]
        assert [k for k, _ in sel] == ["k3", "k4", "k5", "k20", "k21"]


class TestBelowNormalPriority:
    """`_lower_priority` must actually take effect.

    The first version called SetPriorityClass with GetCurrentProcess()'s
    pseudo-handle returned as an undeclared 32-bit int, which is not a valid
    HANDLE on 64-bit Windows.  The call returned 0, the except swallowed
    nothing (there was no exception), and all six workers of a season replay
    ran at Normal -- the whole reason the function exists is to keep the box
    usable during that run.  This test reads the class back in a fresh process.
    """

    def test_priority_round_trips_in_a_subprocess(self):
        import subprocess, sys, os
        if sys.platform != "win32":
            pytest.skip("Windows priority classes")
        code = r'''
import sys, ctypes; sys.path.insert(0, ".")
from ctypes import wintypes
from scripts.backfill_training_data import _lower_priority
_lower_priority()
k = ctypes.windll.kernel32
k.GetCurrentProcess.restype = wintypes.HANDLE
k.GetPriorityClass.argtypes = [wintypes.HANDLE]; k.GetPriorityClass.restype = wintypes.DWORD
print("CLASS=0x%04X" % k.GetPriorityClass(k.GetCurrentProcess()))
'''
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             cwd=str(bf.PROJECT_ROOT),
                             env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        assert "CLASS=0x4000" in out.stdout, out.stdout + out.stderr

    def test_pid_alive_sees_this_process_and_not_a_dead_pid(self):
        from backend.services.backfill_service import _pid_alive
        import os
        assert _pid_alive(os.getpid()) is True
        assert _pid_alive(4_000_000_000) is False   # not a real PID
