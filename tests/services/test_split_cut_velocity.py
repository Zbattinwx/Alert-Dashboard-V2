"""The NEXRAD split cut, and the two detectors it silently killed.

NEXRAD scans its lowest tilts TWICE: a long-PRT SURVEILLANCE cut carrying
reflectivity only, then a short-PRT DOPPLER cut carrying velocity. Both report
the same fixed_angle. Measured on KILN20240402_113023_V06:

    sweep 0  0.48 deg  488,964 REF gates        0 VEL gates
    sweep 1  0.48 deg  431,932 REF gates  414,628 VEL gates

The downburst and straight-line-wind detectors both chose their sweep with
`min(candidates, key=elevation)`, which on a tie returns the FIRST -- sweep 0,
the surveillance cut. They read an entirely masked velocity array, found
nothing, and returned. No exception. No failure recorded. The fields left at
the values the detector had just reset them to.

Across 206,896 re-derived training rows that produced:

    downburst_detected      0 nonzero of 206,896
    downburst_delta_v_ms    null in all 206,896
    max_wind_velocity_ms    null in all 206,896
    strong_wind_swath_km2   null in all 206,896

i.e. a damaging-wind model trained with the wind withheld -- the exact defect
the re-derivation was run to repair. MARC (38,066 detections) and the
rear-inflow jet were unaffected because they work above the split cuts, which
is precisely why only these two were dead and nobody noticed.
"""
import numpy as np
import pytest

from backend.services.storm_tracking_service import StormTrackingService


class FakeRadar:
    """The minimum a sweep-picker touches, with a real split-cut layout."""

    def __init__(self, sweeps, rays=10, gates=5):
        """`sweeps` is [(elevation, has_velocity), ...] in file order."""
        self.fixed_angle = {"data": [e for e, _ in sweeps]}
        starts, ends = [], []
        rows = rays * len(sweeps)
        data = np.ma.masked_all((rows, gates))
        for i, (_, has_vel) in enumerate(sweeps):
            s, e = i * rays, (i + 1) * rays - 1
            starts.append(s)
            ends.append(e)
            if has_vel:
                data[s:e + 1, :] = 12.0
        self.sweep_start_ray_index = {"data": starts}
        self.sweep_end_ray_index = {"data": ends}
        self.fields = {"velocity": {"data": data}}


def pick(radar, max_elev=1.5):
    return StormTrackingService._lowest_velocity_sweep(radar, "velocity", max_elev, "test")


def test_the_split_cut_pair_resolves_to_the_doppler_half():
    """The bug, exactly: two sweeps at the same angle, only the second useful."""
    radar = FakeRadar([(0.48, False), (0.48, True),
                       (0.88, False), (0.88, True),
                       (1.80, True)])
    got = pick(radar)
    assert got is not None
    idx, elev = got
    assert idx == 1, f"took sweep {idx} - the surveillance half has no velocity"
    assert elev == pytest.approx(0.48)


def test_the_old_selection_would_have_failed_this():
    """Guards the guard: if min-by-elevation ever comes back, this fails."""
    radar = FakeRadar([(0.48, False), (0.48, True)])
    naive = min([(i, float(a)) for i, a in enumerate(radar.fixed_angle["data"])],
                key=lambda x: x[1])
    assert naive[0] == 0, "test setup no longer reproduces the tie"
    s = radar.sweep_start_ray_index["data"][0]
    e = radar.sweep_end_ray_index["data"][0]
    assert not np.any(~np.ma.getmaskarray(radar.fields["velocity"]["data"][s:e + 1]))
    assert pick(radar)[0] == 1


def test_it_still_prefers_the_lowest_usable_tilt():
    """Correctness, not just non-emptiness: a 0.48 Doppler cut beats a 1.3 one."""
    radar = FakeRadar([(1.32, True), (0.48, False), (0.48, True), (0.88, True)])
    idx, elev = pick(radar)
    assert elev == pytest.approx(0.48) and idx == 2


def test_sweeps_above_the_ceiling_are_ignored():
    radar = FakeRadar([(0.48, False), (2.40, True), (3.10, True)])
    assert pick(radar, max_elev=1.5) is None


def test_no_velocity_anywhere_is_reported_not_silent():
    """A detector that CANNOT run must say so. Silence is what let this live:
    it looked identical to a detector that ran and found nothing."""
    from backend.services import failure_log

    def count() -> int:
        for d in (failure_log.snapshot() or {}).get("detectors", []):
            if d.get("key") == "test.no_velocity_sweep":
                return int(d.get("count", 0))
        return 0

    radar = FakeRadar([(0.48, False), (0.88, False)])
    before = count()
    assert pick(radar) is None
    assert count() > before, "returning None was not recorded as a failure"
    # And it must say what stopped working, not what threw.
    what = next(d["what"] for d in failure_log.snapshot()["detectors"]
                if d["key"] == "test.no_velocity_sweep")
    assert "producing nothing" in what and "velocity" in what, what


def test_a_malformed_radar_does_not_raise():
    """It runs inside a per-volume loop; a bad volume must cost one volume."""
    class Broken:
        fixed_angle = {"data": [0.5]}
        fields = {}
    assert pick(Broken()) is None
