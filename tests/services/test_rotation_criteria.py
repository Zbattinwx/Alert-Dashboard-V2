"""Rotation criteria vs the published literature.

The centrepiece is test_noise_does_not_trip_significant_shear_at_close_range:
it reproduces the actual near-radar false-alarm bug with pure numpy, shows the
fixed-ray kernel failing at 10 km, and shows the fixed-metre kernel passing at
every range.  If someone ever "simplifies" llsd_kernel_rays back to a constant,
that test is what stops it.
"""
import math

import numpy as np
import pytest

from backend.services import rotation_criteria as rc


# ── MDA ranks and range relaxation ─────────────────────────────────────────

def test_mda_rank5_matches_published_range_bands():
    """Stumpf 1998: 15 m/s at <=100 km, 13.1 at 150, 11.3 at >=200."""
    assert rc.mda_vrot_threshold(5, 50) == pytest.approx(15.0, abs=0.05)
    assert rc.mda_vrot_threshold(5, 100) == pytest.approx(15.0, abs=0.05)
    assert rc.mda_vrot_threshold(5, 150) == pytest.approx(13.1, abs=0.05)
    assert rc.mda_vrot_threshold(5, 200) == pytest.approx(11.3, abs=0.05)
    assert rc.mda_vrot_threshold(5, 250) == pytest.approx(11.3, abs=0.05)


def test_mda_thresholds_relax_monotonically_with_range():
    prev = None
    for r in range(0, 260, 10):
        t = rc.mda_vrot_threshold(5, r)
        if prev is not None:
            assert t <= prev + 1e-9, f"threshold rose at {r} km"
        prev = t


def test_rank_anchors():
    """Ranks 3/5/7/9 sit on their published Vrot values at close range."""
    for rank, vrot in rc.MDA_RANK_VROT_MS.items():
        assert rc.meso_rank(vrot, 50) == rank


def test_rank_is_monotonic_in_vrot():
    ranks = [rc.meso_rank(v, 80) for v in range(0, 40)]
    assert ranks == sorted(ranks)


def test_flat_13_threshold_was_wrong_in_both_directions():
    """The old flat MESO_VELOCITY_THRESHOLD_MS = 13 was too permissive close in
    and too strict far out -- exactly backwards from the published relaxation."""
    assert rc.mda_vrot_threshold(5, 30) > 13.0    # should have needed 15
    assert rc.mda_vrot_threshold(5, 220) < 13.0   # should have needed 11.3


# ── Mesocyclone gates ──────────────────────────────────────────────────────

def test_minimal_mesocyclone_passes_every_gate():
    ok, why = rc.is_mesocyclone(16.0, 60, depth_km=4.0, base_km=1.5,
                                volumes=2, aspect_ratio=1.2)
    assert ok, why


def test_single_volume_is_a_couplet_not_a_mesocyclone():
    ok, why = rc.is_mesocyclone(20.0, 60, depth_km=4.0, base_km=1.0, volumes=1)
    assert not ok
    assert "volume" in why


def test_shallow_rotation_rejected():
    ok, why = rc.is_mesocyclone(20.0, 60, depth_km=1.0, base_km=1.0, volumes=2)
    assert not ok
    assert "depth" in why


def test_linear_signature_rejected_by_aspect_ratio():
    """A gust front along the beam is a shear line, not a vortex."""
    ok, why = rc.is_mesocyclone(20.0, 60, depth_km=4.0, base_km=1.0,
                                volumes=2, aspect_ratio=6.0)
    assert not ok
    assert "aspect" in why


def test_near_radar_mask_applies():
    ok, why = rc.is_mesocyclone(30.0, 6.0, depth_km=5.0, base_km=0.3, volumes=3)
    assert not ok
    assert "near-radar" in why


# ── TVS ────────────────────────────────────────────────────────────────────

def test_tvs_requires_both_base_and_aloft():
    assert rc.is_tvs(30.0, 40.0, depth_km=2.0, tilts=3)[0]
    # Strong at the base, nothing above it -- a low-level couplet, not a TVS.
    ok, why = rc.is_tvs(30.0, 20.0, depth_km=2.0, tilts=3)
    assert not ok and "aloft" in why
    ok, why = rc.is_tvs(20.0, 40.0, depth_km=2.0, tilts=3)
    assert not ok and "base" in why


def test_tvs_cannot_be_claimed_without_polar_data():
    """A 1 km Cartesian grid cannot produce gate-to-gate dV, so it must not be
    able to assert a TVS at all."""
    ok, why = rc.is_tvs(None, None)
    assert not ok
    assert "polar" in why


def test_tvs_needs_vertical_continuity():
    assert not rc.is_tvs(30.0, 40.0, depth_km=0.5, tilts=3)[0]
    assert not rc.is_tvs(30.0, 40.0, depth_km=2.0, tilts=1)[0]


# ── The LLSD kernel: the actual bug ────────────────────────────────────────

def test_kernel_ray_count_shrinks_with_range():
    """A fixed 2500 m kernel spans many rays close in and few far out."""
    assert rc.llsd_kernel_rays(10, 0.5) >= 12
    assert rc.llsd_kernel_rays(50, 0.5) == 3
    assert rc.llsd_kernel_rays(150, 0.5) == 1
    assert rc.llsd_kernel_rays(230, 0.5) == 1


def test_kernel_is_bounded_at_zero_range():
    """Gate 0 sits at range zero, where a fixed-metre kernel wants infinitely
    many rays.  Unbounded, that exceeded the sweep's ray count and the detector
    bailed before looking at a single cell -- it silently produced nothing."""
    for r in (0.0, 0.05, 0.5, 1.0):
        n = rc.llsd_kernel_rays(r, 0.5)
        assert 1 <= n <= rc.LLSD_MAX_KERNEL_RAYS, f"{r} km -> {n} rays"


def test_kernel_cap_only_binds_inside_the_near_radar_mask():
    """The cap must never distort a range whose answer we actually use."""
    uncapped_from = rc.LLSD_MIN_RANGE_KM
    n = rc.llsd_kernel_rays(uncapped_from, 0.5)
    assert n < rc.LLSD_MAX_KERNEL_RAYS, (
        "the cap is binding at the mask edge, so it is changing real answers")


def test_kernel_physical_width_is_range_invariant():
    """Whatever the range, the kernel spans ~2500 m -- within one ray."""
    for r in (10, 25, 50, 100, 150, 200):
        n = rc.llsd_kernel_rays(r, 0.5)
        per_ray = r * 1000.0 * math.radians(0.5)
        width = 2 * n * per_ray
        assert abs(width - rc.LLSD_KERNEL_AZIMUTHAL_M) <= per_ray * 1.05, (
            f"kernel at {r} km spans {width:.0f} m, not ~2500 m")


def _rankine_sweep(range_km, vrot_ms, core_radius_m=1500.0,
                   ray_spacing_deg=0.5, n_rays=720, noise_ms=0.0, seed=0):
    """One ray of radial velocity through a Rankine vortex centred at range_km.

    Returns velocities along the azimuth at the vortex's range gate: solid-body
    inside the core, 1/r outside.  This is the standard idealised couplet.
    """
    rng = np.random.default_rng(seed)
    az = (np.arange(n_rays) - n_rays / 2) * math.radians(ray_spacing_deg)
    cross_m = az * range_km * 1000.0          # cross-beam distance from centre
    r = np.abs(cross_m)
    v = np.where(r <= core_radius_m,
                 vrot_ms * (r / core_radius_m),
                 vrot_ms * (core_radius_m / np.maximum(r, 1e-6)))
    v = np.sign(cross_m) * v
    if noise_ms:
        v = v + rng.normal(0.0, noise_ms, size=v.shape)
    return v


def _peak_shear(v, half_rays, range_km, ray_spacing_deg=0.5):
    """Max |dV| / span over a kernel, the way the detector measures it."""
    per_ray_m = range_km * 1000.0 * math.radians(ray_spacing_deg)
    span_m = 2 * half_rays * per_ray_m
    dv = v[2 * half_rays:] - v[:-2 * half_rays]
    return float(np.max(np.abs(dv))) / span_m


def test_noise_does_not_trip_significant_shear_at_close_range():
    """The bug, and the fix, in one test.

    Pure 2 m/s velocity noise -- no vortex at all.  With the old fixed +/-2 ray
    kernel the measured shear at 10 km blows past the 0.010 s^-1 'significant'
    threshold, because 2 rays there span 87 m.  With the fixed 2500 m kernel it
    stays under the noise floor at every range.
    """
    NOISE = 2.0
    v10 = _rankine_sweep(10, vrot_ms=0.0, noise_ms=NOISE, seed=1)

    fixed_ray = _peak_shear(v10, half_rays=2, range_km=10)
    assert fixed_ray > rc.LLSD_SIGNIFICANT, (
        "expected the old fixed-ray kernel to be tripped by noise at 10 km")

    for r in (10, 25, 50, 100, 150, 200):
        v = _rankine_sweep(r, vrot_ms=0.0, noise_ms=NOISE, seed=1)
        physical = _peak_shear(v, rc.llsd_kernel_rays(r, 0.5), range_km=r)
        assert physical < rc.LLSD_NOISE_FLOOR, (
            f"physical kernel tripped by noise at {r} km: {physical:.4f} s^-1")


def test_real_vortex_is_detected_at_every_range_with_physical_kernel():
    """A 20 m/s Rankine couplet reads as significant shear from 10 to 200 km."""
    for r in (10, 25, 50, 100, 150):
        v = _rankine_sweep(r, vrot_ms=20.0, noise_ms=1.0, seed=2)
        s = _peak_shear(v, rc.llsd_kernel_rays(r, 0.5), range_km=r)
        assert s >= rc.LLSD_SIGNIFICANT, (
            f"missed a real 20 m/s couplet at {r} km ({s:.4f} s^-1)")


def test_fixed_ray_kernel_false_alarm_rate_is_range_dependent():
    """Quantifies the old behaviour: with a fixed ray kernel, pure noise trips
    the threshold near the radar and never trips it far out.  Detection skill
    that varies by an order of magnitude with range is not skill."""
    trips_close = trips_far = 0
    for seed in range(40):
        close = _peak_shear(_rankine_sweep(10, 0.0, noise_ms=2.0, seed=seed),
                            half_rays=2, range_km=10)
        far = _peak_shear(_rankine_sweep(150, 0.0, noise_ms=2.0, seed=seed),
                          half_rays=2, range_km=150)
        trips_close += close > rc.LLSD_SIGNIFICANT
        trips_far += far > rc.LLSD_SIGNIFICANT
    assert trips_close == 40, "noise should trip the old kernel every time at 10 km"
    assert trips_far == 0, "noise should never trip the old kernel at 150 km"


def test_significant_shear_implies_a_constant_dv_across_the_kernel():
    """25 m/s across 2500 m, at any range -- a real, physical number."""
    assert rc.llsd_noise_equivalent_dv(10) == pytest.approx(25.0)
    assert rc.llsd_noise_equivalent_dv(200) == pytest.approx(25.0)


# ── Aspect ratio ───────────────────────────────────────────────────────────

def test_aspect_ratio_edges():
    assert rc.aspect_ratio(4.0, 2.0) == pytest.approx(2.0)
    assert rc.aspect_ratio(2.0, 0.0) is None
    assert rc.aspect_ratio(2.0, None) is None
    assert rc.aspect_ratio(float("nan"), 2.0) is None
