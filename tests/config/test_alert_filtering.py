"""
Coverage-area filtering: which alerts this deployment accepts.

This logic had NO direct tests. It was exercised only incidentally, through
alert-parser tests that happened to run under whatever settings the machine's
operator had configured -- so it "passed" for reasons unrelated to the filter
being correct, and changing a coverage county in the dashboard UI could turn the
build red. tests/conftest.py now pins permissive settings for the parser tests,
which would have left this entirely uncovered.

The asymmetry worth remembering: a filter that is too tight drops a warning the
operator needed, which is silent and on air.
"""

import pytest

from backend.parsers.alert_parser import AlertParser


@pytest.fixture
def cfg(monkeypatch):
    """Set filter settings explicitly for one test."""
    from backend.config import settings as mod

    def _apply(**kw):
        s = mod.get_settings()
        for k, v in kw.items():
            monkeypatch.setattr(s, k, v, raising=False)
        return s
    return _apply


class TestStateFilter:
    def test_no_filter_accepts_everything(self, cfg):
        cfg(filter_states=[])
        assert AlertParser._is_target_state(["INC001"]) is True
        assert AlertParser._is_target_state([]) is True

    def test_a_matching_state_is_accepted(self, cfg):
        cfg(filter_states=["OH"])
        assert AlertParser._is_target_state(["OHC049"]) is True

    def test_a_non_matching_state_is_rejected(self, cfg):
        cfg(filter_states=["OH"])
        assert AlertParser._is_target_state(["INC001"]) is False

    def test_one_matching_county_is_enough(self, cfg):
        """A multi-state warning that clips the coverage area must be kept."""
        cfg(filter_states=["OH"])
        assert AlertParser._is_target_state(["INC001", "OHC049", "KYC015"]) is True

    def test_matching_is_case_insensitive(self, cfg):
        cfg(filter_states=["oh"])
        assert AlertParser._is_target_state(["OHC049"]) is True

    def test_empty_areas_are_rejected_when_a_filter_is_set(self, cfg):
        """Can't attribute it to a state, so it is not shown.

        Note the deliberate exception in parse_text_alert: a VTEC CANCELLATION
        with no areas is still processed, because dropping it leaves a cancelled
        warning on screen. See TestAreaLessCancellation.
        """
        cfg(filter_states=["OH"])
        assert AlertParser._is_target_state([]) is False

    def test_a_malformed_ugc_does_not_crash_the_filter(self, cfg):
        cfg(filter_states=["OH"])
        for bad in ([""], ["O"], ["!!"], ["OHC049", ""]):
            assert AlertParser._is_target_state(bad) in (True, False)


class TestAreaLessCancellation:
    """A cancellation carrying no UGC must still reach the alert manager.

    alert_manager matches it by product_id and clears the whole alert when
    cancelled_areas is empty; it never needed the UGC. Dropping it in the parser
    left the warning on screen until it expired by itself.
    """

    CAN = """
    /O.CAN.KCLE.TO.W.0001.000000T0000Z-250120T1630Z/

    THE TORNADO WARNING FOR FRANKLIN COUNTY HAS BEEN CANCELLED.
    """

    def test_it_survives_parsing(self):
        from backend.models.alert import AlertStatus

        alert = AlertParser.parse_text_alert(self.CAN)
        assert alert is not None, (
            "an area-less cancellation was dropped -- the warning it cancels "
            "would stay on screen until it expired")
        assert alert.status == AlertStatus.CANCELLED
        assert alert.vtec.action.value == "CAN"
        assert alert.product_id, "the manager matches cancellations by product_id"

    def test_it_survives_even_with_a_state_filter_set(self, cfg):
        """The exception must not depend on the coverage configuration."""
        cfg(filter_states=["OH"])
        assert AlertParser.parse_text_alert(self.CAN) is not None

    def test_an_ordinary_arealess_alert_is_still_rejected(self):
        """The exception is for cancellations only -- a NEW warning with no
        areas is not actionable and must not leak through."""
        new = """
        /O.NEW.KCLE.TO.W.0002.250120T1600Z-250120T1630Z/

        A TORNADO WARNING HAS BEEN ISSUED.
        """
        assert AlertParser.parse_text_alert(new) is None
