"""A real train() call must produce a loadable bundle.

This exists because the first version of the bundle save referenced a `target`
variable that was not in scope inside train(). Both models trained to
completion -- full CV, full holdout evaluation, permutation importance -- and
then died on the very last line, so `run_training` returned ok=False after
several minutes of work and the models on disk were left untouched.

Nothing caught it, because every other test uses a hand-built estimator. This
one does the smallest possible end-to-end fit and then loads the result the way
the tracker does.
"""
import numpy as np
import pytest

from backend.services.model_paths import load_model_bundle


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for p in (str(root), str(root / "scripts")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import train_rotation_model as T

    rng = np.random.default_rng(0)
    n, f = 400, len(T.FEATURE_NAMES)
    X = rng.normal(size=(n, f))
    # A learnable signal so calibration has something to work with.
    y = (X[:, 0] + rng.normal(scale=0.4, size=n) > 0.4).astype(int).tolist()
    # Enough distinct convective days that the day-disjoint split is possible.
    groups = [f"2026-05-{(i % 12) + 1:02d}" for i in range(n)]
    times = [f"{g}T18:00:00+00:00" for g in groups]

    out = tmp_path_factory.mktemp("m") / "rotation_model.joblib"
    T.train(X.tolist(), y, groups=groups, times=times, out_path=out,
            holdout_days=3, target="rotation")
    return out, T


def test_training_writes_a_file(trained):
    out, _T = trained
    assert out.exists(), "train() produced no model file"


def test_bundle_records_the_feature_list_it_was_trained_on(trained):
    out, T = trained
    _model, feats = load_model_bundle(out, expected_features=None)
    assert feats == list(T.FEATURE_NAMES)


def test_bundle_records_its_target(trained):
    import joblib
    out, _T = trained
    obj = joblib.load(out)
    assert isinstance(obj, dict), "a bare estimator loses its feature list"
    assert obj["target"] == "rotation"
    assert obj["bundle_version"] >= 1


def test_the_loaded_model_can_score(trained):
    out, T = trained
    model, feats = load_model_bundle(out, expected_features=None)
    row = np.zeros((1, len(feats)))
    p = float(model.predict_proba(row)[0, 1])
    assert 0.0 <= p <= 1.0


def test_a_mismatched_expectation_is_rejected(trained):
    """The whole point: this is what stops new code scoring an old model."""
    from backend.services.model_paths import ModelFeatureMismatch
    out, _T = trained
    with pytest.raises(ModelFeatureMismatch):
        load_model_bundle(out, expected_features=["only", "three", "names"])


def test_nan_features_are_accepted(trained):
    """Absent dual-pol and absent environment both arrive as NaN; an estimator
    that cannot take them makes every probability None at serving time."""
    out, _T = trained
    model, feats = load_model_bundle(out, expected_features=None)
    row = np.full((1, len(feats)), np.nan)
    p = float(model.predict_proba(row)[0, 1])
    assert 0.0 <= p <= 1.0
