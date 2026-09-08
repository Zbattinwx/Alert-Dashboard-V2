"""
Two targets, one archive: `rotation` (tornado-warned) and `severe` (warned at all).

A storm is normally severe-thunderstorm-warned as it organises and
tornado-warned later if it keeps intensifying, so these are two stages of the
same storm. That makes SV.W rows genuinely different for each question, and the
difference is the thing worth guarding:

  * for `severe`, an SVR-warned cell is a POSITIVE -- the question is whether
    the cell warrants a warning at all;
  * for `rotation`, an SVR-warned cell is EXCLUDED, not negative. A rotating
    storm a forecaster warned on is not a non-event, and calling it one teaches
    the model that rotation without a tornado warning means no rotation.

The exclusion is the fragile part: `None` and `0` are both falsy, so a refactor
that collapses them would quietly convert thousands of ambiguous rows into
negatives and depress precision with nothing in the logs to show for it.

    python -m pytest tests/scripts/test_prediction_targets.py -v
"""

import pytest

from scripts.train_rotation_model import TARGETS, target_label


def rec(label, source):
    return {"label": label, "label_source": source}


TO_W    = rec(True,  "TO.W")
SV_W    = rec(True,  "SV.W")
CLEAR   = rec(False, "no_warning_in_area")
UNLABEL = {"label": None}


class TestRotationTarget:
    def test_a_tornado_warning_is_positive(self):
        assert target_label(TO_W, "rotation") == 1

    def test_an_svr_only_cell_is_excluded_not_negative(self):
        got = target_label(SV_W, "rotation")
        assert got is None, (
            f"got {got!r} -- an SVR-warned cell counted as a negative teaches "
            "the model that a storm a forecaster warned on is a non-event")

    def test_exclusion_is_distinguishable_from_negative(self):
        """None and 0 are both falsy; the caller must not be able to confuse them."""
        assert target_label(SV_W, "rotation") is None
        assert target_label(CLEAR, "rotation") == 0
        assert target_label(SV_W, "rotation") is not target_label(CLEAR, "rotation")

    def test_a_clear_cell_is_negative(self):
        assert target_label(CLEAR, "rotation") == 0

    def test_unlabelled_is_excluded(self):
        assert target_label(UNLABEL, "rotation") is None


class TestSevereTarget:
    def test_an_svr_warning_is_positive(self):
        assert target_label(SV_W, "severe") == 1

    def test_a_tornado_warning_is_also_positive(self):
        """A tornado-warned storm is severe by construction."""
        assert target_label(TO_W, "severe") == 1

    def test_a_clear_cell_is_negative(self):
        assert target_label(CLEAR, "severe") == 0

    def test_nothing_warned_is_ever_excluded(self):
        """`severe` asks about any warning, so no labelled row is ambiguous."""
        for r in (TO_W, SV_W, CLEAR):
            assert target_label(r, "severe") is not None

    def test_unlabelled_is_still_excluded(self):
        assert target_label(UNLABEL, "severe") is None


class TestBackwardCompatibility:
    def test_rotation_matches_strict_labelling_exactly(self):
        """The archive is currently labelled --strict-tornado, which leaves no
        SV.W rows at all. On that data the new target logic must reduce to the
        old `1 if rec['label'] else 0`, or every previously-reported number
        silently changes meaning."""
        strict_rows = [TO_W, CLEAR, CLEAR, TO_W, CLEAR]
        old = [1 if r["label"] else 0 for r in strict_rows]
        new = [target_label(r, "rotation") for r in strict_rows]
        assert new == old

    @pytest.mark.parametrize("target", TARGETS)
    def test_every_target_handles_every_label_source(self, target):
        for r in (TO_W, SV_W, CLEAR, UNLABEL):
            assert target_label(r, target) in (0, 1, None)

    def test_a_missing_label_source_does_not_crash(self):
        """Older rows predate label_source; a positive without one is ambiguous
        for rotation rather than an exception."""
        assert target_label({"label": True}, "rotation") is None
        assert target_label({"label": True}, "severe") == 1
        assert target_label({"label": False}, "rotation") == 0
