"""
Tests for VTEC parser.
"""

import pytest
from datetime import datetime, timezone

from backend.parsers.vtec_parser import VTECParser, VTECData
from backend.models.alert import VTECAction, AlertSignificance


class TestVTECParser:
    """Tests for VTECParser class."""

    def test_parse_valid_tornado_warning(self):
        """Test parsing a valid tornado warning VTEC."""
        text = "/O.NEW.KCLE.TO.W.0001.250120T1530Z-250120T1630Z/"

        result = VTECParser.parse(text)

        assert result.is_valid
        assert result.vtec_info is not None
        assert result.vtec_info.product_class == "O"
        assert result.vtec_info.action == VTECAction.NEW
        assert result.vtec_info.office == "KCLE"
        assert result.vtec_info.phenomenon == "TO"
        assert result.vtec_info.significance == AlertSignificance.WARNING
        assert result.vtec_info.event_tracking_number == 1
        assert result.vtec_info.begin_time is not None
        assert result.vtec_info.end_time is not None

    def test_parse_severe_thunderstorm_watch(self):
        """Test parsing a severe thunderstorm watch VTEC."""
        text = "/O.NEW.KWNS.SV.A.0150.250120T1800Z-250121T0000Z/"

        result = VTECParser.parse(text)

        assert result.is_valid
        assert result.vtec_info.phenomenon == "SV"
        assert result.vtec_info.significance == AlertSignificance.WATCH
        assert result.vtec_info.event_tracking_number == 150

    def test_parse_continuation_action(self):
        """Test parsing a CON (continuation) VTEC."""
        text = "/O.CON.KILN.WS.W.0005.250120T1200Z-250121T1200Z/"

        result = VTECParser.parse(text)

        assert result.is_valid
        assert result.vtec_info.action == VTECAction.CON
        assert result.vtec_info.office == "KILN"

    def test_parse_cancellation(self):
        """Test parsing a CAN (cancellation) VTEC."""
        text = "/O.CAN.KPBZ.TO.W.0003.000000T0000Z-250120T1630Z/"

        result = VTECParser.parse(text)

        assert result.is_valid
        assert result.vtec_info.action == VTECAction.CAN
        # Begin time should be None for 000000T0000Z
        assert result.vtec_info.begin_time is None

    def test_parse_undefined_begin_time(self):
        """Test that 000000T0000Z is parsed as undefined (None)."""
        text = "/O.NEW.KCLE.TO.W.0001.000000T0000Z-250120T1630Z/"

        result = VTECParser.parse(text)

        assert result.is_valid
        assert result.vtec_info.begin_time is None
        assert result.vtec_info.end_time is not None

    def test_parse_no_vtec_found(self):
        """Test parsing text with no VTEC string."""
        text = "This is just some random text without any VTEC."

        result = VTECParser.parse(text)

        assert not result.is_valid
        assert len(result.validation_errors) > 0
        assert "No VTEC" in result.validation_errors[0]

    def test_parse_invalid_action_code(self):
        """Test parsing VTEC with invalid action code."""
        text = "/O.XXX.KCLE.TO.W.0001.250120T1530Z-250120T1630Z/"

        result = VTECParser.parse(text)

        assert not result.is_valid
        assert any("action" in err.lower() for err in result.validation_errors)

    def test_parse_unknown_phenomenon_warns(self):
        """Test that unknown phenomenon code generates warning but still parses."""
        # Using a made-up phenomenon code
        text = "/O.NEW.KCLE.XX.W.0001.250120T1530Z-250120T1630Z/"

        result = VTECParser.parse(text)

        assert result.is_valid
        assert len(result.validation_warnings) > 0
        assert any("unknown" in w.lower() or "phenomenon" in w.lower()
                   for w in result.validation_warnings)

    def test_parse_test_product_class(self):
        """Test parsing a test (T) product class."""
        text = "/T.NEW.KCLE.TO.W.0001.250120T1530Z-250120T1630Z/"

        result = VTECParser.parse(text)

        assert result.is_valid
        assert result.vtec_info.product_class == "T"

    def test_build_product_id_warning(self):
        """Test building product ID for a warning."""
        text = "/O.NEW.KCLE.TO.W.0001.250120T1530Z-250120T1630Z/"
        result = VTECParser.parse(text)

        product_id = VTECParser.build_product_id(result.vtec_info)

        assert product_id == "TO.CLE.0001"

    def test_build_product_id_watch(self):
        """Test building product ID for a watch (includes 'A' suffix)."""
        text = "/O.NEW.KWNS.TO.A.0150.250120T1800Z-250121T0000Z/"
        result = VTECParser.parse(text)

        product_id = VTECParser.build_product_id(result.vtec_info)

        assert product_id == "TOA.WNS.0150"

    def test_is_cancellation(self):
        """Test cancellation detection."""
        can_text = "/O.CAN.KCLE.TO.W.0001.000000T0000Z-250120T1630Z/"
        exp_text = "/O.EXP.KCLE.TO.W.0001.000000T0000Z-250120T1630Z/"
        new_text = "/O.NEW.KCLE.TO.W.0001.250120T1530Z-250120T1630Z/"

        can_result = VTECParser.parse(can_text)
        exp_result = VTECParser.parse(exp_text)
        new_result = VTECParser.parse(new_text)

        assert VTECParser.is_cancellation(can_result.vtec_info)
        assert VTECParser.is_cancellation(exp_result.vtec_info)
        assert not VTECParser.is_cancellation(new_result.vtec_info)

    def test_is_continuation(self):
        """Test continuation detection."""
        con_text = "/O.CON.KCLE.TO.W.0001.250120T1530Z-250120T1630Z/"
        ext_text = "/O.EXT.KCLE.TO.W.0001.250120T1530Z-250120T1830Z/"
        new_text = "/O.NEW.KCLE.TO.W.0001.250120T1530Z-250120T1630Z/"

        con_result = VTECParser.parse(con_text)
        ext_result = VTECParser.parse(ext_text)
        new_result = VTECParser.parse(new_text)

        assert VTECParser.is_continuation(con_result.vtec_info)
        assert VTECParser.is_continuation(ext_result.vtec_info)
        assert not VTECParser.is_continuation(new_result.vtec_info)

    def test_parse_all_multiple_vtec(self):
        """Test parsing text with multiple VTEC strings."""
        text = """
        /O.NEW.KCLE.TO.W.0001.250120T1530Z-250120T1630Z/
        /O.CON.KILN.SV.W.0005.250120T1500Z-250120T1600Z/
        """

        results = VTECParser.parse_all(text)

        assert len(results) == 2
        assert all(r.is_valid for r in results)
        assert results[0].vtec_info.office == "KCLE"
        assert results[1].vtec_info.office == "KILN"

    def test_vtec_in_longer_text(self):
        """Test finding VTEC embedded in longer alert text."""
        text = """
        WWUS53 KCLE 201530
        TORCLE

        OHC049-041-201630-
        /O.NEW.KCLE.TO.W.0001.250120T1530Z-250120T1630Z/

        BULLETIN - EAS ACTIVATION REQUESTED
        TORNADO WARNING
        """

        result = VTECParser.parse(text)

        assert result.is_valid
        assert result.vtec_info.phenomenon == "TO"
        assert result.vtec_info.office == "KCLE"


class TestVTECTimestampParsing:
    """Tests for VTEC timestamp parsing edge cases."""

    def test_parse_valid_timestamp(self):
        """Test parsing a valid VTEC timestamp."""
        from backend.utils.timezone import TimezoneHelper

        result = TimezoneHelper.parse_vtec_timestamp("250120T1530Z")

        assert result is not None
        assert result.year == 2025
        assert result.month == 1
        assert result.day == 20
        assert result.hour == 15
        assert result.minute == 30
        assert result.tzinfo == timezone.utc

    def test_parse_undefined_timestamp(self):
        """Test that 000000T0000Z returns None."""
        from backend.utils.timezone import TimezoneHelper

        result = TimezoneHelper.parse_vtec_timestamp("000000T0000Z")

        assert result is None

    def test_parse_timestamp_without_z(self):
        """Test parsing timestamp without trailing Z."""
        from backend.utils.timezone import TimezoneHelper

        result = TimezoneHelper.parse_vtec_timestamp("250120T1530")

        assert result is not None
        assert result.hour == 15

    def test_parse_invalid_timestamp_format(self):
        """Test parsing invalid timestamp format."""
        from backend.utils.timezone import TimezoneHelper

        result = TimezoneHelper.parse_vtec_timestamp("invalid")

        assert result is None

    def test_parse_timestamp_invalid_month(self):
        """Test parsing timestamp with invalid month."""
        from backend.utils.timezone import TimezoneHelper

        result = TimezoneHelper.parse_vtec_timestamp("251320T1530Z")  # Month 13

        assert result is None
