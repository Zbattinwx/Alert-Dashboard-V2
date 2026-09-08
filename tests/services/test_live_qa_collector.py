"""
What the collector writes to the training archive.

Two bugs in this path made 3 of the classifier's 27 features useless and buried
the useful rows under drizzle.  Both were invisible from row counts.

Run:  python -m pytest tests/services/test_live_qa_collector.py -v
"""

import pytest

from backend.services.live_qa_service import (
    LOG_MIN_SCORE, build_training_record, extract_features, should_log_cell,
)


def _cell(**kw):
    base = {
        "cell_id": "CELL-1", "lat": 40.0, "lon": -84.0,
        "max_reflectivity_dbz": 30.0, "severity_score": 0,
    }
    base.update(kw)
    return base


class TestLogGate:
    def test_drizzle_is_not_logged(self):
        """September 2026 collected 301,715 rows in 7 days, nearly all of this."""
        assert should_log_cell(_cell(max_reflectivity_dbz=30.0), 40.0) is False

    def test_convective_reflectivity_is_logged(self):
        assert should_log_cell(_cell(max_reflectivity_dbz=52.0), 40.0) is True

    @pytest.mark.parametrize("flag", [
        "rotation_detected", "low_level_meso_detected", "mid_level_meso_detected",
        "tvs_detected", "qlcs_meso_detected", "llsd_rotation_detected",
        "debris_signature", "hail_indicated", "bwer_detected",
    ])
    def test_any_flagged_cell_is_logged_however_weak(self, flag):
        """A tornado-warned QLCS cell can sit below the dBZ floor. Losing a
        positive costs far more than keeping a marginal negative."""
        assert should_log_cell(_cell(max_reflectivity_dbz=22.0, **{flag: True}), 40.0) is True

    def test_a_high_scoring_cell_is_logged_below_the_floor(self):
        assert should_log_cell(
            _cell(max_reflectivity_dbz=25.0, severity_score=LOG_MIN_SCORE + 1), 40.0
        ) is True

    def test_the_floor_is_configurable(self):
        c = _cell(max_reflectivity_dbz=38.0)
        assert should_log_cell(c, 40.0) is False
        assert should_log_cell(c, 35.0) is True

    def test_missing_fields_do_not_crash(self):
        assert should_log_cell({}, 40.0) is False


class TestDualPolReachesTheRecord:
    def test_dual_pol_is_carried_into_the_training_features(self):
        """These were 0.0 in 100% of rows in every month of the archive because
        TrackedStormCell never received them from the detection."""
        rec = build_training_record(
            _cell(mean_cc=0.91, min_cc=0.74, mean_zdr=0.35), "2026-05-20T22:00:00+00:00")
        f = rec["features"]
        assert f["mean_cc"] == pytest.approx(0.91)
        assert f["min_cc"] == pytest.approx(0.74)
        assert f["mean_zdr"] == pytest.approx(0.35)

    def test_absent_dual_pol_still_defaults_to_zero(self):
        f = extract_features(_cell())
        assert f["mean_cc"] == 0.0 and f["min_cc"] == 0.0 and f["mean_zdr"] == 0.0


class TestTrainServeParity:
    """The training builder and the inference builder must agree feature by
    feature.  They diverged: inference hardcoded mean_cc/min_cc/mean_zdr to 0.0
    while training read the real values, so the moment the cell started carrying
    dual-pol the model would have been trained on one distribution and served
    another — with nothing raising."""

    def test_both_builders_agree_on_every_shared_feature(self):
        from backend.services.storm_tracking_service import (
            StormTrackingService, TrackedStormCell,
        )
        from scripts.train_rotation_model import FEATURE_NAMES

        cell = TrackedStormCell(
            cell_id="CELL-1", lat=40.0, lon=-84.0,
            max_reflectivity_dbz=58.0, area_km2=44.0, severity_score=61,
            threat_level="severe", motion_direction_deg=225.0, motion_speed_kph=58.0,
            rotation_detected=True, rotation_velocity_ms=19.0,
            tvs_detected=False, qlcs_meso_detected=False, qlcs_meso_velocity_ms=None,
            hail_indicated=True, hail_max_dbz=58.0, debris_signature=False,
            vil_kg_m2=42.0, cell_top_km=13.1,
            track_history=[], forecast_track=[], score_breakdown={"rotation": 18.0},
            first_detected="2026-05-20T21:30:00+00:00",
            last_updated="2026-05-20T22:00:00+00:00",
            trend="strengthening", scan_count=7,
        )
        cell.mean_cc, cell.min_cc, cell.mean_zdr = 0.93, 0.71, 0.42
        cell.llsd_max_shear, cell.llsd_elevation_deg = 0.0221, 0.48
        cell.max_rot_velocity_ms, cell.max_rot_height_km = 21.0, 3.4
        cell.rotation_depth_km = 4.2
        cell.llsd_trend, cell.rot_vel_trend = 0.004, 2.1
        cell.vil_trend, cell.echo_top_trend, cell.dbz_trend = 1.2, 0.4, 0.9

        serve = StormTrackingService._cell_to_feature_vector(cell, FEATURE_NAMES)
        train_feats = extract_features(cell.to_dict())
        train = [float(train_feats.get(n, 0.0)) for n in FEATURE_NAMES]

        mismatched = [
            (n, t, s) for n, t, s in zip(FEATURE_NAMES, train, serve)
            if abs(t - s) > 1e-9
        ]
        assert not mismatched, f"train/serve skew: {mismatched}"

    def test_the_dual_pol_features_are_not_hardcoded_at_inference(self):
        """Guards the specific regression: a constant 0.0 in the serve path."""
        from backend.services.storm_tracking_service import (
            StormTrackingService, TrackedStormCell,
        )
        names = ["mean_cc", "min_cc", "mean_zdr"]
        cell = TrackedStormCell(
            cell_id="C", lat=40.0, lon=-84.0, max_reflectivity_dbz=58.0,
            area_km2=44.0, severity_score=61, threat_level="severe",
            motion_direction_deg=0.0, motion_speed_kph=0.0,
            rotation_detected=False, rotation_velocity_ms=None,
            tvs_detected=False, qlcs_meso_detected=False, qlcs_meso_velocity_ms=None,
            hail_indicated=False, hail_max_dbz=None, debris_signature=False,
            vil_kg_m2=None, cell_top_km=None, track_history=[], forecast_track=[],
            score_breakdown={}, first_detected="2026-05-20T22:00:00+00:00",
            last_updated="2026-05-20T22:00:00+00:00", trend="steady", scan_count=1,
        )
        cell.mean_cc, cell.min_cc, cell.mean_zdr = 0.93, 0.71, 0.42
        assert StormTrackingService._cell_to_feature_vector(cell, names) == [0.93, 0.71, 0.42]


class TestAbsentDualPolIsNaN:
    """Absent dual-pol must be NaN, not 0.0.

    Copolar correlation for a weather target is 0.8-1.0. CC collapsing to zero
    IS the debris signature — the strongest single tornado indicator on radar.
    Storing "not computed" as 0.0 therefore told the model that 391,471 ordinary
    storms (78% of the archive) looked like debris balls, and doubled as a
    giveaway for which collection era a row came from.
    """

    @staticmethod
    def _cell(**kw):
        from backend.services.storm_tracking_service import TrackedStormCell
        c = TrackedStormCell(
            cell_id="C", lat=40.0, lon=-84.0, max_reflectivity_dbz=58.0,
            area_km2=44.0, severity_score=61, threat_level="severe",
            motion_direction_deg=0.0, motion_speed_kph=0.0,
            rotation_detected=False, rotation_velocity_ms=None,
            tvs_detected=False, qlcs_meso_detected=False, qlcs_meso_velocity_ms=None,
            hail_indicated=False, hail_max_dbz=None, debris_signature=False,
            vil_kg_m2=None, cell_top_km=None, track_history=[], forecast_track=[],
            score_breakdown={}, first_detected="2026-05-20T22:00:00+00:00",
            last_updated="2026-05-20T22:00:00+00:00", trend="steady", scan_count=1,
        )
        for k, v in kw.items():
            setattr(c, k, v)
        return c

    def test_inference_reports_nan_when_dual_pol_was_not_computed(self):
        import math
        from backend.services.storm_tracking_service import StormTrackingService
        v = StormTrackingService._cell_to_feature_vector(
            self._cell(), ["mean_cc", "min_cc", "mean_zdr"])
        assert all(math.isnan(x) for x in v), v

    def test_inference_reports_real_values_when_present(self):
        from backend.services.storm_tracking_service import StormTrackingService
        v = StormTrackingService._cell_to_feature_vector(
            self._cell(mean_cc=0.93, min_cc=0.71, mean_zdr=0.42),
            ["mean_cc", "min_cc", "mean_zdr"])
        assert v == [0.93, 0.71, 0.42]

    def test_training_maps_the_same_sentinel_to_nan(self):
        import math
        from scripts.train_rotation_model import FEATURE_NAMES, feature_row
        row = feature_row({"max_dbz": 58.0})          # no dual-pol at all
        for name in ("mean_cc", "min_cc", "mean_zdr"):
            assert math.isnan(row[FEATURE_NAMES.index(name)]), name
        # A real feature is untouched.
        assert row[FEATURE_NAMES.index("max_dbz")] == 58.0

    def test_train_and_serve_agree_on_both_branches(self):
        """The two builders must make the same call, or the model trains on one
        distribution and infers on another."""
        import math
        from backend.services.storm_tracking_service import StormTrackingService
        from backend.services.live_qa_service import extract_features
        from scripts.train_rotation_model import FEATURE_NAMES, feature_row

        for kw in ({}, {"mean_cc": 0.93, "min_cc": 0.71, "mean_zdr": 0.42}):
            cell = self._cell(**kw)
            serve = StormTrackingService._cell_to_feature_vector(cell, FEATURE_NAMES)
            train = feature_row(extract_features(cell.to_dict()))
            for n, t, s in zip(FEATURE_NAMES, train, serve):
                if math.isnan(t) or math.isnan(s):
                    assert math.isnan(t) and math.isnan(s), f"{n}: train={t} serve={s}"
                else:
                    assert abs(t - s) < 1e-9, f"{n}: train={t} serve={s}"

    def test_a_genuine_zero_zdr_is_preserved(self):
        """ZDR of 0.0 is physically real (spherical drops) — it must survive as
        long as CC says the dual-pol analysis actually ran."""
        from scripts.train_rotation_model import FEATURE_NAMES, feature_row
        row = feature_row({"mean_cc": 0.95, "min_cc": 0.90, "mean_zdr": 0.0})
        assert row[FEATURE_NAMES.index("mean_zdr")] == 0.0
