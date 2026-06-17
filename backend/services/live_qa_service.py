"""
Live QA Reporter (in-process)
=============================
Subscribed as an additional `on_cells_updated` callback on the storm
tracking service.  Mirrors the per-cell QA print + optional training-data
JSONL append that `live_qa.py` (CLI) does, but without the WebSocket
round-trip.

Auto-starts when `live_qa_enabled` is true in settings, gated on the
storm tracking service starting successfully.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("LiveQA")

# ── Thresholds (mirror storm_tracking_service constants) ──────────────────
MESO_VELOCITY_THRESHOLD_MS = 15.0
TVS_VELOCITY_THRESHOLD_MS  = 25.0
TDS_MIN_ROTATION_MS        = 20.0
DEBRIS_CC_THRESHOLD        = 0.80
MAX_PLAUSIBLE_SPEED_KPH    = 175.0
LLSD_WEAK_SHEAR            = 0.005
LLSD_MESO_SHEAR            = 0.010
LLSD_STRONG_SHEAR          = 0.020
LLSD_TORNADIC_SHEAR        = 0.025


def _shear_label(shear):
    if shear is None:
        return "n/a"
    if shear >= LLSD_TORNADIC_SHEAR:
        return f"{shear:.4f}/s TORNADIC"
    if shear >= LLSD_STRONG_SHEAR:
        return f"{shear:.4f}/s STRONG"
    if shear >= LLSD_MESO_SHEAR:
        return f"{shear:.4f}/s MESO-CLASS"
    if shear >= LLSD_WEAK_SHEAR:
        return f"{shear:.4f}/s weak"
    return f"{shear:.4f}/s sub-threshold"


def _speed_label(speed_kph):
    if speed_kph is None:
        return "0 kph"
    warn = " *** IMPOSSIBLE — BUG" if speed_kph > MAX_PLAUSIBLE_SPEED_KPH else (
        " *** SUSPECT" if speed_kph > 120 else ""
    )
    return f"{speed_kph:.0f} kph{warn}"


def _rot_label(vel_ms, detected, low_level=False, mid_level=False,
               base_km=None, peak_km=None):
    if not detected or vel_ms is None:
        return "none"
    if vel_ms >= TVS_VELOCITY_THRESHOLD_MS:
        return f"{vel_ms:.1f} m/s *** TVS ***"
    if low_level and mid_level:
        depth = ""
        if base_km is not None and peak_km is not None:
            depth = f" ({base_km:.1f}→{peak_km:.1f} km)"
        return f"{vel_ms:.1f} m/s LOW+MID-LEVEL meso{depth}"
    if low_level:
        base = f" base {base_km:.1f} km" if base_km is not None else ""
        return f"{vel_ms:.1f} m/s *** LOW-LEVEL meso ***{base}"
    if mid_level:
        peak = f" @ {peak_km:.1f} km" if peak_km is not None else ""
        return f"{vel_ms:.1f} m/s mid-level meso{peak}"
    return f"{vel_ms:.1f} m/s meso"


def extract_features(cell: dict) -> dict:
    """Extract the feature vector for ML training (mirrors live_qa CLI)."""
    profile = cell.get("rotation_profile") or []
    peak_profile_vel = max((p.get("rot_velocity_ms", 0) for p in profile), default=0.0)
    breakdown = cell.get("score_breakdown") or {}

    # MRMS multi-radar rotation features.  Sampled at the cell's lat/lon from
    # the latest cached MRMS frame, when the rotation service is running.
    # Both default to 0.0 when MRMS is unavailable so the model handles
    # missing data gracefully.
    mrms_rot = 0.0
    mrms_azshear = 0.0
    try:
        from backend.services.mrms_rotation_service import get_mrms_rotation_service
        svc = get_mrms_rotation_service()
        if svc is not None and svc.available:
            lat = cell.get("lat")
            lon = cell.get("lon")
            if lat is not None and lon is not None:
                rt = svc.get_rotation_track_at(float(lat), float(lon))
                if rt is not None:
                    mrms_rot = float(rt)
                az = svc.get_azshear_at(float(lat), float(lon))
                if az is not None:
                    mrms_azshear = float(az)
    except Exception:
        pass

    return {
        "max_dbz":            float(cell.get("max_reflectivity_dbz") or 0),
        "area_km2":           float(cell.get("area_km2") or 0),
        "vil_kg_m2":          float(cell.get("vil_kg_m2") or 0),
        "cell_top_km":        float(cell.get("cell_top_km") or 0),
        "cell_base_km":       float(cell.get("cell_base_km") or 0),
        "depth_km":           float(cell.get("depth_km") or 0),
        "max_ref_height_km":  float(cell.get("max_ref_height_km") or 0),
        "centroid_height_km": float(cell.get("centroid_height_km") or 0),
        "mean_cc":            float(cell.get("mean_cc") or 0),
        "min_cc":             float(cell.get("min_cc") or 0),
        "mean_zdr":           float(cell.get("mean_zdr") or 0),
        "rot_velocity_ms":    float(cell.get("rotation_velocity_ms") or 0),
        "llsd_max_shear":     float(cell.get("llsd_max_shear") or 0),
        "llsd_elevation_deg": float(cell.get("llsd_elevation_deg") or 0),
        "max_rot_vel_profile_ms": float(cell.get("max_rot_velocity_ms") or peak_profile_vel),
        "max_rot_height_km":  float(cell.get("max_rot_height_km") or 0),
        "rotation_depth_km":  float(cell.get("rotation_depth_km") or 0),
        "rotation_base_km":   float(cell.get("rotation_base_km") or 0),
        "motion_speed_kph":   float(cell.get("motion_speed_kph") or 0),
        "motion_dir_deg":     float(cell.get("motion_direction_deg") or 0),
        "score_rotation":     float(breakdown.get("rotation") or 0),
        "llsd_trend":         float(cell.get("llsd_trend") or 0),
        "rot_vel_trend":      float(cell.get("rot_vel_trend") or 0),
        "vil_trend":          float(cell.get("vil_trend") or 0),
        "echo_top_trend":     float(cell.get("echo_top_trend") or 0),
        "dbz_trend":          float(cell.get("dbz_trend") or 0),
        "mrms_rotation_track_30min": mrms_rot,
        "mrms_azshear_0_2km":        mrms_azshear,
    }


def build_training_record(cell: dict, scan_ts: str) -> dict:
    return {
        "ts":       scan_ts,
        "cell_id":  cell.get("cell_id"),
        "site":     cell.get("site"),
        "lat":      cell.get("lat"),
        "lon":      cell.get("lon"),
        "features": extract_features(cell),
        "flags": {
            "rotation_detected":      cell.get("rotation_detected", False),
            "low_level_meso":         cell.get("low_level_meso_detected", False),
            "mid_level_meso":         cell.get("mid_level_meso_detected", False),
            "tvs_detected":           cell.get("tvs_detected", False),
            "qlcs_meso_detected":     cell.get("qlcs_meso_detected", False),
            "llsd_rotation":          cell.get("llsd_rotation_detected", False),
            "debris_signature":       cell.get("debris_signature", False),
            "hail_indicated":         cell.get("hail_indicated", False),
            "bwer_detected":          cell.get("bwer_detected", False),
        },
        "mesh_mm":           cell.get("mesh_mm"),
        "shi_value":         cell.get("shi_value"),
        "p_rotation_model":  cell.get("p_rotation_model"),
        "label": None,
    }


class LiveQAReporter:
    """In-process per-scan QA reporter.

    Created at backend startup when the storm tracking service starts.
    Registered as an additional callback alongside the broker broadcast
    and storm analyst — receives the same `TrackedStormCell` list per scan.
    """

    def __init__(
        self,
        log_file: Optional[Path] = None,
        min_score: int = 30,
        verbose: bool = False,
    ):
        self.log_file = log_file
        self.min_score = min_score
        self.verbose = verbose
        self._scan_count = 0
        if self.log_file is not None:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            logger.info(f"Live QA logging training data to {self.log_file}")
        logger.info(
            f"Live QA reporter running in-process "
            f"(min_score={min_score}, verbose={verbose}, "
            f"log={'on' if log_file else 'off'})"
        )

    async def on_cells(self, cells: list) -> None:
        """Storm tracker callback.  `cells` is a list of TrackedStormCell."""
        try:
            self._scan_count += 1
            scan_ts = datetime.now(timezone.utc).isoformat()

            # Convert dataclasses to dicts (same shape the WebSocket clients see)
            cell_dicts = [
                c.to_dict() if hasattr(c, "to_dict") else dict(c)
                for c in cells
            ]

            notable = [
                c for c in cell_dicts
                if (c.get("severity_score", 0) >= self.min_score
                    or c.get("rotation_detected")
                    or c.get("tvs_detected")
                    or c.get("debris_signature")
                    or c.get("llsd_rotation_detected")
                    or c.get("bwer_detected"))
            ]

            logger.info(
                f"=== Scan #{self._scan_count} | {len(cell_dicts)} cells total | "
                f"{len(notable)} notable ==="
            )

            for cell in sorted(notable, key=lambda c: c.get("severity_score", 0), reverse=True):
                self._display_cell(cell)

            if self.log_file is not None:
                with self.log_file.open("a", encoding="utf-8") as f:
                    for cell in cell_dicts:
                        rec = build_training_record(cell, scan_ts)
                        f.write(json.dumps(rec) + "\n")
        except Exception as e:
            logger.warning(f"Live QA reporter error: {e}", exc_info=True)

    def _display_cell(self, cell: dict) -> None:
        cell_id  = cell.get("cell_id", "?")
        threat   = (cell.get("threat_level") or "minimal").upper()
        score    = cell.get("severity_score", 0)
        dbz      = cell.get("max_reflectivity_dbz", 0)
        area     = cell.get("area_km2", 0)
        lat      = cell.get("lat", 0)
        lon      = cell.get("lon", 0)
        site     = cell.get("site") or "?"

        rot          = cell.get("rotation_detected", False)
        rot_vel      = cell.get("rotation_velocity_ms")
        tvs          = cell.get("tvs_detected", False)
        qlcs         = cell.get("qlcs_meso_detected", False)
        debris       = cell.get("debris_signature", False)
        hail         = cell.get("hail_indicated", False)
        llsd_rot     = cell.get("llsd_rotation_detected", False)
        llsd_shear   = cell.get("llsd_max_shear")
        llsd_elev    = cell.get("llsd_elevation_deg")
        max_prof_vel = cell.get("max_rot_velocity_ms")
        rot_depth    = cell.get("rotation_depth_km")
        rot_h        = cell.get("max_rot_height_km")
        rot_base     = cell.get("rotation_base_km")
        low_level    = cell.get("low_level_meso_detected", False)
        mid_level    = cell.get("mid_level_meso_detected", False)
        bwer         = cell.get("bwer_detected", False)
        bwer_over    = cell.get("bwer_overhang_dbz")
        mesh         = cell.get("mesh_mm")
        p_model      = cell.get("p_rotation_model")
        speed        = cell.get("motion_speed_kph", 0)
        direction    = cell.get("motion_direction_deg", 0)
        vil          = cell.get("vil_kg_m2")
        top          = cell.get("cell_top_km")
        mean_cc      = cell.get("mean_cc")
        scan_count   = cell.get("scan_count", 0)
        trend        = cell.get("trend", "steady")

        flags = []
        if tvs:              flags.append("*** TVS ***")
        elif rot:
            flags.append(f"ROT({_rot_label(rot_vel, rot, low_level, mid_level, rot_base, rot_h)})")
        if llsd_rot:         flags.append(f"LLSD({_shear_label(llsd_shear)})")
        if qlcs:             flags.append("QLCS-MESO")
        if debris:           flags.append("*** TDS ***")
        if bwer:
            over = f" overhang {bwer_over:.0f} dBZ" if bwer_over else ""
            flags.append(f"BWER{over}" if over else "BWER")
        if mesh:
            if mesh >= 76: flags.append(f"*** GIANT HAIL {mesh:.0f}mm ***")
            elif mesh >= 44: flags.append(f"LARGE HAIL {mesh:.0f}mm")
            elif mesh >= 19: flags.append(f"hail {mesh:.0f}mm")
        elif hail:           flags.append("HAIL")
        if p_model is not None:
            flags.append(f"p={p_model:.2f}")
        flag_str = " ".join(flags) if flags else "--"

        logger.info(
            f"[{threat:>12}] {cell_id}  score={score:3d}  "
            f"site={site}  Z={dbz:.0f}dBZ  area={area:.0f}km²  "
            f"({lat:.3f},{lon:.3f})"
        )
        logger.info(f"              flags: {flag_str}")
        logger.info(
            f"              motion: {_speed_label(speed)} at {direction:.0f}°  |  "
            f"trend: {trend}  |  scans tracked: {scan_count}"
        )

        if self.verbose or rot or llsd_rot or debris or tvs or bwer:
            couplet = _rot_label(rot_vel, rot, low_level, mid_level, rot_base, rot_h)
            if max_prof_vel:
                logger.info(
                    f"              grid couplet: {couplet}  |  "
                    f"profile peak: {max_prof_vel:.1f} m/s"
                )
            else:
                logger.info(f"              grid couplet: {couplet}  |  profile: n/a")
            if rot_depth or rot_h or rot_base is not None:
                altitude = []
                if low_level: altitude.append("LOW-LEVEL")
                if mid_level: altitude.append("MID-LEVEL")
                alt_str = f"  [{' + '.join(altitude)}]" if altitude else ""
                logger.info(
                    f"              column: base {rot_base or 0:.1f} km → peak {rot_h or 0:.1f} km AGL  "
                    f"(depth {rot_depth or 0:.1f} km){alt_str}"
                )
            if llsd_shear is not None:
                logger.info(
                    f"              LLSD shear: {_shear_label(llsd_shear)}  "
                    f"(tilt={llsd_elev or 0:.2f}°)"
                )
            if vil or top or mean_cc or mesh:
                mesh_str = f"  MESH={mesh:.0f}mm" if mesh else ""
                logger.info(
                    f"              VIL={vil or 0:.0f} kg/m²  top={top or 0:.1f} km  "
                    f"mean_CC={mean_cc or 0:.3f}{mesh_str}"
                )

        # QA sanity alerts
        if speed > MAX_PLAUSIBLE_SPEED_KPH:
            logger.warning(
                f"  QA ALERT: {cell_id} speed {speed:.0f} kph is physically impossible — "
                "likely multi-site timing bug"
            )
        if debris and not rot:
            logger.warning(
                f"  QA ALERT: {cell_id} debris_signature=True but rotation_detected=False"
            )
        if rot and max_prof_vel and rot_h and rot_h > 8.0:
            logger.warning(
                f"  QA ALERT: {cell_id} rotation peak at {rot_h:.1f} km AGL (>8 km) — "
                "should have been cleared by _reconcile_rotation_flags"
            )


# ───────────────────────────────────────────────────────────────────────────
# Singleton helper
# ───────────────────────────────────────────────────────────────────────────

_reporter: Optional[LiveQAReporter] = None


def get_live_qa_reporter() -> Optional[LiveQAReporter]:
    return _reporter


def create_live_qa_reporter(
    log_file: Optional[Path] = None,
    min_score: int = 30,
    verbose: bool = False,
) -> LiveQAReporter:
    global _reporter
    _reporter = LiveQAReporter(
        log_file=log_file, min_score=min_score, verbose=verbose
    )
    return _reporter
