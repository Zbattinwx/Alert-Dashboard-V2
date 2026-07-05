"""
Tests for AlertManager partial-cancellation lifecycle (watches AND warnings).

A follow-up product can clear some areas from an event while it continues for
others under the same ETN: a Watch County Notification (WCN) clearing a watch's
counties, or a warning follow-up statement (SVS) whose storm has left one county
while it continues for the rest (CAN one county, CON the others). AlertManager
must subtract the cleared areas from the stored alert rather than (a) keeping
them filled forever via the union merge, or (b) removing the entire alert on a
subset cancellation. This applies to warnings too — the polygon shrinks and the
county list must shrink with it (otherwise widgets keep showing a county that is
no longer under the warning). A cancellation that clears EVERY area still removes
the whole alert.
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
        """A CAN clearing every county still removes the whole warning."""
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


def _warning(areas, status=AlertStatus.ACTIVE, cancelled=None, polygon=None):
    return Alert(
        product_id="TO.ILN.0012",
        phenomenon="TO",
        significance=AlertSignificance.WARNING,
        status=status,
        source="nwws",
        affected_areas=list(areas),
        cancelled_areas=list(cancelled or []),
        polygon=list(polygon or []),
    )


class TestWarningPartialCancel:
    """A tornado/severe warning whose follow-up SVS clears one county while
    continuing for the rest must drop that county from the alert — not keep it
    filled via the NWWS union merge (the reported bug: two counties stayed in a
    tornado warning after the storm passed one and the NWS cleared it)."""

    def test_con_merge_subtracts_cleared_county(self):
        """An active follow-up (CAN one county / CON the rest, parsed as active
        with cancelled_areas) drops the cleared county from the warning."""
        mgr = AlertManager()
        mgr.add_alert(_warning(["OHC025", "OHC027"], polygon=[[40.0, -83.0], [40.1, -83.0], [40.1, -82.9], [40.0, -83.0]]))
        # SVS: storm has left 027; CON 025 (active update carrying the cleared one).
        shrunk = [[40.0, -83.0], [40.05, -83.0], [40.05, -82.95], [40.0, -83.0]]
        mgr.add_alert(_warning(["OHC025"], cancelled=["OHC027"], polygon=shrunk))
        stored = mgr.get_alert("TO.ILN.0012")
        assert stored is not None
        assert stored.affected_areas == ["OHC025"]      # 027 dropped, not unioned back
        assert stored.polygon == shrunk                  # map shrinks with the county list

    def test_subset_cancel_status_keeps_remaining_county(self):
        """A CANCELLED-status follow-up clearing a subset keeps the warning for
        the rest (and adopts its shrunk polygon), rather than removing it whole."""
        mgr = AlertManager()
        mgr.add_alert(_warning(["OHC025", "OHC027"]))
        shrunk = [[40.0, -83.0], [40.05, -83.0], [40.05, -82.95], [40.0, -83.0]]
        mgr.add_alert(_warning(["OHC027"], status=AlertStatus.CANCELLED,
                               cancelled=["OHC027"], polygon=shrunk))
        stored = mgr.get_alert("TO.ILN.0012")
        assert stored is not None
        assert stored.affected_areas == ["OHC025"]
        assert stored.polygon == shrunk

    def test_clearing_all_counties_removes_warning(self):
        """Clearing every county still removes the whole warning."""
        mgr = AlertManager()
        mgr.add_alert(_warning(["OHC025", "OHC027"]))
        mgr.add_alert(_warning(["OHC025", "OHC027"], status=AlertStatus.CANCELLED,
                               cancelled=["OHC025", "OHC027"]))
        assert mgr.get_alert("TO.ILN.0012") is None

    def test_partial_cancel_notifies_update(self):
        """The county drop must fire an update so widgets re-render."""
        mgr = AlertManager()
        updates = []
        mgr.on_alert_updated(lambda a: updates.append(a.affected_areas[:]))
        mgr.add_alert(_warning(["OHC025", "OHC027"]))
        mgr.add_alert(_warning(["OHC025"], cancelled=["OHC027"]))
        assert updates and updates[-1] == ["OHC025"]
