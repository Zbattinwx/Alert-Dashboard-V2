"""
The promotion gate.

This is the only thing standing between an automated retrain and a worse model
making on-air rotation calls, so it is tested harder than the training itself.
The failure mode it exists to prevent is silent: a degraded model still returns
probabilities, still renders, and still looks fine on a dashboard.

Run:  python -m pytest tests/services/test_model_training_gate.py -v
"""

import json
from datetime import datetime, timedelta, timezone

import joblib
import numpy as np
import pytest

from backend.services import model_training_service as mts
from scripts.train_rotation_model import FEATURE_NAMES, MIN_HOLDOUT_POS


class RankingStub:
    """A model whose score is `skill`-weighted toward the true label.

    skill=1.0 ranks perfectly; skill=0.0 is pure noise.  `bias` shifts every
    probability without changing the ranking, which separates calibration
    (Brier) from discrimination (AP) — the two the gate weighs differently.
    """

    def __init__(self, skill: float, bias: float = 0.0, seed: int = 0):
        self.skill, self.bias, self.seed = skill, bias, seed

    def predict_proba(self, X):
        X = np.asarray(X, dtype=float)
        # Feature 0 carries the planted signal (see _rows below).
        signal = X[:, 0]
        rng = np.random.default_rng(self.seed)
        noise = rng.random(len(X))
        p = self.skill * signal + (1 - self.skill) * noise
        p = np.clip(p * (1 - abs(self.bias)) + self.bias, 0.001, 0.999)
        return np.column_stack([1 - p, p])


def _rows(n_pos: int, n_neg: int, day: str = "2026-05-20"):
    """Labelled rows on one convective day, signal planted in feature 0."""
    out = []
    base = datetime.fromisoformat(f"{day}T18:00:00+00:00")
    for i in range(n_pos + n_neg):
        pos = i < n_pos
        feats = {k: 0.0 for k in FEATURE_NAMES}
        feats[FEATURE_NAMES[0]] = 0.9 if pos else 0.1
        out.append({
            "ts": (base + timedelta(seconds=30 * i)).isoformat(),
            "cell_id": f"CELL-{i}", "lat": 40.0, "lon": -84.0,
            "label": pos, "features": feats,
        })
    return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point the service at a scratch archive + model paths."""
    data = tmp_path / "training_data.jsonl"
    model = tmp_path / "rotation_model.joblib"
    monkeypatch.setattr(mts, "TRAINING_DATA", data)
    monkeypatch.setattr(mts, "MODEL_PATH", model)
    monkeypatch.setattr(mts, "PREVIOUS_PATH", tmp_path / "rotation_model.previous.joblib")
    monkeypatch.setattr(mts, "CANDIDATE_PATH", tmp_path / "rotation_model.candidate.joblib")
    monkeypatch.setattr(mts, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(mts, "LOCK_PATH", tmp_path / "lock")

    rows = _rows(n_pos=60, n_neg=140)
    data.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return tmp_path, data, model


def _svc():
    return mts.ModelTrainingService(settings=None)


def _cand_metrics(ap, brier=0.15, n_pos=60, days=("2026-05-20",)):
    return {
        "holdout_days": list(days),
        "holdout": {"set": "temporal", "n": 200, "n_pos": n_pos, "n_neg": 200 - n_pos,
                    "ap": ap, "auc": 0.9, "brier": brier, "degenerate": False,
                    "precision": 0.8, "recall": 0.7, "tp": 40, "fp": 10, "fn": 20, "tn": 130},
    }


class TestGate:
    def test_promotes_when_there_is_no_incumbent(self, env):
        _, _, model = env
        assert not model.exists()
        # AP must clear MIN_AP_LIFT x the 0.3 base rate of this fixture.
        d = _svc()._decide(_cand_metrics(ap=0.8))
        assert d["promote"] is True
        assert "no incumbent" in d["why"]

    def test_rejects_a_degenerate_holdout(self, env):
        m = _cand_metrics(ap=0.99)
        m["holdout"]["degenerate"] = True
        d = _svc()._decide(m)
        assert d["promote"] is False

    def test_rejects_a_holdout_with_too_few_positives(self, env):
        d = _svc()._decide(_cand_metrics(ap=0.99, n_pos=MIN_HOLDOUT_POS - 1))
        assert d["promote"] is False
        assert "cannot compare" in d["why"]

    def test_promotes_a_clearly_better_candidate(self, env):
        _, _, model = env
        joblib.dump(RankingStub(skill=0.0, seed=1), model)     # incumbent: noise
        # Candidate AP measured against the same rows the gate will rebuild.
        d = _svc()._decide(_cand_metrics(ap=0.95))
        assert d["promote"] is True
        assert d["incumbent"]["ap"] < d["candidate"]["ap"]

    def test_rejects_a_worse_candidate(self, env):
        _, _, model = env
        joblib.dump(RankingStub(skill=1.0, seed=1), model)     # incumbent: perfect
        d = _svc()._decide(_cand_metrics(ap=0.40))
        assert d["promote"] is False, d["why"]

    def test_rejects_an_equal_candidate(self, env):
        """No churn without evidence: an identical model must not be promoted."""
        _, _, model = env
        inc = RankingStub(skill=1.0, seed=1)
        joblib.dump(inc, model)
        from scripts.train_rotation_model import evaluate
        X = [[0.9 if i < 60 else 0.1] + [0.0] * (len(FEATURE_NAMES) - 1)
             for i in range(200)]
        y = [1] * 60 + [0] * 140
        inc_ap = evaluate(inc, X, y)["ap"]
        d = _svc()._decide(_cand_metrics(ap=inc_ap, brier=0.15))
        assert d["promote"] is False

    def test_calibration_breaks_a_ranking_tie(self, env):
        """Equal AP, better Brier wins — the tracker nudges on fixed 0.10/0.80
        probability thresholds, so calibration is not cosmetic."""
        _, _, model = env
        inc = RankingStub(skill=1.0, bias=0.4, seed=1)   # ranks well, badly calibrated
        joblib.dump(inc, model)
        from scripts.train_rotation_model import evaluate
        X = [[0.9 if i < 60 else 0.1] + [0.0] * (len(FEATURE_NAMES) - 1)
             for i in range(200)]
        y = [1] * 60 + [0] * 140
        inc_m = evaluate(inc, X, y)
        d = _svc()._decide(_cand_metrics(ap=inc_m["ap"], brier=inc_m["brier"] - 0.05))
        assert d["promote"] is True
        assert "Brier" in d["why"]

    def test_rejects_when_the_candidate_records_no_holdout_days(self, env):
        _, _, model = env
        joblib.dump(RankingStub(skill=0.5), model)
        m = _cand_metrics(ap=0.99)
        m["holdout_days"] = []
        d = _svc()._decide(m)
        assert d["promote"] is False

    def test_replaces_an_unloadable_incumbent(self, env):
        """A corrupt model on disk is worse than any candidate."""
        _, _, model = env
        model.write_bytes(b"not a joblib file")
        d = _svc()._decide(_cand_metrics(ap=0.8))
        assert d["promote"] is True
        assert "will not load" in d["why"]


class TestPromoteAndRollback:
    def test_promote_keeps_a_rollback_copy(self, env):
        _, _, model = env
        joblib.dump(RankingStub(skill=0.1), model)
        joblib.dump(RankingStub(skill=0.9), mts.CANDIDATE_PATH)
        mts.CANDIDATE_PATH.with_suffix(".metrics.json").write_text("{}", encoding="utf-8")

        svc = _svc()
        svc._promote()
        assert mts.PREVIOUS_PATH.exists(), "no rollback copy was kept"
        assert not mts.CANDIDATE_PATH.exists(), "candidate was not moved into place"
        assert joblib.load(model).skill == 0.9

    def test_rollback_restores_the_previous_model(self, env):
        _, _, model = env
        joblib.dump(RankingStub(skill=0.1), model)
        joblib.dump(RankingStub(skill=0.9), mts.CANDIDATE_PATH)
        mts.CANDIDATE_PATH.with_suffix(".metrics.json").write_text("{}", encoding="utf-8")
        svc = _svc()
        svc._promote()
        assert svc.rollback()["ok"] is True
        assert joblib.load(model).skill == 0.1

    def test_rollback_without_a_previous_model_fails_cleanly(self, env):
        assert _svc().rollback()["ok"] is False


class TestLock:
    def test_a_second_cycle_cannot_start_while_one_holds_the_lock(self, env):
        a, b = _svc(), _svc()
        assert a._acquire_lock() is True
        assert b._acquire_lock() is False
        a._release_lock()
        assert b._acquire_lock() is True

    def test_a_stale_lock_is_cleared(self, env, monkeypatch):
        """A cycle killed mid-run must not wedge retraining forever."""
        import os, time
        svc = _svc()
        assert svc._acquire_lock() is True
        old = time.time() - (mts.LOCK_STALE_S + 60)
        os.utime(mts.LOCK_PATH, (old, old))
        assert _svc()._acquire_lock() is True


class TestSkillFloor:
    """A candidate must discriminate before calibration is even considered.

    This class exists because of a real near-miss on live data: a candidate with
    ROC-AUC 0.457 and zero true positives was voted through on Brier score,
    since at a 0.02% base rate a model that answers "no" to everything scores a
    near-perfect Brier.
    """

    def test_the_real_near_miss_is_now_rejected(self, env):
        _, _, model = env
        joblib.dump(RankingStub(skill=0.0, seed=1), model)
        m = _cand_metrics(ap=0.00024, brier=0.00117)
        m["holdout"].update(auc=0.457, n=316261, n_pos=61, n_neg=316200,
                            tp=0, fp=26, fn=61, tn=316174)
        d = _svc()._decide(m)
        assert d["promote"] is False, d["why"]
        assert "not ranking storms" in d["why"]

    def test_a_no_op_predictor_cannot_win_on_calibration(self, env):
        _, _, model = env
        joblib.dump(RankingStub(skill=1.0, bias=0.4, seed=1), model)
        m = _cand_metrics(ap=0.0001, brier=0.0001)
        m["holdout"].update(auc=0.50, n=200000, n_pos=60, tp=0, fp=0, fn=60)
        assert _svc()._decide(m)["promote"] is False

    def test_a_model_that_catches_nothing_is_rejected(self, env):
        _, _, model = env
        joblib.dump(RankingStub(skill=0.2, seed=1), model)
        m = _cand_metrics(ap=0.9, brier=0.05)
        m["holdout"].update(auc=0.95, n=1000, n_pos=60, tp=0, fp=0, fn=60)
        d = _svc()._decide(m)
        assert d["promote"] is False
        assert "catches nothing" in d["why"]

    def test_ap_must_beat_the_base_rate(self, env):
        _, _, model = env
        joblib.dump(RankingStub(skill=0.2, seed=1), model)
        m = _cand_metrics(ap=0.031, brier=0.05)          # base rate 0.03
        m["holdout"].update(auc=0.75, n=2000, n_pos=60, tp=5, fp=10, fn=55)
        d = _svc()._decide(m)
        assert d["promote"] is False
        assert "base rate" in d["why"]

    def test_the_floor_also_applies_when_there_is_no_incumbent(self, env):
        """An empty production slot is not a reason to ship an unskilled model."""
        _, _, model = env
        assert not model.exists()
        m = _cand_metrics(ap=0.0002)
        m["holdout"].update(auc=0.45, n=100000, n_pos=60, tp=0, fp=5, fn=60)
        assert _svc()._decide(m)["promote"] is False

    def test_the_comparison_holdout_encodes_absent_dualpol_as_nan(self, env, monkeypatch):
        """The rebuilt holdout must go through feature_row(), like fit and inference.

        An absent dual-pol reading has to reach the model as NaN: CC for a
        weather target is 0.8-1.0, so 0.0 is not a low correlation, it is "not
        measured", and HistGradientBoosting learns a separate branch for it.
        This rebuild used a plain `float(feats.get(k, 0.0))` comprehension, so
        the matrix both models were judged on was one neither had been fitted
        on -- and the AP it reported could not be reconciled with the AP the
        trainer printed for the same model on the same days.
        """
        import math
        from scripts.train_rotation_model import FEATURE_NAMES

        _, data, model = env
        # One day of rows whose dual-pol was never computed (mean_cc absent).
        rows = _rows(n_pos=60, n_neg=140)
        for r in rows:
            for k in ("mean_cc", "min_cc", "mean_zdr"):
                r["features"].pop(k, None)
        data.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

        seen = {}

        def spy(model_obj, X, y, label=""):
            seen[label] = np.asarray(X, dtype=float)
            return {"ap": 0.9, "auc": 0.9, "brier": 0.1, "n": len(y),
                    "n_pos": int(sum(y)), "precision": 0.8, "recall": 0.7,
                    "tp": 40, "fp": 10, "fn": 20, "tn": 130}

        joblib.dump(RankingStub(skill=0.5), model)
        monkeypatch.setattr("scripts.train_rotation_model.evaluate", spy)
        _svc()._decide(_cand_metrics(ap=0.9))

        assert seen, "evaluate was never called"
        X = next(iter(seen.values()))
        for name in ("mean_cc", "min_cc", "mean_zdr"):
            col = X[:, FEATURE_NAMES.index(name)]
            assert np.isnan(col).all(), (
                f"{name} came through as {col[0]!r}; absent dual-pol must be NaN, "
                "not 0.0 -- see feature_row()")
        # A feature that IS present must survive untouched.
        assert not math.isnan(float(X[0, 0]))

    def test_a_genuinely_skilful_candidate_still_passes(self, env):
        _, _, model = env
        joblib.dump(RankingStub(skill=0.0, seed=1), model)
        m = _cand_metrics(ap=0.55, brier=0.05)
        m["holdout"].update(auc=0.88, n=2000, n_pos=60, tp=35, fp=40, fn=25)
        d = _svc()._decide(m)
        assert d["promote"] is True, d["why"]
