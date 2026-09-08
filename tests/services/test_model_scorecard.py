"""
Live scorecard: the ways it could flatter itself, pinned.

Every test here corresponds to a way of counting that would make a broken model
look better than it is. The scorecard exists to be trusted during severe
weather, so a number it reports too high is worse than no number at all.

    python -m pytest tests/services/test_model_scorecard.py -v
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.services.model_scorecard import (
    DEFAULT_OP, scorecard, verdict, _target_truth,
)


def _row(ts, label=None, source=None, p_rot=None, p_sev=None):
    r = {"ts": ts.isoformat(), "cell_id": "C1", "site": "KILN",
         "features": {}, "label": label}
    if source is not None:
        r["label_source"] = source
    if p_rot is not None:
        r["p_rotation_model"] = p_rot
    if p_sev is not None:
        r["p_severe_model"] = p_sev
    return r


@pytest.fixture
def arena(tmp_path):
    def build(rows):
        p = tmp_path / "training_data.jsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return p, tmp_path
    return build


NOW = datetime.now(timezone.utc)
RECENT = NOW - timedelta(hours=6)
OLD = NOW - timedelta(days=90)


class TestHonestCounting:
    def test_an_unscored_row_is_not_a_confident_miss(self, arena):
        """p == None means the tracker could not score the cell.

        Counting it as 0 invents a prediction the system never made, and turns a
        model that is failing to load into one that merely looks cautious --
        which is exactly how the NaN-rejecting model hid for months.
        """
        rows = [_row(RECENT, label=True, source="TO.W") for _ in range(50)]
        path, d = arena(rows)
        c = scorecard(path, d, days=30)
        assert c["unscored"] == 50
        assert c["targets"]["rotation"]["n"] == 0, (
            "unscored rows were counted as predictions")

    def test_unlabelled_rows_are_pending_not_negative(self, arena):
        """The last few hours always lack labels; counting them as negatives
        would make every fresh window look like a wall of false alarms."""
        rows = [_row(RECENT, p_rot=0.9) for _ in range(30)]
        path, d = arena(rows)
        c = scorecard(path, d, days=30)
        assert c["awaiting_labels"] == 30
        assert c["targets"]["rotation"]["n"] == 0

    def test_severe_only_rows_are_excluded_from_rotation(self, arena):
        """An SVR-warned cell is ambiguous for the tornado target -- counting it
        as a rotation false positive punishes the model for a cell a forecaster
        did warn on. Training excludes it; scoring must match."""
        rows = [_row(RECENT, label=True, source="SV.W", p_rot=0.9, p_sev=0.9)
                for _ in range(20)]
        path, d = arena(rows)
        c = scorecard(path, d, days=30)
        assert c["targets"]["rotation"]["n"] == 0, "SVR rows leaked into rotation"
        assert c["targets"]["severe"]["n"] == 20, "SVR rows must count for severe"

    def test_the_scoring_rule_matches_the_trainer(self):
        """Drift between _target_truth here and target_label in the trainer
        would report metrics for a different question than was trained."""
        from scripts.train_rotation_model import target_label
        cases = [
            {"label": True, "label_source": "TO.W"},
            {"label": True, "label_source": "SV.W"},
            {"label": False, "label_source": "no_warning_in_area"},
            {"label": None},
        ]
        for rec in cases:
            for t in ("rotation", "severe"):
                assert _target_truth(rec, t) == target_label(rec, t), (
                    f"{rec} disagrees for target={t}")

    def test_rows_outside_the_window_are_ignored(self, arena):
        rows = ([_row(OLD, label=True, source="TO.W", p_rot=0.9)] * 40 +
                [_row(RECENT, label=False, source="no_warning_in_area", p_rot=0.1)] * 10)
        path, d = arena(rows)
        c = scorecard(path, d, days=7)
        assert c["targets"]["rotation"]["n"] == 10


class TestMetrics:
    def test_precision_recall_and_lift(self, arena):
        thr = DEFAULT_OP["rotation"]
        hi, lo = thr + 0.2, thr - 0.2
        rows = (
            [_row(RECENT, label=True, source="TO.W", p_rot=hi)] * 3 +    # tp
            [_row(RECENT, label=False, source="no_warning_in_area", p_rot=hi)] * 1 +  # fp
            [_row(RECENT, label=True, source="TO.W", p_rot=lo)] * 1 +    # fn
            [_row(RECENT, label=False, source="no_warning_in_area", p_rot=lo)] * 15   # tn
        )
        path, d = arena(rows)
        r = scorecard(path, d, days=30)["targets"]["rotation"]
        assert (r["tp"], r["fp"], r["fn"], r["tn"]) == (3, 1, 1, 15)
        assert r["precision"] == pytest.approx(0.75)
        assert r["recall"] == pytest.approx(0.75)
        assert r["base_rate"] == pytest.approx(4 / 20)
        assert r["lift"] == pytest.approx(0.75 / 0.2)

    def test_the_live_threshold_comes_from_the_model_not_a_constant(self, arena, tmp_path):
        """A retrain that shifts the operating point must not leave the
        scorecard measuring the old one."""
        rows = [_row(RECENT, label=True, source="TO.W", p_rot=0.30)] * 5
        path, d = arena(rows)
        (d / "rotation_model.metrics.json").write_text(
            json.dumps({"holdout": {"op_threshold": 0.25}}), encoding="utf-8")
        r = scorecard(path, d, days=30)["targets"]["rotation"]
        assert r["threshold"] == pytest.approx(0.25)
        assert r["tp"] == 5, "should be caught at the model's real threshold"

    def test_a_nonsense_sidecar_falls_back_rather_than_crashing(self, arena):
        rows = [_row(RECENT, label=True, source="TO.W", p_rot=0.9)] * 3
        path, d = arena(rows)
        (d / "rotation_model.metrics.json").write_text("{not json", encoding="utf-8")
        r = scorecard(path, d, days=30)["targets"]["rotation"]
        assert r["threshold"] == pytest.approx(DEFAULT_OP["rotation"])


class TestVerdict:
    def test_it_says_so_when_alerts_are_no_better_than_chance(self, arena):
        """A lift near 1 must read as worthless, not as a percentage that
        sounds vaguely encouraging."""
        thr = DEFAULT_OP["rotation"]
        rows = ([_row(RECENT, label=True, source="TO.W", p_rot=thr + 0.1)] * 1 +
                [_row(RECENT, label=False, source="no_warning_in_area", p_rot=thr + 0.1)] * 9 +
                [_row(RECENT, label=True, source="TO.W", p_rot=thr - 0.1)] * 9 +
                [_row(RECENT, label=False, source="no_warning_in_area", p_rot=thr - 0.1)] * 81)
        path, d = arena(rows)
        v = verdict(scorecard(path, d, days=30))
        assert "no better than chance" in v, v

    def test_it_reports_nothing_rather_than_guessing_on_an_empty_window(self, arena):
        path, d = arena([])
        assert "Not enough" in verdict(scorecard(path, d, days=30))

    def test_a_missing_archive_is_reported_not_raised(self, tmp_path):
        c = scorecard(tmp_path / "nope.jsonl", tmp_path, days=7)
        assert "error" in c
        assert verdict(c)
