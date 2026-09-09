"""Published radar rotation-detection criteria, as pure functions.

Everything here is a number somebody derived and verified, kept apart from the
detectors so it can be tested against the literature without a radar file.

────────────────────────────────────────────────────────────────────────────
THE UNIT TRAP.  Read this before touching any threshold.
────────────────────────────────────────────────────────────────────────────
Three different quantities get called "the rotation number", and they are not
interchangeable:

  Vrot   = (Vmax_outbound - Vmax_inbound) / 2, measured across the vortex
           DIAMETER (km scale).  This is what the MDA ranks.
  dV     = the full difference, measured GATE TO GATE (~250 m).  This is what
           the TDA thresholds.  A gate-to-gate dV of 25 m/s is a far tighter,
           more intense signature than a Vrot of 15 m/s across 5 km -- the
           numbers are similar and the scales are an order of magnitude apart,
           which is exactly why they get confused.
  shear  = dV / distance, in s^-1.  MRMS azimuthal shear.  Only meaningful
           against a FIXED PHYSICAL kernel (see llsd_kernel_rays).

Comparing a km-scale Vrot against the TDA's gate-scale dV threshold is not
conservative in a useful way -- it is a category error.  A TVS cannot be
diagnosed from a 1 km Cartesian grid at all; it needs polar gate-to-gate data.

────────────────────────────────────────────────────────────────────────────
WHY RANGE MATTERS FOR SHEAR AND NOT FOR VELOCITY
────────────────────────────────────────────────────────────────────────────
Vrot is a wind speed: a 20 m/s couplet is 20 m/s whether it sits at 10 km or
at 150 km.  That is why neither the MDA nor the TDA carries a minimum-range
parameter, and why the TDA's false-alarm rate is essentially flat with range
(46% at 0-100 km vs 49% at 100-150 km).  The MDA's mild relaxation with range
is a RESOLUTION correction -- the beam broadens, so a real couplet's measured
peak weakens -- not a noise correction.

Shear is a velocity DIVIDED BY A DISTANCE, so the moment that distance is
"however far apart N rays happen to be", it collapses toward zero as the
range does, and a fixed shear threshold becomes a fixed *fraction* of a
vanishing number.  At 10 km with 0.5 degree rays, adjacent rays are 87 m
apart: a 0.010 s^-1 threshold is asking for 0.87 m/s across the kernel, and
WSR-88D velocity noise is about 2 m/s.  The threshold is then met by noise
alone, everywhere, all the time -- while at 130 km the same threshold asks for
a genuine 11 m/s.  That is the entire near-radar false-alarm problem, and the
fix is not a bigger number: it is a kernel with a fixed size IN METRES, which
is what MRMS uses and why MRMS's threshold is range-independent.

Sources
-------
MDA   Stumpf et al. 1998, Wea. Forecasting 13, 304-326
      "The National Severe Storms Laboratory Mesocyclone Detection Algorithm
      for the WSR-88D"
TDA   Mitchell et al. 1998, Wea. Forecasting 13, 352-366
      "The National Severe Storms Laboratory Tornado Detection Algorithm"
LLSD  Smith & Elmore 2004, 22nd Conf. Severe Local Storms, "The use of radial
      velocity derivatives to diagnose rotation and divergence"
      Mahalik et al. 2019, Wea. Forecasting 34, 1197-1215
"""
from __future__ import annotations

import math

# ── MDA mesocyclone ranks, Vrot in m/s at <= 100 km ────────────────────────
# Stumpf et al. 1998.  Rank 5 is the "minimal mesocyclone"; the operational
# convention is that ranks below 5 are circulations, not mesocyclones.
MDA_RANK_VROT_MS = {
    3: 10.0,   # weak circulation
    5: 15.0,   # minimal mesocyclone
    7: 20.0,   # strong
    9: 25.0,   # very strong
}
MDA_MIN_MESO_RANK = 5

# Range relaxation of the MDA thresholds (Stumpf et al. 1998): 15 m/s at
# <= 100 km, 13.1 at 150 km, 11.3 at >= 200 km.  Stored as a multiplier on the
# <= 100 km value so every rank relaxes together.
_MDA_RANGE_KM = (100.0, 150.0, 200.0)
_MDA_RANGE_FACTOR = (1.0, 13.1 / 15.0, 11.3 / 15.0)

# ── TDA tornado vortex signature, GATE-TO-GATE dV in m/s ───────────────────
# Mitchell et al. 1998.  Both must be met: a strong low-level couplet with
# nothing above it is not a TVS.
TDA_TVS_DV_BASE_MS = 25.0
TDA_TVS_DV_ALOFT_MS = 36.0
# Vertical continuity: the signature must span at least this depth across at
# least this many successive tilts.  The TDA deliberately has no multi-volume
# persistence requirement -- a tornado can form and be gone inside two scans.
TDA_TVS_MIN_DEPTH_KM = 1.5
TDA_TVS_MIN_TILTS = 3

# ── MDA mesocyclone structure ──────────────────────────────────────────────
MDA_MESO_MIN_DEPTH_KM = 3.0
MDA_MESO_MAX_BASE_KM = 5.0
# A single-volume detection is a COUPLET.  It becomes a MESOCYCLONE on the
# second consecutive volume.  This is the MDA's own vocabulary and it is worth
# keeping: it lets a first-scan signal be shown honestly rather than either
# suppressed or overstated.
MDA_MESO_MIN_VOLUMES = 2

# Aspect ratio, radial extent / azimuthal extent.  A vortex is roughly round;
# a gust front or a shear line along the beam is not.  The MDA uses < 2 for a
# mesocyclone, the TDA a looser < 4 for a TVS.
MDA_MAX_ASPECT_RATIO = 2.0
TDA_MAX_ASPECT_RATIO = 4.0

# The MDA discards any circulation further than this from a storm cell --
# published for exactly the problem of clear-air and clutter circulations.
MDA_MAX_CELL_DISTANCE_KM = 20.0

# Reflectivity co-location.  MRMS masks azimuthal shear to within 5 km of
# >= 20 dBZ; the MDA/TDA require > 0 dBZ.  Rotation in clear air is not a
# storm signature whatever the velocity field says.
MIN_REFLECTIVITY_DBZ = 20.0
REFLECTIVITY_SEARCH_KM = 5.0

# ── LLSD azimuthal shear ───────────────────────────────────────────────────
# The MRMS kernel is a fixed PHYSICAL size -- 2500 m azimuthally by 750 m
# radially -- so the number of rays it spans varies with range.  That is the
# whole point: it keeps the shear denominator constant, which is what makes a
# single threshold valid at every range.
LLSD_KERNEL_AZIMUTHAL_M = 2500.0
LLSD_KERNEL_RADIAL_M = 750.0

# Shear magnitudes, s^-1 (MRMS).  Note what is NOT here: there is no published
# "tornadic" azimuthal-shear threshold, so this module does not invent one.
# Shear ranks rotation; it does not diagnose tornadoes.
LLSD_NOISE_FLOOR = 0.006
LLSD_SIGNIFICANT = 0.010
LLSD_EXTREME = 0.050

# Inside this range the LLSD retrieval breaks down regardless of kernel: the
# beam is narrow, ground clutter dominates, and the cone of silence and
# sidelobe returns contaminate the velocity field.  MRMS masks 5-6 km; 10 km
# is our own choice, not a published parameter, and is recorded as such.
LLSD_MIN_RANGE_KM = 10.0

# Ceiling on the kernel half-width, in rays.  A fixed-metre kernel wants more
# and more rays as the range falls, and at gate 0 -- range zero -- it wants an
# infinite number of them.  Every range that could ask for more than this is
# inside the near-radar mask, where the answer is discarded anyway, so the cap
# costs nothing real and stops the sweep-wide kernel from exceeding the ray
# count and disabling the whole detector.  45 rays is 22.5 degrees at 0.5 deg
# spacing, already far wider than the 14 rays wanted at the 10 km mask edge.
LLSD_MAX_KERNEL_RAYS = 45


def mda_range_factor(range_km: float) -> float:
    """Resolution relaxation of the MDA velocity thresholds with range.

    Not a noise correction -- the beam broadens with range, so a real couplet's
    measured peak weakens and the threshold follows it down.
    """
    r = max(0.0, float(range_km))
    if r <= _MDA_RANGE_KM[0]:
        return _MDA_RANGE_FACTOR[0]
    if r >= _MDA_RANGE_KM[-1]:
        return _MDA_RANGE_FACTOR[-1]
    for i in range(len(_MDA_RANGE_KM) - 1):
        lo, hi = _MDA_RANGE_KM[i], _MDA_RANGE_KM[i + 1]
        if lo <= r <= hi:
            f_lo, f_hi = _MDA_RANGE_FACTOR[i], _MDA_RANGE_FACTOR[i + 1]
            return f_lo + (f_hi - f_lo) * (r - lo) / (hi - lo)
    return _MDA_RANGE_FACTOR[-1]


def mda_vrot_threshold(rank: int, range_km: float) -> float:
    """Vrot (m/s) needed for an MDA rank at this range."""
    if rank not in MDA_RANK_VROT_MS:
        raise KeyError(f"no published MDA threshold for rank {rank}")
    return MDA_RANK_VROT_MS[rank] * mda_range_factor(range_km)


def meso_rank(vrot_ms: float | None, range_km: float) -> int:
    """MDA rank 0-9 for a measured Vrot at a range.

    Returns 0 when there is nothing to rank.  Ranks between published anchors
    are interpolated, so a rank is monotonic in Vrot rather than jumping in
    steps of two -- the anchors are the calibrated points, the interpolation
    is presentation.
    """
    if vrot_ms is None or not math.isfinite(vrot_ms) or vrot_ms <= 0:
        return 0
    f = mda_range_factor(range_km)
    ranks = sorted(MDA_RANK_VROT_MS)
    lowest = MDA_RANK_VROT_MS[ranks[0]] * f
    if vrot_ms < lowest:
        # Below the weakest published anchor: scale linearly to it so a real
        # but sub-threshold circulation still orders correctly.
        return max(0, int(round(ranks[0] * vrot_ms / lowest)))
    out = ranks[0]
    for lo, hi in zip(ranks, ranks[1:]):
        v_lo = MDA_RANK_VROT_MS[lo] * f
        v_hi = MDA_RANK_VROT_MS[hi] * f
        if v_lo <= vrot_ms <= v_hi:
            return int(round(lo + (hi - lo) * (vrot_ms - v_lo) / (v_hi - v_lo)))
        if vrot_ms > v_hi:
            out = hi
    return int(min(9, out))


def is_mesocyclone(
    vrot_ms: float | None,
    range_km: float,
    *,
    depth_km: float | None = None,
    base_km: float | None = None,
    volumes: int = 1,
    aspect_ratio: float | None = None,
) -> tuple[bool, str]:
    """MDA mesocyclone test.  Returns (is_meso, reason).

    `reason` names the failing criterion so the caller can log or display why a
    couplet did not become a mesocyclone, rather than it silently vanishing.
    """
    rank = meso_rank(vrot_ms, range_km)
    if rank < MDA_MIN_MESO_RANK:
        return False, f"rank {rank} < {MDA_MIN_MESO_RANK} (Vrot {vrot_ms} m/s at {range_km:.0f} km)"
    if range_km < LLSD_MIN_RANGE_KM:
        return False, f"inside {LLSD_MIN_RANGE_KM:.0f} km near-radar mask"
    if depth_km is not None and depth_km < MDA_MESO_MIN_DEPTH_KM:
        return False, f"depth {depth_km:.1f} km < {MDA_MESO_MIN_DEPTH_KM} km"
    if base_km is not None and base_km > MDA_MESO_MAX_BASE_KM:
        return False, f"base {base_km:.1f} km > {MDA_MESO_MAX_BASE_KM} km"
    if aspect_ratio is not None and aspect_ratio > MDA_MAX_ASPECT_RATIO:
        return False, f"aspect ratio {aspect_ratio:.1f} > {MDA_MAX_ASPECT_RATIO} (linear, not a vortex)"
    if volumes < MDA_MESO_MIN_VOLUMES:
        return False, f"couplet on {volumes} volume (need {MDA_MESO_MIN_VOLUMES})"
    return True, "mesocyclone"


def is_tvs(
    dv_base_ms: float | None,
    dv_aloft_ms: float | None,
    *,
    depth_km: float | None = None,
    tilts: int = 0,
    aspect_ratio: float | None = None,
) -> tuple[bool, str]:
    """TDA tornado vortex signature test.  Inputs are GATE-TO-GATE dV, not Vrot.

    Both the base and the aloft criteria must be met, which is what separates a
    TVS from a strong low-level couplet.
    """
    if dv_base_ms is None or dv_aloft_ms is None:
        return False, "no gate-to-gate dV profile (TVS needs polar data)"
    if dv_base_ms < TDA_TVS_DV_BASE_MS:
        return False, f"base dV {dv_base_ms:.0f} < {TDA_TVS_DV_BASE_MS:.0f} m/s"
    if dv_aloft_ms < TDA_TVS_DV_ALOFT_MS:
        return False, f"aloft dV {dv_aloft_ms:.0f} < {TDA_TVS_DV_ALOFT_MS:.0f} m/s"
    if depth_km is not None and depth_km < TDA_TVS_MIN_DEPTH_KM:
        return False, f"depth {depth_km:.1f} km < {TDA_TVS_MIN_DEPTH_KM} km"
    if tilts and tilts < TDA_TVS_MIN_TILTS:
        return False, f"{tilts} tilts < {TDA_TVS_MIN_TILTS}"
    if aspect_ratio is not None and aspect_ratio > TDA_MAX_ASPECT_RATIO:
        return False, f"aspect ratio {aspect_ratio:.1f} > {TDA_MAX_ASPECT_RATIO}"
    return True, "TVS"


def llsd_kernel_rays(range_km: float, ray_spacing_deg: float) -> int:
    """Half-width in RAYS of a fixed 2500 m azimuthal kernel at this range.

    This is the function that makes a single shear threshold valid everywhere.
    The arc a ray subtends grows with range, so the ray count must shrink:
    about +/-14 rays at 10 km, +/-3 at 50 km, +/-1 beyond ~130 km with 0.5 deg
    rays.  A FIXED ray count -- what we had -- means the denominator collapses
    toward zero near the radar and the threshold is met by noise.
    """
    r_m = max(1.0, float(range_km) * 1000.0)
    per_ray_m = r_m * math.radians(max(1e-4, float(ray_spacing_deg)))
    half = (LLSD_KERNEL_AZIMUTHAL_M / 2.0) / per_ray_m
    return max(1, min(LLSD_MAX_KERNEL_RAYS, int(round(half))))


def llsd_kernel_gates(gate_spacing_m: float) -> int:
    """Half-width in gates of the fixed 750 m radial kernel."""
    g = max(1.0, float(gate_spacing_m))
    return max(1, int(round((LLSD_KERNEL_RADIAL_M / 2.0) / g)))


def llsd_noise_equivalent_dv(range_km: float) -> float:
    """dV (m/s) across the physical kernel that the significant-shear threshold
    implies at this range.  With a fixed physical kernel this is constant --
    which is the point, and what makes it a useful assertion in tests.
    """
    return LLSD_SIGNIFICANT * LLSD_KERNEL_AZIMUTHAL_M


def aspect_ratio(radial_extent_km: float, azimuthal_extent_km: float) -> float | None:
    """Radial / azimuthal extent.  None when it cannot be formed."""
    if azimuthal_extent_km is None or radial_extent_km is None:
        return None
    if azimuthal_extent_km <= 0 or not math.isfinite(azimuthal_extent_km):
        return None
    if not math.isfinite(radial_extent_km):
        return None
    return abs(radial_extent_km) / abs(azimuthal_extent_km)
