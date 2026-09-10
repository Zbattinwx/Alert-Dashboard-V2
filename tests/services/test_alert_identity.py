"""Tests for the update-vs-new-alert distinction.

The dashboard was announcing follow-up products as brand new alerts -- toast,
chime, Google Chat, regenerated broadcast graphic -- for warnings that had
already been running. The audit log showed 24% of all ADD events carrying a
CON/EXT/EXA/EXB action, and 79 of 467 distinct product_ids added more than once.

Each test here pins one of the mechanisms behind that.
"""

from datetime import datetime, timezone

import pytest

from backend.models.alert import (
    Alert, AlertSignificance, AlertStatus, VTECAction, VTECInfo,
)
from backend.parsers.alert_parser import AlertParser
from backend.parsers.vtec_parser import VTECParser
from backend.services.alert_manager import AlertManager


def _vtec(action=VTECAction.NEW, phenomenon="HT", sig=AlertSignificance.ADVISORY,
          office="KCLE", etn=3):
    return VTECInfo(action=action, office=office, phenomenon=phenomenon,
                    significance=sig, event_tracking_number=etn)


def _alert(pid="SV.W.ILN.0001", source="api", areas=("OHC025",), vtec=None,
           status=AlertStatus.ACTIVE, sig=AlertSignificance.WARNING, **kw):
    return Alert(
        product_id=pid, phenomenon="SV", significance=sig, status=status,
        source=source, affected_areas=list(areas), vtec=vtec, **kw,
    )


class TestIsNewIssuance:
    """The flag that gates the toast, the chime and the Chat notification."""

    def test_new_action_is_a_new_issuance(self):
        assert _alert(vtec=_vtec(VTECAction.NEW)).is_new_issuance

    @pytest.mark.parametrize("action", [
        VTECAction.CON, VTECAction.EXT, VTECAction.EXA,
        VTECAction.EXB, VTECAction.COR,
    ])
    def test_follow_up_actions_are_not(self, action):
        assert not _alert(vtec=_vtec(action)).is_new_issuance

    def test_no_vtec_counts_as_new(self):
        """SPS and friends have no follow-up concept of their own."""
        assert _alert(vtec=None).is_new_issuance

    def test_first_sighting_of_a_con_still_enters_the_store(self):
        """The alert must still appear -- it is an active warning. Only the
        announcement is suppressed, and that is the caller's job to read off
        is_new_issuance.
        """
        mgr = AlertManager()
        added = []
        mgr.on_alert_added(lambda a: added.append(a))

        mgr.add_alert(_alert(vtec=_vtec(VTECAction.CON)))

        assert mgr.get_alert("SV.W.ILN.0001") is not None
        assert len(added) == 1
        assert added[0].is_new_issuance is False


class TestUpgradeClearsOnlyItsOwnZones:
    """A multi-segment product upgrades some zones and continues others under
    one ETN. The API delivers those as two CAP features sharing a product_id.
    The upgrade used to pop the whole alert and the continuation re-added it in
    the same second, as new.
    """

    def test_partial_upgrade_keeps_the_remaining_zones(self):
        mgr = AlertManager()
        added, removed = [], []
        mgr.on_alert_added(lambda a: added.append(a.product_id))
        mgr.on_alert_removed(lambda a: removed.append(a.product_id))

        mgr.add_alert(_alert(pid="HT.Y.CLE.0003", sig=AlertSignificance.ADVISORY,
                             areas=("OHZ007", "OHZ008", "OHZ012"),
                             vtec=_vtec(VTECAction.NEW)))
        # The UPG segment names only the zones it upgrades away.
        mgr.add_alert(_alert(pid="HT.Y.CLE.0003", sig=AlertSignificance.ADVISORY,
                             areas=("OHZ007", "OHZ008"), status=AlertStatus.CANCELLED,
                             cancelled_areas=["OHZ007", "OHZ008"],
                             vtec=_vtec(VTECAction.UPG)))

        surviving = mgr.get_alert("HT.Y.CLE.0003")
        assert surviving is not None
        assert surviving.affected_areas == ["OHZ012"]
        assert removed == []
        assert added == ["HT.Y.CLE.0003"]  # never re-announced

    def test_upgrade_covering_every_zone_still_removes_the_alert(self):
        mgr = AlertManager()
        removed = []
        mgr.on_alert_removed(lambda a: removed.append(a.product_id))

        mgr.add_alert(_alert(pid="HT.Y.CLE.0003", sig=AlertSignificance.ADVISORY,
                             areas=("OHZ007", "OHZ008"), vtec=_vtec(VTECAction.NEW)))
        mgr.add_alert(_alert(pid="HT.Y.CLE.0003", sig=AlertSignificance.ADVISORY,
                             areas=("OHZ007", "OHZ008"), status=AlertStatus.CANCELLED,
                             cancelled_areas=["OHZ007", "OHZ008"],
                             vtec=_vtec(VTECAction.UPG)))

        assert mgr.get_alert("HT.Y.CLE.0003") is None
        assert removed == ["HT.Y.CLE.0003"]

    def test_upg_is_a_cancellation_in_both_definitions(self):
        """VTECParser.is_cancellation counted UPG and VTECInfo.is_cancellation
        did not -- two answers to one question, in one codebase.
        """
        vtec = _vtec(VTECAction.UPG)
        assert VTECParser.is_cancellation(vtec)
        assert vtec.is_cancellation
        assert not vtec.is_update


class TestApiCancelSegmentCarriesItsZones:
    """The API path never populated cancelled_areas, so every cancelling
    feature looked like a whole-alert cancellation.
    """

    @staticmethod
    def _feature(vtec_str, ugc_zones):
        return {
            "properties": {
                "id": "urn:oid:2.49.0.1.840.0.abc.001.1",
                "event": "Heat Advisory",
                "senderName": "NWS Cleveland OH",
                "description": "...",
                "expires": "2035-01-01T00:00:00+00:00",
                "parameters": {"VTEC": [vtec_str]},
                "geocode": {"UGC": list(ugc_zones)},
            },
            "geometry": None,
        }

    def test_upgrade_feature_reports_its_zones_as_cancelled(self, monkeypatch):
        monkeypatch.setattr(AlertParser, "_is_target_phenomenon",
                            classmethod(lambda cls, p: True))
        monkeypatch.setattr(AlertParser, "_is_target_state",
                            classmethod(lambda cls, a: True))
        monkeypatch.setattr(AlertParser, "_filter_to_target_states",
                            classmethod(lambda cls, a: a))
        monkeypatch.setattr(AlertParser, "_filter_to_target_counties",
                            classmethod(lambda cls, a: a))

        alert = AlertParser.parse_api_alert(
            self._feature("/O.UPG.KCLE.HT.Y.0003.000000T0000Z-350120T1630Z/",
                          ["OHZ007", "OHZ008"]),
            source="api",
        )

        assert alert is not None
        assert alert.status == AlertStatus.CANCELLED
        assert alert.cancelled_areas == ["OHZ007", "OHZ008"]


class TestSpsIdentity:
    """SPS ids embedded the issuance minute, so a statement had no stable
    identity: every follow-up became its own card, and the NWWS and API copies
    of one statement split apart whenever their timestamps disagreed.
    """

    def test_same_zones_give_the_same_id(self):
        zones = ["OHZ012", "OHZ013"]
        assert AlertParser._generate_sps_id(zones) == AlertParser._generate_sps_id(zones)

    def test_id_is_order_independent(self):
        assert (AlertParser._generate_sps_id(["OHZ013", "OHZ012"])
                == AlertParser._generate_sps_id(["OHZ012", "OHZ013"]))

    def test_different_zones_give_different_ids(self):
        assert (AlertParser._generate_sps_id(["OHZ012"])
                != AlertParser._generate_sps_id(["OHZ089"]))

    def test_id_carries_no_timestamp(self):
        sps_id = AlertParser._generate_sps_id(["OHZ012", "OHZ013"])
        assert sps_id.startswith("SPS.adhoc.")
        assert len(sps_id.split(".")) == 3

    def test_no_zones_has_no_id(self):
        assert AlertParser._generate_sps_id([]) is None


class TestCheckpointing:
    """Only a graceful shutdown used to persist the store, so any crash or
    dev reload lost every active warning and the next poll re-announced them.
    """

    @pytest.mark.asyncio
    async def test_mutation_marks_the_store_dirty_and_checkpoints(self, tmp_path):
        path = tmp_path / "active_alerts.json"
        mgr = AlertManager(persistence_path=path)

        assert not mgr._dirty
        mgr.add_alert(_alert(vtec=_vtec(VTECAction.NEW)))
        assert mgr._dirty

        assert await mgr.checkpoint() is True
        assert path.exists()

    @pytest.mark.asyncio
    async def test_unchanged_store_does_not_rewrite(self, tmp_path):
        path = tmp_path / "active_alerts.json"
        mgr = AlertManager(persistence_path=path)
        mgr.add_alert(_alert(vtec=_vtec(VTECAction.NEW)))
        await mgr.checkpoint()

        assert await mgr.checkpoint() is False

    def test_restored_alert_is_rekeyed_to_the_current_format(self, tmp_path):
        """A persisted file written before the key format changed holds the old
        IDs. Left alone, the restored copy matches nothing in the API feed, gets
        reaped by reconcile_api_alerts after two polls, and comes back as a new
        alert -- the exact failure the format change was meant to fix.
        """
        import json

        stale = _alert(pid="HT.CLE.0003", sig=AlertSignificance.ADVISORY,
                       expiration_time=datetime(2035, 1, 1, tzinfo=timezone.utc),
                       vtec=_vtec(VTECAction.CON))
        path = tmp_path / "active_alerts.json"
        path.write_text(json.dumps({
            "saved_at": "2026-01-01T00:00:00+00:00",
            "alert_count": 1,
            "alerts": [stale.to_dict()],
        }), encoding="utf-8")

        mgr = AlertManager(persistence_path=path)
        assert mgr.load_from_file() == 1

        assert mgr.get_alert("HT.CLE.0003") is None
        assert mgr.get_alert("HT.Y.CLE.0003") is not None

    def test_restored_alert_without_vtec_keeps_its_id(self):
        sps = _alert(pid="SPS.adhoc.abcd1234", vtec=None)
        assert AlertManager._recanonicalize_id(sps) is False
        assert sps.product_id == "SPS.adhoc.abcd1234"

    @pytest.mark.asyncio
    async def test_checkpoint_round_trips_through_load(self, tmp_path):
        path = tmp_path / "active_alerts.json"
        mgr = AlertManager(persistence_path=path)
        mgr.add_alert(_alert(expiration_time=datetime(2035, 1, 1, tzinfo=timezone.utc),
                             vtec=_vtec(VTECAction.CON, phenomenon="SV",
                                        sig=AlertSignificance.WARNING,
                                        office="KILN", etn=1)))
        await mgr.checkpoint()

        restored = AlertManager(persistence_path=path)
        assert restored.load_from_file() == 1
        # is_new_issuance is computed, not stored -- from_dict must not choke on it.
        assert restored.get_alert("SV.W.ILN.0001").is_new_issuance is False
