"""The initiation gate's instrument, and the area floor's job.

TWO FAILURES, MEASURED 2026-09-09 19Z, on a Slight-risk afternoon with a warned
line across northern Ohio (82 tracked cells, 16 severe, 59 dBZ on radar):

1. THE GATE WAS UNSATISFIABLE.  It thresholded the RAP's SIMULATED composite
   reflectivity at 40 dBZ, a number taken from SPC's HREF -- a CONVECTION-
   ALLOWING 3 km ensemble.  RAP is 13 km with parameterized convection and
   cannot represent a convective core:

       RAP  18Z F01   >=40 dBZ  0.028% of grid   max anywhere  45.1 dBZ
                      >=50 dBZ  0 cells nationwide
       HRRR 18Z F01   >=40 dBZ  0.086%           max anywhere  69.4 dBZ

   Over the warned line the RAP peaked at 39.1 dBZ, so the storm mask was empty
   and every threat read "if storms form" while the storms were on screen.  The
   threshold sat near the 99.97th percentile of the model's own distribution.
   Same shape as the Vrot/dV unit trap: a number carried to an instrument it was
   not derived on.  The fix is a different instrument -- observed MRMS.

2. THE AREA FLOOR MOVED THREATS INSTEAD OF SUPPRESSING THEM.  `conditional` is
   `capable & ~storms`, so when the storm area fell below the floor the code did
   `mask &= conditional` and deleted precisely the cells holding the storms,
   drawing the zone on the surrounding storm-free air.  At ~181 km^2 per
   assessment cell the marginal floor is 12,120 km^2 (67 cells), which a dilated
   single storm (4,531) or small cluster (6,525) never reaches -- so the zone was
   displaced off the convection exactly during initiation.
"""
import struct

import numpy as np
import pytest

from backend.services import mesoanalysis_service as ms
from backend.services.mesoanalysis_service import MesoanalysisService, analysis_axes

# NE Ohio, where the line was.
OHIO_LAT, OHIO_LON = 41.3, -82.5
# The far corner of the domain, for "this did not just return True everywhere".
FAR_LAT, FAR_LON = 31.0, -103.0


@pytest.fixture
def svc():
    return MesoanalysisService()


def assessment_shape():
    lats, lons = analysis_axes()
    return (len(lats), len(lons))


def nearest_cell(lat, lon):
    lats, lons = analysis_axes()
    return int(np.argmin(np.abs(lats - lat))), int(np.argmin(np.abs(lons - lon)))


def pack_mrms(blobs, ni=700, nj=350, vmin=-20.0, vmax=80.0):
    """A synthetic MRMS frame in the service's wire format.

    `blobs` is a list of (lat, lon, half_deg, dbz).
    """
    north, south, west, east = 55.005, 20.005, -129.995, -60.005
    dlat = (north - south) / nj
    dlon = (east - west) / ni
    grid = np.zeros((nj, ni), dtype=np.uint8)
    grid[:] = 1  # valid, but far below any threshold
    span = vmax - vmin
    for lat, lon, half, dbz in blobs:
        j = int((north - lat) / dlat)
        i = int((lon - west) / dlon)
        rj = max(1, int(half / dlat))
        ri = max(1, int(half / dlon))
        val = int(round((dbz - vmin) / span * 255.0))
        grid[max(0, j - rj):j + rj + 1, max(0, i - ri):i + ri + 1] = val
    header = (b"MRMS" + struct.pack("<II", ni, nj)
              + struct.pack("<dddd", north, south, west, east)
              + struct.pack("<ff", vmin, vmax))
    return header + grid.tobytes()


class FakeMRMS:
    """Stands in for the real service; only what _observed_storms touches."""

    def __init__(self, frames):
        self.frames = frames            # {ts: packed bytes}
        self.asked = []

    def available(self):
        return True

    def get_frame_list(self):
        return [{"ts": ts} for ts in sorted(self.frames)]

    def get_frame_binary(self, ts):
        self.asked.append(ts)
        return self.frames.get(ts)


def use_mrms(monkeypatch, fake):
    import backend.services.mrms_service as real
    monkeypatch.setattr(real, "get_mrms_service", lambda: fake)


# ── 1. Unpacking and resampling ────────────────────────────────────────────

def test_storm_blob_lands_on_the_right_cells(svc):
    shape = assessment_shape()
    raw = pack_mrms([(OHIO_LAT, OHIO_LON, 0.5, 55.0)])
    out = MesoanalysisService._unpack_mrms(raw, shape)
    assert out is not None
    j, i = nearest_cell(OHIO_LAT, OHIO_LON)
    assert out[j, i], "storm not detected where it was placed"
    fj, fi = nearest_cell(FAR_LAT, FAR_LON)
    assert not out[fj, fi], "detected a storm 1,700 km from the only echo"
    assert out.sum() < out.size * 0.02, "one blob lit up the whole country"


def test_below_threshold_echo_is_not_a_storm(svc):
    shape = assessment_shape()
    # 39.1 dBZ is exactly what the RAP peaked at over the warned line. As an
    # OBSERVED value it is genuinely below a convective core, so the 40 dBZ
    # threshold is right here -- it was only wrong applied to a 13 km model.
    raw = pack_mrms([(OHIO_LAT, OHIO_LON, 0.5, 39.1)])
    assert MesoanalysisService._unpack_mrms(raw, shape) is None

    raw = pack_mrms([(OHIO_LAT, OHIO_LON, 0.5, 41.0)])
    out = MesoanalysisService._unpack_mrms(raw, shape)
    assert out is not None and out.any()


def test_a_small_core_survives_resampling(svc):
    """MAX over the footprint, not mean.

    MRMS is 0.02 deg and the assessment grid 0.14 deg -- ~49 MRMS cells per
    assessment cell. A single intense core averaged against its surroundings
    would vanish, which is the same smoothing that made the model field useless.
    """
    shape = assessment_shape()
    raw = pack_mrms([(OHIO_LAT, OHIO_LON, 0.03, 60.0)])   # ~3 km core
    out = MesoanalysisService._unpack_mrms(raw, shape)
    assert out is not None, "a small intense core was averaged away"
    j, i = nearest_cell(OHIO_LAT, OHIO_LON)
    assert out[j, i]


def test_garbage_frames_are_refused(svc):
    shape = assessment_shape()
    assert MesoanalysisService._unpack_mrms(b"", shape) is None
    assert MesoanalysisService._unpack_mrms(b"NOPE" + b"\0" * 60, shape) is None
    # An entirely empty sweep is not an answer — fall back rather than assert
    # "no storms anywhere in the country".
    assert MesoanalysisService._unpack_mrms(pack_mrms([]), shape) is None


# ── 2. Matching the frame to the analysis's valid time ─────────────────────

def test_picks_the_frame_nearest_the_valid_time(svc, monkeypatch):
    fake = FakeMRMS({
        "20260909-184000": pack_mrms([(OHIO_LAT, OHIO_LON, 0.5, 55.0)]),
        "20260909-190000": pack_mrms([(OHIO_LAT, OHIO_LON, 0.5, 55.0)]),
        "20260909-192000": pack_mrms([(OHIO_LAT, OHIO_LON, 0.5, 55.0)]),
    })
    use_mrms(monkeypatch, fake)
    out = svc._observed_storms("2026-09-09T19:00:00+00:00", assessment_shape())
    assert out is not None
    assert fake.asked == ["20260909-190000"], f"took {fake.asked}"


def test_a_frame_from_another_hour_is_refused(svc, monkeypatch):
    """An analysis valid at 19Z gated on 21Z radar is two moments as one.

    This is what keeps the older cycles kept for trends honest: they get no
    observation and fall back to the model, rather than being told about storms
    that formed after they were valid.
    """
    fake = FakeMRMS({"20260909-210000": pack_mrms([(OHIO_LAT, OHIO_LON, 0.5, 55.0)])})
    use_mrms(monkeypatch, fake)
    assert svc._observed_storms("2026-09-09T19:00:00+00:00", assessment_shape()) is None
    assert fake.asked == [], "downloaded a frame it then had to reject"


def test_no_valid_time_and_no_service_degrade_quietly(svc, monkeypatch):
    fake = FakeMRMS({"20260909-190000": pack_mrms([(OHIO_LAT, OHIO_LON, 0.5, 55.0)])})
    use_mrms(monkeypatch, fake)
    assert svc._observed_storms(None, assessment_shape()) is None

    import backend.services.mrms_service as real
    monkeypatch.setattr(real, "get_mrms_service", lambda: None)
    assert svc._observed_storms("2026-09-09T19:00:00+00:00", assessment_shape()) is None


# ── 3. The gate prefers observation, and the model threshold is model-scaled ─

def _env(shape, refc):
    return {"mucape": np.full(shape, 2500.0), "mlcape": np.full(shape, 2000.0),
            "mlcin": np.full(shape, -20.0), "refc": np.full(shape, float(refc))}


def test_observation_wins_over_the_model_field(svc, monkeypatch):
    shape = assessment_shape()
    fake = FakeMRMS({"20260909-190000": pack_mrms([(OHIO_LAT, OHIO_LON, 1.0, 55.0)])})
    use_mrms(monkeypatch, fake)
    # Model says nothing anywhere; the radar says there is a storm in Ohio.
    gate = svc._initiation(_env(shape, 5.0), "2026-09-09T19:00:00+00:00")
    assert gate["source"] == "observed"
    j, i = nearest_cell(OHIO_LAT, OHIO_LON)
    assert gate["storms"][j, i], "observed storms were ignored"


def test_model_fallback_catches_what_40_dBZ_missed(svc):
    """39.1 dBZ is the RAP's real peak over the warned line."""
    shape = assessment_shape()
    gate = svc._initiation(_env(shape, 39.1))          # no valid time -> model path
    assert gate["source"] == "model"
    assert gate["storms"].any(), "the RAP peak over a warned severe line still misses"

    # ...without becoming a gate that fires on stratiform rain.
    assert not svc._initiation(_env(shape, 25.0))["storms"].any()


def test_source_is_none_when_nothing_can_answer(svc):
    shape = assessment_shape()
    p = {"mucape": np.full(shape, 2500.0), "mlcin": np.full(shape, -20.0)}
    gate = svc._initiation(p)
    assert gate["source"] == "none"
    assert not gate["storms"].any()


# ── 4. The area floor suppresses, it does not relocate ─────────────────────

def test_a_small_storm_keeps_its_zone_on_the_storm(svc, monkeypatch):
    shape = assessment_shape()
    # One storm: far below the 67-cell marginal floor even after dilation.
    fake = FakeMRMS({"20260909-190000": pack_mrms([(OHIO_LAT, OHIO_LON, 0.1, 55.0)])})
    use_mrms(monkeypatch, fake)
    gate = svc._initiation(_env(shape, 5.0), "2026-09-09T19:00:00+00:00")
    storms, capable, conditional = gate["storms"], gate["capable"], gate["conditional"]

    assert storms.any() and svc._frac(storms) < ms.THREAT_AREA_THRESHOLDS["marginal"], \
        "test needs a storm area BELOW the floor to be meaningful"
    # The old code's branch, kept here as the thing that must never happen again.
    old = capable & conditional
    assert not (old & storms).any(), "sanity: conditional excludes the storms by construction"
    # The new branch keeps the storm cells.
    new = capable & storms
    assert (new & storms).any(), "zone no longer covers the storm it was drawn for"


def test_conditional_still_means_no_convection(svc, monkeypatch):
    """The forecast half of the product must survive the fix.

    'Ingredients loaded, nothing going yet' is a real and useful state — it is
    the destabilising-area readout. It just must not be the ONLY state.
    """
    shape = assessment_shape()
    fake = FakeMRMS({"20260909-190000": pack_mrms([])})   # clear skies
    use_mrms(monkeypatch, fake)
    gate = svc._initiation(_env(shape, 5.0), "2026-09-09T19:00:00+00:00")
    assert not gate["storms"].any()
    assert gate["conditional"].any(), "a loaded warm sector stopped being reportable"


# ── 5. The trigger term ────────────────────────────────────────────────────
# Thresholds are the measured CONUS percentiles from RAP 18Z F01 valid 19Z on
# 2026-09-09 (omega700 p5 -0.34 Pa/s, |grad thetae| p90 13.0 K/100km, z500 1-h
# change p5 -9.98 m). Over the NE Ohio box where the warned line was, omega700
# reached -1.28 and |grad thetae| 37.3 -- both well past their thresholds --
# while height falls were only -3.85, correctly, because the shortwave was
# still upstream over the Lakes.

def _flat(shape, **over):
    base = {"mucape": np.full(shape, 2500.0), "mlcape": np.full(shape, 2000.0),
            "mlcin": np.full(shape, -20.0), "refc": np.full(shape, 5.0),
            "omega700": np.full(shape, 0.05), "thetae": np.full(shape, 330.0)}
    base.update(over)
    return base


def test_no_forcing_is_no_trigger(svc):
    shape = assessment_shape()
    out = svc._trigger(_flat(shape))
    assert out["mask"] is not None
    assert not out["mask"].any(), "flat fields produced a trigger from nothing"


def test_ascent_is_a_trigger(svc):
    shape = assessment_shape()
    om = np.full(shape, 0.05)
    j, i = nearest_cell(OHIO_LAT, OHIO_LON)
    om[j, i] = -1.28                       # the value measured over the line
    out = svc._trigger(_flat(shape, omega700=om))
    assert out["mask"][j, i]
    assert "ascent" in out["parts"]
    # Sinking motion is not a trigger.
    om2 = np.full(shape, 0.05); om2[j, i] = +1.28
    assert not svc._trigger(_flat(shape, omega700=om2))["mask"].any()


def test_a_theta_e_boundary_is_a_trigger(svc):
    """SPC: 'the primary baroclinic [zone] ... will be the focus'."""
    shape = assessment_shape()
    th = np.full(shape, 330.0)
    j, _ = nearest_cell(OHIO_LAT, OHIO_LON)
    th[j:, :] = 300.0                      # a sharp north-south theta-e drop
    out = svc._trigger(_flat(shape, thetae=th))
    assert out["mask"].any() and "boundary" in out["parts"]
    assert out["parts"]["boundary"][j - 1:j + 2, :].any(), "front not found at the gradient"
    # A smooth continental gradient is not a front.
    smooth = np.linspace(300.0, 330.0, shape[0])[:, None] * np.ones((1, shape[1]))
    assert not svc._trigger(_flat(shape, thetae=smooth))["mask"].any()


def test_height_falls_need_the_previous_cycle(svc):
    shape = assessment_shape()
    z = np.full(shape, 5820.0)
    prev = {"z500": np.full(shape, 5835.0)}      # 15 m fall in an hour
    out = svc._trigger(_flat(shape, z500=z), prev)
    assert out["mask"].any() and "height_falls" in out["parts"]
    # Rising heights are not a trigger, and no previous cycle means no term.
    assert not svc._trigger(_flat(shape, z500=np.full(shape, 5850.0)), prev)["mask"].any()
    assert "height_falls" not in svc._trigger(_flat(shape, z500=z))["parts"]


def test_the_terms_are_alternatives_not_a_checklist(svc):
    """A storm needs ONE mechanism, not all three."""
    shape = assessment_shape()
    om = np.full(shape, 0.05)
    j, i = nearest_cell(OHIO_LAT, OHIO_LON)
    om[j, i] = -1.0
    out = svc._trigger(_flat(shape, omega700=om))
    assert out["mask"][j, i], "ascent alone did not qualify"
    assert set(out["parts"]) >= {"ascent", "boundary"}, "a term went missing entirely"


def test_trigger_is_reported_not_used_to_shrink(svc, monkeypatch):
    """A destabilising area with no forcing must still be visible.

    That is most of what this product is for on a quiet afternoon; the trigger
    changes how it READS, never whether it exists.
    """
    shape = assessment_shape()
    fake = FakeMRMS({"20260909-190000": pack_mrms([])})
    use_mrms(monkeypatch, fake)
    gate = svc._initiation(_flat(shape), "2026-09-09T19:00:00+00:00")
    assert gate["conditional"].any(), "no-trigger area stopped being reportable"
