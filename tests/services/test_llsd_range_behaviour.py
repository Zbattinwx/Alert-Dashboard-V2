"""The rewritten LLSD detector, driven end to end on synthetic sweeps.

test_rotation_criteria covers the maths.  This covers the WIRING: that the
detector really uses a range-varying kernel, really masks the near-radar zone,
and really stops LLSD from manufacturing a mesocyclone out of noise -- which is
what was putting a rotation flag on nearly every cell close to the radar.
"""
import math
from types import SimpleNamespace

import numpy as np
import pytest

from backend.services import rotation_criteria as rc
from backend.services.storm_tracking_service import (
    StormTrackingService,
    TrackedStormCell,
)

RAD_LAT, RAD_LON = 39.42, -83.82   # KILN
N_RAYS = 720
RAY_SPACING = 0.5
GATE_M = 250.0
N_GATES = 900


def _cell(cell_id, lat, lon):
    return TrackedStormCell(
        cell_id=cell_id, lat=lat, lon=lon,
        max_reflectivity_dbz=55.0, area_km2=40.0, severity_score=50.0,
        threat_level="moderate", motion_direction_deg=240.0, motion_speed_kph=50.0,
        rotation_detected=False, rotation_velocity_ms=None, tvs_detected=False,
        qlcs_meso_detected=False, qlcs_meso_velocity_ms=None,
        hail_indicated=False, hail_max_dbz=None, debris_signature=False,
        vil_kg_m2=30.0, cell_top_km=12.0, track_history=[], forecast_track=[],
        score_breakdown={}, first_detected="2026-09-08T00:00:00Z",
        last_updated="2026-09-08T00:00:00Z", trend="steady", scan_count=3,
    )


def _point_at(range_km, bearing_deg=90.0):
    """Lat/lon `range_km` from the radar along `bearing_deg`."""
    d = range_km / 6371.0
    b = math.radians(bearing_deg)
    lat1, lon1 = math.radians(RAD_LAT), math.radians(RAD_LON)
    lat2 = math.asin(math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(b))
    lon2 = lon1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(lat1),
                             math.cos(d) - math.sin(lat1) * math.sin(lat2))
    return math.degrees(lat2), math.degrees(lon2)


def _radar(vel):
    """A minimal Py-ART-shaped radar carrying one 0.5 deg Doppler sweep."""
    return SimpleNamespace(
        fields={"velocity_dealiased": {"data": np.ma.masked_invalid(vel)}},
        fixed_angle={"data": np.array([0.5])},
        sweep_start_ray_index={"data": np.array([0])},
        sweep_end_ray_index={"data": np.array([N_RAYS - 1])},
        azimuth={"data": np.arange(N_RAYS) * RAY_SPACING},
        range={"data": np.arange(N_GATES) * GATE_M},
        latitude={"data": np.array([RAD_LAT])},
        longitude={"data": np.array([RAD_LON])},
        instrument_parameters={"nyquist_velocity": {"data": np.array([32.0])}},
    )


def _sweep(noise_ms=2.0, vortex=None, seed=0):
    """Velocity field: noise everywhere, optionally a Rankine vortex.

    `vortex` is (range_km, bearing_deg, vrot_ms, core_m).
    """
    rng = np.random.default_rng(seed)
    vel = rng.normal(0.0, noise_ms, size=(N_RAYS, N_GATES))
    if vortex is None:
        return vel
    r_km, bearing, vrot, core = vortex
    az = np.arange(N_RAYS) * RAY_SPACING
    rng_m = np.arange(N_GATES) * GATE_M
    # Cross-beam offset of each ray from the vortex centre, at its range.
    d_az = np.deg2rad((az - bearing + 540.0) % 360.0 - 180.0)
    cross = d_az[:, None] * (r_km * 1000.0)
    along = rng_m[None, :] - r_km * 1000.0
    rad = np.hypot(cross, np.zeros_like(cross) + along)
    tang = np.where(rad <= core, vrot * (rad / max(core, 1.0)),
                    vrot * (core / np.maximum(rad, 1.0)))
    # Radial component of a tangential wind: sign flips across the beam.
    vel = vel + np.sign(cross) * tang * np.exp(-(along / 3000.0) ** 2)
    return vel


@pytest.fixture
def svc():
    s = StormTrackingService()
    s._radar_locations = {"KILN": (RAD_LAT, RAD_LON)}
    return s


def test_noise_alone_no_longer_flags_rotation_near_the_radar(svc):
    """The reported bug: cells close to the radar carried rotation constantly.

    Pure 2 m/s noise, no vortex, cells at 12/15/20/30 km.  With the old fixed
    +/-2 ray kernel every one of these tripped the 0.010 /s threshold.
    """
    radar = _radar(_sweep(noise_ms=2.0, seed=7))
    cells = []
    for i, r in enumerate((12, 15, 20, 30)):
        lat, lon = _point_at(r)
        cells.append(_cell(f"noise{i}", lat, lon))
    svc._detect_llsd_rotation(radar, cells)
    flagged = [c.cell_id for c in cells if c.llsd_rotation_detected]
    assert not flagged, f"noise flagged as rotation at close range: {flagged}"


def test_inside_the_near_radar_mask_llsd_declines_to_guess(svc):
    lat, lon = _point_at(6.0)
    cell = _cell("tooclose", lat, lon)
    svc._detect_llsd_rotation(_radar(_sweep(noise_ms=2.0, seed=3)), [cell])
    assert not cell.llsd_rotation_detected
    assert cell.llsd_max_shear is None
    assert "near-radar mask" in (cell.llsd_diagnostic or "")


def test_a_real_couplet_is_still_found_at_every_range(svc):
    """The fix must not buy its quiet by going deaf."""
    for r in (25, 50, 100, 150):
        lat, lon = _point_at(r)
        cell = _cell(f"vortex{r}", lat, lon)
        radar = _radar(_sweep(noise_ms=1.0, vortex=(r, 90.0, 25.0, 1500.0), seed=11))
        svc._detect_llsd_rotation(radar, [cell])
        assert cell.llsd_rotation_detected, (
            f"missed a 25 m/s couplet at {r} km "
            f"(shear {cell.llsd_max_shear}, {cell.llsd_diagnostic})")


def test_measured_shear_is_comparable_across_range_for_the_same_vortex(svc):
    """The same storm should read about the same at 25 km and at 150 km.

    This is what a fixed-metre kernel buys, and the reason a single threshold
    can be valid everywhere.  Under the old fixed-ray kernel the 25 km reading
    was several times the 150 km one for an identical vortex.
    """
    got = {}
    for r in (25, 50, 100, 150):
        lat, lon = _point_at(r)
        cell = _cell(f"v{r}", lat, lon)
        radar = _radar(_sweep(noise_ms=0.5, vortex=(r, 90.0, 25.0, 1500.0), seed=5))
        svc._detect_llsd_rotation(radar, [cell])
        got[r] = cell.llsd_max_shear
    assert all(v is not None for v in got.values()), got
    spread = max(got.values()) / min(got.values())
    assert spread < 4.0, f"shear still strongly range-dependent: {got}"


def test_gate_to_gate_dv_is_recorded_for_the_tvs_test(svc):
    lat, lon = _point_at(60.0)
    cell = _cell("dv", lat, lon)
    radar = _radar(_sweep(noise_ms=1.0, vortex=(60.0, 90.0, 30.0, 800.0), seed=9))
    svc._detect_llsd_rotation(radar, [cell])
    assert cell.gate_to_gate_dv_ms is not None
    assert cell.gate_to_gate_dv_ms > 0


def test_llsd_alone_cannot_manufacture_a_mesocyclone(svc):
    """The removed `approx_vel = shear x 2000` path.

    Strong LLSD shear with no Vrot from either polar path must leave the cell
    unflagged: shear ranks rotation, it does not diagnose it.
    """
    lat, lon = _point_at(40.0)
    cell = _cell("shear-only", lat, lon)
    cell.llsd_rotation_detected = True
    cell.llsd_max_shear = 0.030          # would have become 60 m/s "Vrot"
    cell.max_rot_velocity_ms = None
    cell.rotation_velocity_ms = None
    cell.vrot_range_km = 40.0

    svc._reconcile_rotation_flags([cell], _radar(_sweep(seed=1)))

    assert not cell.rotation_detected
    assert not cell.tvs_detected
    assert cell.rotation_class is None


def test_a_couplet_becomes_a_mesocyclone_only_on_the_second_volume(svc):
    lat, lon = _point_at(60.0)
    cell = _cell("persist", lat, lon)
    cell.vrot_range_km = 60.0
    cell.max_rot_velocity_ms = 22.0
    cell.rotation_depth_km = 4.0
    cell.rotation_base_km = 1.0
    radar = _radar(_sweep(seed=1))

    svc._reconcile_rotation_flags([cell], radar)
    assert cell.rotation_class == "couplet"
    assert cell.rotation_detected
    assert cell.meso_rank >= rc.MDA_MIN_MESO_RANK

    svc._reconcile_rotation_flags([cell], radar)
    assert cell.rotation_class == "mesocyclone"


def test_tvs_needs_base_and_aloft_gate_to_gate_dv(svc):
    lat, lon = _point_at(60.0)
    cell = _cell("tvs", lat, lon)
    cell.vrot_range_km = 60.0
    cell.max_rot_velocity_ms = 28.0
    cell.rotation_depth_km = 3.5
    cell.rotation_base_km = 0.6
    cell.rotation_volumes = 2
    radar = _radar(_sweep(seed=1))

    # Strong at the base, nothing above it: a couplet, not a TVS.
    cell.gate_to_gate_dv_ms = 30.0
    cell.gate_to_gate_dv_aloft_ms = 18.0
    cell.rotation_tilt_count = 3
    svc._reconcile_rotation_flags([cell], radar)
    assert not cell.tvs_detected
    assert cell.rotation_class == "mesocyclone"

    cell.gate_to_gate_dv_aloft_ms = 40.0
    svc._reconcile_rotation_flags([cell], radar)
    assert cell.tvs_detected
    assert cell.rotation_class == "tvs"
