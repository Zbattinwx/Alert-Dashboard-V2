"""
The split must be grouped by convective day.

A tracked storm emits one row per volume scan, so consecutive rows are nearly
identical. Under the original random `train_test_split` scan N landed in train
and scan N+1 in validation, and the reported ROC-AUC measured memorisation. That
number is what an automated promotion gate reads, so the leak does not just
flatter a report — it decides what goes on air.

The test below is constructed so the two behaviours give opposite answers:
each convective day gets a RANDOM signature value and an INDEPENDENT random
label. Nothing about a day's features predicts its label, so:

  * a leaky split sees day D in training, meets day D's signature again in
    validation, and scores near-perfect;
  * a day-grouped split only ever meets unseen signatures and scores at chance.

Run:  python -m pytest tests/scripts/test_train_grouping.py -v
"""

import numpy as np
import pytest

from scripts.train_rotation_model import FEATURE_NAMES, convective_day, train


def _day_signature_dataset(n_days=24, rows_per_day=60, seed=7):
    """Label is a property of the DAY, unrelated to feature magnitude."""
    rng = np.random.default_rng(seed)
    X, y, groups = [], [], []
    for d in range(n_days):
        signature = float(rng.uniform(0, 100))
        label = int(rng.integers(0, 2))
        for _ in range(rows_per_day):
            row = [0.0] * len(FEATURE_NAMES)
            row[0] = signature + rng.normal(0, 0.01)   # ~identical within a day
            X.append(row)
            y.append(label)
            groups.append(f"2026-05-{d + 1:02d}")
    return X, y, groups


class TestConvectiveDay:
    def test_evening_and_after_midnight_share_a_day(self):
        """An Ohio severe evening runs 22Z to 04Z. Splitting on calendar days
        would put its first hours in train and its last in test."""
        assert (convective_day("2026-05-20T22:00:00+00:00")
                == convective_day("2026-05-21T02:00:00+00:00"))

    def test_morning_belongs_to_the_previous_convective_day(self):
        assert convective_day("2026-05-21T06:00:00+00:00") == "2026-05-20"
        assert convective_day("2026-05-21T13:00:00+00:00") == "2026-05-21"

    def test_unparsable_timestamp_is_isolated_not_crashing(self):
        assert convective_day("") == "unknown"
        assert convective_day("not a date") == "unknown"


class TestGroupedSplit:
    def test_day_level_leakage_does_not_inflate_the_holdout_score(self, tmp_path):
        """The regression guard. Under a random split this scores ~1.0."""
        X, y, groups = _day_signature_dataset()
        _, metrics = train(X, y, groups=groups,
                           out_path=tmp_path / "m.joblib", holdout_days=6)
        hold = metrics["holdout"]
        assert not hold.get("degenerate"), hold
        assert hold["auc"] < 0.80, (
            f"held-out AUC {hold['auc']:.3f} on a dataset whose labels are pure "
            "day-level noise — the split is leaking across convective days"
        )

    def test_holdout_days_are_recorded_for_the_promotion_gate(self, tmp_path):
        X, y, groups = _day_signature_dataset()
        _, metrics = train(X, y, groups=groups,
                           out_path=tmp_path / "m.joblib", holdout_days=6)
        assert metrics.get("holdout_days"), "gate cannot rebuild an unrecorded holdout"
        assert set(metrics["holdout_days"]).issubset(set(groups))

    def test_a_metrics_sidecar_is_written_next_to_the_model(self, tmp_path):
        X, y, groups = _day_signature_dataset()
        out = tmp_path / "m.joblib"
        train(X, y, groups=groups, out_path=out, holdout_days=6)
        assert out.exists()
        assert out.with_suffix(".metrics.json").exists()

    def test_a_genuinely_predictive_feature_is_still_learned(self, tmp_path):
        """The grouping must remove leakage without destroying real skill —
        otherwise the guard above would pass on a model that learned nothing."""
        rng = np.random.default_rng(3)
        X, y, groups = [], [], []
        for d in range(24):
            for _ in range(60):
                label = int(rng.integers(0, 2))
                row = [0.0] * len(FEATURE_NAMES)
                # llsd_max_shear genuinely separates the classes.
                row[FEATURE_NAMES.index("llsd_max_shear")] = (
                    rng.normal(8.0 if label else 2.0, 1.0))
                X.append(row)
                y.append(label)
                groups.append(f"2026-05-{d + 1:02d}")
        _, metrics = train(X, y, groups=groups,
                           out_path=tmp_path / "m.joblib", holdout_days=6)
        assert metrics["holdout"]["auc"] > 0.90, metrics["holdout"]


class TestDegenerateHoldout:
    def test_an_all_negative_recent_window_falls_back(self, tmp_path):
        """A quiet fortnight cannot score a model. The trainer must notice and
        fall back rather than emit a meaningless number for the gate."""
        X, y, groups = [], [], []
        rng = np.random.default_rng(11)
        for d in range(30):
            for _ in range(60):
                # Positives only in the first ten days; the recent window is quiet.
                label = int(rng.integers(0, 2)) if d < 10 else 0
                row = [0.0] * len(FEATURE_NAMES)
                row[FEATURE_NAMES.index("llsd_max_shear")] = (
                    rng.normal(8.0 if label else 2.0, 1.0))
                X.append(row)
                y.append(label)
                groups.append(f"2026-05-{d + 1:02d}")
        _, metrics = train(X, y, groups=groups,
                           out_path=tmp_path / "m.joblib", holdout_days=10)
        assert metrics["holdout_kind"] == "grouped_random"
        assert metrics["holdout"]["n_pos"] > 0
