"""
Tests for AlertManager ingestion lifecycle:
- change detection (don't re-broadcast on an identical API re-poll)
- API replace vs NWWS union of affected_areas
- reconciliation of API alerts that dropped out of the active feed
- the no-expiration purge backstop
"""

from datetime import datetime, timezone, timedelta

from backend.models.alert import Alert, AlertStatus, AlertSignificance
from backend.services.alert_manager import AlertManager


def _alert(pid="SV.ILN.0001", source="api", areas=("OHC025",),
           sig=AlertSignificance.WARNING, status=AlertStatus.ACTIVE, **kw):
    return Alert(
        product_id=pid, phenomenon="SV", significance=sig, status=status,
        source=source, affected_areas=list(areas), **kw,
    )


class TestChangeDetection:
    def test_identical_readd_does_not_notify_update(self):
        mgr = AlertManager()
        updates = []
        mgr.on_alert_updated(lambda a: updates.append(a.product_id))
        mgr.add_alert(_alert())
        mgr.add_alert(_alert())  # identical API re-poll
        assert updates == []

    def test_real_change_notifies_update(self):
        mgr = AlertManager()
        updates = []
        mgr.on_alert_updated(lambda a: updates.append(a.product_id))
        mgr.add_alert(_alert())
        mgr.add_alert(_alert(expiration_time=datetime(2030, 1, 1, tzinfo=timezone.utc)))
        assert updates == ["SV.ILN.0001"]


class TestAreaMergeSemantics:
    def test_api_update_replaces_areas(self):
        mgr = AlertManager()
        mgr.add_alert(_alert(pid="SVA.0150", sig=AlertSignificance.WATCH,
                             source="api", areas=("OHC025", "OHC027", "OHC061")))
        # API re-poll shows fewer counties (watch shrank) → replace
        mgr.add_alert(_alert(pid="SVA.0150", sig=AlertSignificance.WATCH,
                             source="api", areas=("OHC025", "OHC027")))
        assert mgr.get_alert("SVA.0150").affected_areas == ["OHC025", "OHC027"]

    def test_nwws_update_unions_areas(self):
        mgr = AlertManager()
        mgr.add_alert(_alert(pid="SVA.0150", sig=AlertSignificance.WATCH,
                             source="nwws", areas=("OHC025",)))
        # NWWS issues a second product for a different area → union
        mgr.add_alert(_alert(pid="SVA.0150", sig=AlertSignificance.WATCH,
                             source="nwws", areas=("OHC027",)))
        assert mgr.get_alert("SVA.0150").affected_areas == ["OHC025", "OHC027"]


class TestReconciliation:
    def test_removes_api_alert_after_threshold(self):
        mgr = AlertManager()
        mgr.add_alert(_alert(pid="SV.ILN.0001", source="api"))
        feed = {"SV.ILN.9999"}  # non-empty feed not containing our alert
        assert mgr.reconcile_api_alerts(feed) == 0          # miss 1 — kept
        assert mgr.get_alert("SV.ILN.0001") is not None
        assert mgr.reconcile_api_alerts(feed) == 1          # miss 2 — removed
        assert mgr.get_alert("SV.ILN.0001") is None

    def test_keeps_present_and_nwws_alerts(self):
        mgr = AlertManager()
        mgr.add_alert(_alert(pid="SV.ILN.0001", source="api"))
        mgr.add_alert(_alert(pid="SV.ILN.0002", source="nwws"))
        feed = {"SV.ILN.0001"}  # 0001 present; 0002 is NWWS-sourced
        mgr.reconcile_api_alerts(feed)
        mgr.reconcile_api_alerts(feed)
        assert mgr.get_alert("SV.ILN.0001") is not None     # present in feed
        assert mgr.get_alert("SV.ILN.0002") is not None     # NWWS never reaped

    def test_reappearing_alert_resets_miss_count(self):
        mgr = AlertManager()
        mgr.add_alert(_alert(pid="SV.ILN.0001", source="api"))
        mgr.reconcile_api_alerts({"SV.ILN.9999"})           # miss 1
        mgr.reconcile_api_alerts({"SV.ILN.0001"})           # back in feed → reset
        assert mgr.reconcile_api_alerts({"SV.ILN.9999"}) == 0  # miss 1 again, not removed
        assert mgr.get_alert("SV.ILN.0001") is not None


class TestNoExpiryBackstop:
    def test_purges_old_no_expiry_alert(self):
        mgr = AlertManager()
        a = _alert(pid="SV.ILN.0003", expiration_time=None)
        a.issued_time = datetime.now(timezone.utc) - timedelta(hours=30)
        mgr.add_alert(a)
        assert mgr.cleanup_expired() == 1
        assert mgr.get_alert("SV.ILN.0003") is None

    def test_keeps_recent_no_expiry_alert(self):
        mgr = AlertManager()
        a = _alert(pid="SV.ILN.0004", expiration_time=None)
        a.issued_time = datetime.now(timezone.utc) - timedelta(hours=1)
        mgr.add_alert(a)
        assert mgr.cleanup_expired() == 0
        assert mgr.get_alert("SV.ILN.0004") is not None
