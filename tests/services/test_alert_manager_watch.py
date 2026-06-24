"""
Tests for AlertManager watch-county lifecycle.

A Watch County Notification (WCN) can clear some counties from a watch while it
continues for others. AlertManager must subtract the cleared counties from the
stored watch rather than (a) keeping them filled forever via the union merge, or
(b) removing the entire watch on a subset cancellation. Warnings are
polygon-based and keep whole-alert cancellation behaviour.
"""

from backend.models.alert import Alert, AlertStatus, AlertSignificance
from backend.services.alert_manager import AlertManager


def _watch(areas, status=AlertStatus.ACTIVE, cancelled=None):
    return Alert(
        product_id="SVA.0150",
        phenomenon="SV",
        significance=AlertSignificance.WATCH,
        status=status,
        affected_areas=list(areas),
        cancelled_areas=list(cancelled or []),
    )


class TestWatchCountyLifecycle:
    def test_merge_subtracts_cleared_counties(self):
        """An active WCN (CON) carrying cancelled_areas drops those counties."""
        mgr = AlertManager()
        mgr.add_alert(_watch(["OHC025", "OHC027", "OHC061"]))
        # Continues 025/027, clears 061 in the same product.
        mgr.add_alert(_watch(["OHC025", "OHC027"], cancelled=["OHC061"]))
        stored = mgr.get_alert("SVA.0150")
        assert stored is not None
        assert stored.affected_areas == ["OHC025", "OHC027"]

    def test_subset_cancel_keeps_watch_alive(self):
        """A CANCELLED product clearing a subset keeps the watch for the rest."""
        mgr = AlertManager()
        mgr.add_alert(_watch(["OHC025", "OHC027", "OHC061"]))
        mgr.add_alert(_watch(["OHC061"], status=AlertStatus.CANCELLED, cancelled=["OHC061"]))
        stored = mgr.get_alert("SVA.0150")
        assert stored is not None
        assert stored.affected_areas == ["OHC025", "OHC027"]

    def test_clearing_last_counties_removes_watch(self):
        """When the final counties are cleared, the watch is removed."""
        mgr = AlertManager()
        mgr.add_alert(_watch(["OHC061"]))
        mgr.add_alert(_watch(["OHC061"], status=AlertStatus.CANCELLED, cancelled=["OHC061"]))
        assert mgr.get_alert("SVA.0150") is None

    def test_warning_cancellation_removes_whole_alert(self):
        """Warnings are unaffected — a CAN removes the whole warning."""
        mgr = AlertManager()
        warning = Alert(
            product_id="SV.ILN.0001",
            phenomenon="SV",
            significance=AlertSignificance.WARNING,
            status=AlertStatus.ACTIVE,
            affected_areas=["OHC025"],
        )
        mgr.add_alert(warning)
        cancel = Alert(
            product_id="SV.ILN.0001",
            phenomenon="SV",
            significance=AlertSignificance.WARNING,
            status=AlertStatus.CANCELLED,
            affected_areas=["OHC025"],
            cancelled_areas=["OHC025"],
        )
        mgr.add_alert(cancel)
        assert mgr.get_alert("SV.ILN.0001") is None
