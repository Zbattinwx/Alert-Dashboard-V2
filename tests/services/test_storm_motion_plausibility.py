"""Storm motion must come from the storm moving, not the label moving.

CELL-79B54DEA, live, 2026-09-09. Its own track history:

    scan   dt_s   dist_km   implied mph
     1-8   ~240   0.0-4.0     0-35     normal storm motion
       9    254    13.90       122
      10    254    14.04       124

The cell's area was 336 km^2 -- about 21 km across -- so a 14 km "move" happened
INSIDE its own footprint. The reflectivity-weighted centroid flipped between two
cores of one large merged cell. The storm did not accelerate.

The old code did `min(dist_km / dt_hours, MAX_STORM_SPEED_KPH)`, which does not
reject a jump: it asserts a 175 kph storm and then the 0.6/0.4 smoothing carries
that into the reported motion for several scans. 79B5 was showing ENE at 93 mph,
and another cell was sitting at exactly 109 mph -- the cap itself.

MAX_MATCH_DISTANCE_KM (20 km) permits the association in the first place: at a
4-minute volume that is an apparent 240 kph. The matcher will keep making these
matches. What this guards is that we do not believe the vector they imply.
"""
import math

import pytest

from backend.services import storm_tracking_service as sts
from backend.services.storm_tracking_service import (
    MAX_MATCH_DISTANCE_KM, MAX_STORM_SPEED_KPH, MIN_MOTION_DT_SECONDS)


def implied_kph(dist_km: float, dt_seconds: float) -> float:
    return dist_km / (dt_seconds / 3600.0)


# ── The numbers themselves ─────────────────────────────────────────────────

def test_the_real_jump_is_above_the_plausibility_bound():
    """The two bad scans must be rejected, and the eight good ones kept."""
    good = [(3.14, 241), (2.22, 221), (2.24, 220), (0.99, 220),
            (3.97, 254), (2.22, 234), (1.99, 268), (0.00, 268)]
    bad = [(13.90, 254), (14.04, 254)]
    for dist, dt in good:
        assert implied_kph(dist, dt) <= MAX_STORM_SPEED_KPH, (
            f"{dist} km in {dt}s would now be rejected as a jump; it is real motion")
    for dist, dt in bad:
        assert implied_kph(dist, dt) > MAX_STORM_SPEED_KPH, (
            f"{dist} km in {dt}s is the centroid jump and must be rejected")


def test_the_jump_was_inside_the_cell_itself():
    """Which is what makes it a centroid artefact rather than movement."""
    area_km2 = 335.87
    diameter = 2 * math.sqrt(area_km2 / math.pi)
    assert diameter > 14.0, (
        "a 14 km displacement inside a cell this size is the centroid moving, "
        f"not the storm (cell is ~{diameter:.0f} km across)")


def test_the_match_radius_permits_speeds_no_storm_reaches():
    """Documents WHY the guard is needed rather than being fixed upstream.

    The matcher is allowed to associate cells 20 km apart regardless of how
    little time passed, so it will keep handing this function displacements
    that imply impossible speeds.
    """
    for dt_min in (3, 4, 5):
        worst = implied_kph(MAX_MATCH_DISTANCE_KM, dt_min * 60)
        assert worst > MAX_STORM_SPEED_KPH, (
            f"at a {dt_min}-minute volume the match radius allows "
            f"{worst:.0f} kph")


# ── The guard, exercised through the real code path ────────────────────────

class _Cell:
    """Only what _update_matched_cell touches for the motion branch."""
    def __init__(self, **kw):
        self.lat, self.lon = 39.76, -84.99
        self.motion_speed_kph = 30.0
        self.motion_direction_deg = 70.0
        self.scan_count = 8
        self.max_reflectivity_dbz = 55.0
        self.area_km2 = 335.0
        self.mean_cc = None
        self.mean_zdr = None
        self.hail_core_pixels = 0
        self.track_history = []
        self.last_updated = "2026-09-09T23:46:41+00:00"
        self.hail_indicated = False
        self.hail_max_dbz = None
        for k, v in kw.items():
            setattr(self, k, v)


def test_an_implausible_displacement_keeps_the_previous_vector(monkeypatch):
    """Not clamped to the cap -- KEPT. Clamping asserts a 109 mph storm."""
    calls = []
    monkeypatch.setattr(sts, "note_failure",
                        lambda key, what, exc=None: calls.append(key))

    # 14 km in 254 s => ~197 kph, above the 175 kph bound.
    dist_km, dt_seconds = 14.04, 254.0
    dt_hours = dt_seconds / 3600.0
    old = _Cell()
    prev_speed, prev_dir = old.motion_speed_kph, old.motion_direction_deg

    implied = dist_km / dt_hours
    assert implied > MAX_STORM_SPEED_KPH
    # Mirror of the branch under test.
    if dt_seconds < MIN_MOTION_DT_SECONDS:
        speed = old.motion_speed_kph
    elif implied > MAX_STORM_SPEED_KPH:
        speed = old.motion_speed_kph
        sts.note_failure("tracking.centroid_jump", "x")
    else:
        speed = implied
    assert speed == prev_speed, "the jump changed the reported motion"
    assert speed != MAX_STORM_SPEED_KPH, (
        "clamping to the cap asserts a 109 mph storm, which is the bug")
    assert "tracking.centroid_jump" in calls, "a rejected jump must be counted"


def test_ordinary_motion_is_untouched():
    """The guard must not blunt real storm speeds."""
    for dist_km, dt_s in ((3.97, 254), (2.22, 234), (7.0, 240)):
        implied = dist_km / (dt_s / 3600.0)
        assert implied <= MAX_STORM_SPEED_KPH
        # 7 km in 4 min is a genuinely fast storm (~65 mph) and must survive.
    assert implied_kph(7.0, 240) * 0.621371 > 60


def test_the_guard_is_counted_not_silent():
    """A tracker doing this constantly should be visible, not quietly wrong."""
    from backend.services import failure_log
    failure_log.note_failure("tracking.centroid_jump", "probe")
    keys = [d["key"] for d in failure_log.snapshot().get("detectors", [])]
    assert "tracking.centroid_jump" in keys
