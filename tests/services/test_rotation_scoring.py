"""
Per-cell ML scoring: two models, one feature vector, and no silent failures.

The silent-failure half exists because of a real, deployed, invisible bug.
`_cell_to_feature_vector` deliberately emits NaN for absent dual-pol -- 0.0
would tell the model the cell looks like a debris ball, since CC for a weather
target is 0.8-1.0 and CC near zero IS the debris signature. But the model that
was live (a CalibratedClassifierCV over a plain GradientBoostingClassifier)
rejects NaN outright, and the scoring loop caught every exception with a bare
`except Exception: cell.p_rotation_model = None; continue`.

So roughly one cell in five to eight silently got no model score at all, for
months, with no log line and no metric -- indistinguishable from "no model is
loaded". HistGradientBoosting (what the trainer produces now) takes NaN
natively, so promoting a freshly-trained model fixed the immediate breakage;
these tests guard the class of failure rather than that one estimator.

The two-model half guards the newer risk: severe and rotation are scored from
the SAME row, and either model may be absent independently. A load-out with one
model must not blank the other's probability or crash the ensemble vote.

    python -m pytest tests/services/test_rotation_scoring.py -v
"""

import logging

import numpy as np
import pytest

from backend.services.storm_tracking_service import StormTrackingService


class _RejectsNaN:
    """Stands in for the deployed GradientBoostingClassifier."""

    def predict_proba(self, X):
        if np.isnan(np.asarray(X, dtype=float)).any():
            raise ValueError("Input X contains NaN.")
        return np.array([[0.7, 0.3]])


class _Fixed:
    """Scores anything, including NaN, with a known probability."""

    def __init__(self, p):
        self.p = p
        self.calls = 0

    def predict_proba(self, X):
        self.calls += 1
        return np.array([[1.0 - self.p, self.p]])


class _Cell:
    """Only what the scoring loop touches."""

    def __init__(self, scan_count=3):
        self.scan_count = scan_count
        self.p_rotation_model = "untouched"
        self.p_severe_model = "untouched"
        self.rotation_detected = False
        self.severity_score = 50
        self.threat_level = "moderate"


def _svc(rotation=None, severe=None):
    s = StormTrackingService.__new__(StormTrackingService)
    s._rotation_model = rotation
    s._severe_model = severe
    s._rotation_model_features = ["mean_cc"]
    return s


def _vector(value):
    def _fn(self, cell, names):
        return [value]
    return _fn


@pytest.fixture(autouse=True)
def _reset_flag(monkeypatch):
    # Class-level, so a prior test must not decide this one.
    monkeypatch.setattr(StormTrackingService, "_scoring_failure_logged", False)


class TestSilentFailure:
    def test_a_scoring_failure_is_logged_not_swallowed(self, monkeypatch, caplog):
        monkeypatch.setattr(StormTrackingService, "_cell_to_feature_vector",
                            _vector(float("nan")))
        cells = [_Cell()]
        with caplog.at_level(logging.WARNING):
            _svc(rotation=_RejectsNaN())._apply_ml_models(cells)

        assert cells[0].p_rotation_model is None, "a failed score must read None"
        assert any("could not score" in r.getMessage() for r in caplog.records), (
            "the failure was swallowed -- this is exactly the bug: no log line, "
            "and None is indistinguishable from 'no model loaded'")

    def test_the_warning_fires_once_not_once_per_cell(self, monkeypatch, caplog):
        """A scan carries hundreds of cells; a per-cell log buries it too."""
        monkeypatch.setattr(StormTrackingService, "_cell_to_feature_vector",
                            _vector(float("nan")))
        cells = [_Cell() for _ in range(250)]
        with caplog.at_level(logging.WARNING):
            _svc(rotation=_RejectsNaN())._apply_ml_models(cells)

        warnings = [r for r in caplog.records if "could not score" in r.getMessage()]
        assert len(warnings) == 1, f"expected exactly one warning, got {len(warnings)}"
        assert all(c.p_rotation_model is None for c in cells)

    def test_a_failure_clears_both_probabilities(self, monkeypatch):
        """A half-written cell would show a stale severe score beside a blank
        rotation one, which reads as a real forecast."""
        monkeypatch.setattr(StormTrackingService, "_cell_to_feature_vector",
                            _vector(float("nan")))
        cells = [_Cell()]
        _svc(rotation=_RejectsNaN(), severe=_RejectsNaN())._apply_ml_models(cells)
        assert cells[0].p_rotation_model is None
        assert cells[0].p_severe_model is None

    def test_a_healthy_score_still_lands(self, monkeypatch, caplog):
        monkeypatch.setattr(StormTrackingService, "_cell_to_feature_vector",
                            _vector(0.95))
        cells = [_Cell()]
        with caplog.at_level(logging.WARNING):
            _svc(rotation=_RejectsNaN())._apply_ml_models(cells)

        assert cells[0].p_rotation_model == 0.3
        assert not [r for r in caplog.records if "could not score" in r.getMessage()]

    def test_a_nan_tolerant_model_scores_the_same_cell(self, monkeypatch):
        """The feature vector is not the problem -- the estimator is."""
        monkeypatch.setattr(StormTrackingService, "_cell_to_feature_vector",
                            _vector(float("nan")))
        cells = [_Cell()]
        _svc(rotation=_Fixed(0.9))._apply_ml_models(cells)
        assert cells[0].p_rotation_model == 0.9


class TestTwoModels:
    def test_both_probabilities_are_populated(self, monkeypatch):
        monkeypatch.setattr(StormTrackingService, "_cell_to_feature_vector",
                            _vector(0.5))
        cells = [_Cell()]
        _svc(rotation=_Fixed(0.12), severe=_Fixed(0.84))._apply_ml_models(cells)
        assert cells[0].p_rotation_model == 0.12
        assert cells[0].p_severe_model == 0.84

    def test_the_feature_vector_is_built_once_and_scored_twice(self, monkeypatch):
        """Both models take the same row; recomputing it per model would double
        the per-scan cost for nothing."""
        calls = []

        def _spy(self, cell, names):
            calls.append(cell)
            return [0.5]

        monkeypatch.setattr(StormTrackingService, "_cell_to_feature_vector", _spy)
        rot, sev = _Fixed(0.2), _Fixed(0.7)
        _svc(rotation=rot, severe=sev)._apply_ml_models([_Cell()])
        assert len(calls) == 1, "the row was rebuilt per model"
        assert rot.calls == 1 and sev.calls == 1

    def test_severe_alone_still_scores(self, monkeypatch):
        """Rotation absent must not suppress severe."""
        monkeypatch.setattr(StormTrackingService, "_cell_to_feature_vector",
                            _vector(0.5))
        cells = [_Cell()]
        _svc(severe=_Fixed(0.61))._apply_ml_models(cells)
        assert cells[0].p_severe_model == 0.61
        assert cells[0].p_rotation_model == "untouched" or \
               cells[0].p_rotation_model is None

    def test_severe_alone_does_not_crash_the_ensemble_vote(self, monkeypatch):
        """The vote reads p_rotation_model; with no rotation model it is unset."""
        monkeypatch.setattr(StormTrackingService, "_cell_to_feature_vector",
                            _vector(0.5))
        cell = _Cell()
        cell.p_rotation_model = None
        _svc(severe=_Fixed(0.9))._apply_ml_models([cell])
        assert cell.p_severe_model == 0.9
        assert cell.severity_score == 50, "severity must not move without rotation"

    def test_rotation_alone_still_scores(self, monkeypatch):
        monkeypatch.setattr(StormTrackingService, "_cell_to_feature_vector",
                            _vector(0.5))
        cells = [_Cell()]
        _svc(rotation=_Fixed(0.33))._apply_ml_models(cells)
        assert cells[0].p_rotation_model == 0.33

    def test_no_models_is_a_no_op(self, monkeypatch):
        monkeypatch.setattr(StormTrackingService, "_cell_to_feature_vector",
                            _vector(0.5))
        cells = [_Cell()]
        _svc()._apply_ml_models(cells)
        assert cells[0].p_rotation_model == "untouched"
        assert cells[0].p_severe_model == "untouched"


class TestReachesTheFrontend:
    def test_p_severe_model_is_a_declared_dataclass_field(self):
        """asdict() walks DECLARED fields only.

        A dynamically-assigned probability scores correctly, logs correctly and
        never reaches the WebSocket -- which is exactly how mean_cc / min_cc /
        mean_zdr ended up 0.0 in 100% of the training archive.
        """
        import dataclasses
        from backend.services.storm_tracking_service import TrackedStormCell

        names = {f.name for f in dataclasses.fields(TrackedStormCell)}
        assert "p_severe_model" in names, (
            "p_severe_model is not a declared field -- to_dict() will drop it "
            "and the frontend will never see a severe probability")
        assert "p_rotation_model" in names
