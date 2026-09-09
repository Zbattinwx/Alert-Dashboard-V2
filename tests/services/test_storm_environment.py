"""Near-storm environment features, and the model bundle that lets them ship.

Two things are pinned here, and they are the same thing viewed from either end:
the feature list a model was trained on must travel WITH the model, and an
absent environment must be NaN rather than a plausible-looking zero.
"""
import math

import numpy as np
import pytest

from backend.services import storm_environment as se
from backend.services.model_paths import ModelFeatureMismatch, load_model_bundle


# ── Sampling ───────────────────────────────────────────────────────────────

def test_every_feature_name_is_always_present():
    """Callers splat this into a feature dict, so a missing key would shift
    every column after it."""
    env = se.environment_at(39.42, -83.82)
    assert set(env) == set(se.ENV_FEATURE_NAMES)


def test_off_grid_is_nan_not_zero():
    """Zero CAPE is a real atmosphere. 'Outside CONUS' is not one, and the two
    must not be spelled the same way."""
    env = se.environment_at(50.0, 5.0)          # Belgium
    assert all(math.isnan(v) for v in env.values())


def test_missing_coordinates_are_nan():
    assert all(math.isnan(v) for v in se.environment_at(None, None).values())


def test_sample_rejects_a_point_far_off_the_grid():
    lats = np.array([40.0, 39.0, 38.0])
    lons = np.array([-90.0, -89.0, -88.0])
    g = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]])
    # argmin always returns SOMETHING, so without the distance check a storm in
    # Spain would silently take the value of the nearest grid edge.
    assert se._sample(g, lats, lons, 39.0, -89.0) == 5.0
    assert math.isnan(se._sample(g, lats, lons, 20.0, -89.0))
    assert math.isnan(se._sample(g, lats, lons, 39.0, -60.0))


def test_non_finite_grid_values_become_nan():
    lats = np.array([40.0]); lons = np.array([-90.0])
    assert math.isnan(se._sample(np.array([[np.nan]]), lats, lons, 40.0, -90.0))


def test_sampling_never_raises_without_an_analysis(monkeypatch):
    """The environment is a nice-to-have; a scan that crashes is an outage."""
    monkeypatch.setattr(se._cache, "get", lambda: (None, None, None, None, None))
    env = se.environment_at(39.42, -83.82)
    assert all(math.isnan(v) for v in env.values())


# ── Feature-list parity ────────────────────────────────────────────────────

def test_training_and_inference_builders_agree():
    """live_qa.extract_features (training rows) and _cell_to_feature_vector
    (serving) must emit the same names, or the model is trained on one
    distribution and served another."""
    from scripts.train_rotation_model import FEATURE_NAMES
    from backend.services.live_qa_service import extract_features

    feats = extract_features({"max_reflectivity_dbz": 55, "lat": 39.5, "lon": -84.0})
    missing = [n for n in FEATURE_NAMES if n not in feats]
    assert not missing, f"training rows would omit {missing}"


def test_feature_row_length_matches_the_name_list():
    from scripts.train_rotation_model import FEATURE_NAMES, feature_row
    assert len(feature_row({})) == len(FEATURE_NAMES)


def test_absent_environment_is_nan_in_the_feature_row():
    from scripts.train_rotation_model import FEATURE_NAMES, feature_row
    row = feature_row({})           # nothing supplied at all
    for name in se.ENV_FEATURE_NAMES:
        assert math.isnan(row[FEATURE_NAMES.index(name)]), f"{name} defaulted to 0.0"


def test_env_names_are_registered_for_training():
    from scripts.train_rotation_model import FEATURE_NAMES
    assert not [n for n in se.ENV_FEATURE_NAMES if n not in FEATURE_NAMES]


# ── Model bundle ───────────────────────────────────────────────────────────

class _Est:
    def __init__(self, n):
        self.n_features_in_ = n


def test_bundle_feature_list_wins_over_the_builds_ordering(tmp_path):
    """The bundle decides which columns and in what ORDER -- a model knows its
    own layout better than whatever constant the build happens to ship."""
    import joblib
    p = tmp_path / "m.joblib"
    joblib.dump({"bundle_version": 1, "model": _Est(3),
                 "features": ["c", "a", "b"], "target": "rotation"}, p)
    model, feats = load_model_bundle(p, ["a", "b", "c", "d"])
    assert feats == ["c", "a", "b"]
    assert model.n_features_in_ == 3


def test_a_bundle_asking_for_features_this_build_cannot_compute_is_rejected(tmp_path):
    """Count alone is not enough. The caller's row builder returns 0.0 for a
    name it does not know, so an unknown feature would be fed as "no CAPE"
    rather than as missing -- right shape, wrong meaning, entirely silent."""
    import joblib
    p = tmp_path / "m.joblib"
    joblib.dump({"bundle_version": 1, "model": _Est(3),
                 "features": ["a", "b", "env_mlcape"], "target": "rotation"}, p)
    with pytest.raises(ModelFeatureMismatch) as e:
        load_model_bundle(p, ["a", "b", "c"])
    assert "env_mlcape" in str(e.value)


def test_legacy_bare_estimator_falls_back_to_the_supplied_list(tmp_path):
    import joblib
    p = tmp_path / "legacy.joblib"
    joblib.dump(_Est(3), p)
    _model, feats = load_model_bundle(p, ["a", "b", "c"])
    assert feats == ["a", "b", "c"]


def test_a_stale_model_raises_instead_of_scoring_on_wrong_columns(tmp_path):
    """The actual failure this prevents: find_model prefers the RUNTIME copy
    over the bundled seed, so an update routinely lands new code on an old
    model file. Silently, that made every probability None."""
    import joblib
    p = tmp_path / "stale.joblib"
    joblib.dump(_Est(27), p)
    with pytest.raises(ModelFeatureMismatch) as e:
        load_model_bundle(p, [f"f{i}" for i in range(39)])
    assert "27" in str(e.value) and "39" in str(e.value)


def test_mismatch_message_names_the_file(tmp_path):
    import joblib
    p = tmp_path / "rotation_model.joblib"
    joblib.dump(_Est(2), p)
    with pytest.raises(ModelFeatureMismatch) as e:
        load_model_bundle(p, ["a"])
    assert "rotation_model.joblib" in str(e.value)


def test_loading_a_mismatched_model_disables_scoring_rather_than_crashing(tmp_path):
    """The tracker must survive it -- degraded, loudly, but running."""
    import joblib
    from backend.services.storm_tracking_service import StormTrackingService
    p = tmp_path / "rotation_model.joblib"
    joblib.dump(_Est(27), p)
    svc = StormTrackingService()
    assert svc.load_rotation_model(str(p)) is False
    assert svc._rotation_model is None


# ── Lightning ──────────────────────────────────────────────────────────────

def test_lightning_features_are_registered_and_produced():
    from scripts.train_rotation_model import FEATURE_NAMES
    from backend.services.live_qa_service import extract_features
    for n in ("flash_rate_fpm", "flash_rate_trend"):
        assert n in FEATURE_NAMES
        assert n in extract_features({"max_reflectivity_dbz": 40})


def test_flash_rate_trend_is_computed_from_history():
    """The jump is the signal, so the rate has to be recorded per scan and
    differenced -- a raw rate alone cannot express 'rapidly increasing'."""
    from backend.services.storm_tracking_service import StormTrackingService

    svc = StormTrackingService()
    cell = _cell_for_trends()
    for i, rate in enumerate((1.0, 4.0, 9.0)):
        cell.flash_rate_fpm = rate
        svc._compute_trends([cell], f"2026-09-09T0{i}:00:00Z")
    assert cell.flash_rate_trend is not None
    assert cell.flash_rate_trend > 0, "a rising flash rate must show a positive jump"


def test_flat_lightning_gives_no_jump():
    from backend.services.storm_tracking_service import StormTrackingService
    svc = StormTrackingService()
    cell = _cell_for_trends()
    for i in range(3):
        cell.flash_rate_fpm = 5.0
        svc._compute_trends([cell], f"2026-09-09T0{i}:00:00Z")
    assert cell.flash_rate_trend == 0


def _cell_for_trends():
    from backend.services.storm_tracking_service import TrackedStormCell
    return TrackedStormCell(
        cell_id="L1", lat=39.5, lon=-84.0, max_reflectivity_dbz=50, area_km2=30,
        severity_score=40, threat_level="moderate", motion_direction_deg=240,
        motion_speed_kph=50, rotation_detected=False, rotation_velocity_ms=None,
        tvs_detected=False, qlcs_meso_detected=False, qlcs_meso_velocity_ms=None,
        hail_indicated=False, hail_max_dbz=None, debris_signature=False,
        vil_kg_m2=25, cell_top_km=11, track_history=[], forecast_track=[],
        score_breakdown={}, first_detected="2026-09-09T00:00:00Z",
        last_updated="2026-09-09T00:00:00Z", trend="steady", scan_count=3)


# ── Time-aware sampling (archive replay) ───────────────────────────────────

def test_a_historical_scan_never_gets_todays_atmosphere(monkeypatch):
    """The failure this prevents.

    backfill_training_data replays archived volumes through the LIVE pipeline,
    and the live pipeline asks for the CURRENT analysis. Without a scan time,
    a storm from 2024 would be labelled with tonight's CAPE and shear -- not a
    degraded value but a fabricated one, and one that would look perfectly
    plausible in the archive forever after.
    """
    from datetime import datetime, timezone

    marker = {n: 999.0 for n in se.ENV_FIELDS}
    lats = np.array([40.0, 39.0]); lons = np.array([-85.0, -84.0])
    grids = {k: np.full((2, 2), v) for k, v in marker.items()}
    monkeypatch.setattr(se._cache, "get",
                        lambda: (grids, lats, lons, "now", "2026-09-09T03:00:00+00:00"))
    monkeypatch.setattr(se, "_historical", lambda at: (None, None, None, None))

    old = datetime(2024, 5, 18, 21, 0, tzinfo=timezone.utc)
    env = se.environment_at(39.0, -84.0, at=old)
    assert all(math.isnan(v) for v in env.values()), (
        "a historical scan took the live analysis")


def test_a_live_scan_still_uses_the_live_analysis(monkeypatch):
    from datetime import datetime, timezone
    lats = np.array([40.0, 39.0]); lons = np.array([-85.0, -84.0])
    grids = {k: np.full((2, 2), 7.0) for k in se.ENV_FIELDS}
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(se._cache, "get",
                        lambda: (grids, lats, lons, "now", now.isoformat()))
    env = se.environment_at(39.0, -84.0, at=now)
    assert env["env_mlcape"] == 7.0


def test_a_scan_the_cached_analysis_does_not_cover_is_refused(monkeypatch):
    """Even live, an analysis hours away from the scan does not describe it."""
    from datetime import datetime, timedelta, timezone
    lats = np.array([40.0, 39.0]); lons = np.array([-85.0, -84.0])
    grids = {k: np.full((2, 2), 7.0) for k in se.ENV_FIELDS}
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(se._cache, "get",
                        lambda: (grids, lats, lons, "stale",
                                 (now - timedelta(hours=9)).isoformat()))
    env = se.environment_at(39.0, -84.0, at=now)
    assert all(math.isnan(v) for v in env.values())


def test_omitting_the_time_keeps_the_live_behaviour(monkeypatch):
    lats = np.array([40.0, 39.0]); lons = np.array([-85.0, -84.0])
    grids = {k: np.full((2, 2), 3.0) for k in se.ENV_FIELDS}
    monkeypatch.setattr(se._cache, "get",
                        lambda: (grids, lats, lons, "now", None))
    assert se.environment_at(39.0, -84.0)["env_mlcape"] == 3.0


def test_naive_timestamps_are_treated_as_utc():
    """The archive writes ISO strings; some carry no offset. Guessing local
    time would shift a storm by hours into a different atmosphere."""
    from datetime import datetime
    env = se.environment_at(None, None, at=datetime(2024, 5, 18, 21, 0))
    assert set(env) == set(se.ENV_FEATURE_NAMES)


def test_the_training_record_passes_its_scan_time():
    """Parity guard: build_training_record must hand the timestamp down, or the
    whole protection above is dead code during a backfill."""
    import inspect
    from backend.services import live_qa_service as lq
    src = inspect.getsource(lq.build_training_record)
    assert "extract_features(cell, scan_ts)" in src
