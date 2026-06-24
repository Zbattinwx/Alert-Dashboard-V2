"""
Tests for per-segment classification of Watch County Notifications (WCN).

A WCN continues an SPC watch for some counties (CON) while clearing it for
others (CAN) — each in its own $$-delimited segment. The whole-product UGC
parse unions every county, so AlertParser._compute_segment_areas re-classifies
per segment so cleared counties are tracked separately.
"""

from backend.parsers.alert_parser import AlertParser


def _wcn(con_ugc_line: str, can_ugc_line: str) -> str:
    """Build a minimal two-segment WCN: one CON segment, one CAN segment."""
    return (
        "WWUS53 KILN 152030\n"
        "WCNILN\n\n"
        f"{con_ugc_line}\n"
        "/O.CON.KILN.SV.A.0150.000000T0000Z-260115T2230Z/\n"
        "Severe Thunderstorm Watch 150 remains in effect.\n"
        "$$\n\n"
        f"{can_ugc_line}\n"
        "/O.CAN.KILN.SV.A.0150.000000T0000Z-260115T2230Z/\n"
        "Severe Thunderstorm Watch 150 has been cancelled.\n"
        "$$\n"
    )


class TestWatchCountyCancel:
    def test_partial_cancel_classifies_segments(self):
        """CON counties are active; CAN counties are cancelled."""
        text = _wcn("OHC025-027-152230-", "OHC061-152030-")
        active, cancelled = AlertParser._compute_segment_areas(text, 150)
        assert active == {"OHC025", "OHC027"}
        assert cancelled == {"OHC061"}

    def test_continued_county_is_not_marked_cancelled(self):
        """A county both continued and cancelled stays active (continue wins)."""
        text = _wcn("OHC025-152230-", "OHC025-027-152030-")
        active, cancelled = AlertParser._compute_segment_areas(text, 150)
        assert "OHC025" in active
        assert cancelled == {"OHC027"}

    def test_other_etn_segments_ignored(self):
        """Segments for a different ETN are not mixed in."""
        text = _wcn("OHC025-152230-", "OHC061-152030-").replace(
            "SV.A.0150.000000T0000Z-260115T2230Z/\n"
            "Severe Thunderstorm Watch 150 has been cancelled.",
            "SV.A.0151.000000T0000Z-260115T2230Z/\n"
            "Severe Thunderstorm Watch 151 has been cancelled.",
        )
        active, cancelled = AlertParser._compute_segment_areas(text, 150)
        assert active == {"OHC025"}
        assert cancelled == set()  # the CAN segment belongs to ETN 151

    def test_single_segment_no_cancellation(self):
        """A plain single-segment continuation yields no cancelled set."""
        text = (
            "WWUS53 KILN 152030\nWCNILN\n\n"
            "OHC025-027-152230-\n"
            "/O.CON.KILN.SV.A.0150.000000T0000Z-260115T2230Z/\n"
            "Severe Thunderstorm Watch 150 remains in effect.\n$$\n"
        )
        active, cancelled = AlertParser._compute_segment_areas(text, 150)
        assert active == {"OHC025", "OHC027"}
        assert cancelled == set()
