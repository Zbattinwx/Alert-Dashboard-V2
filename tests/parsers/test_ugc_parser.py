"""
Tests for UGC parser.
"""

import pytest
from datetime import datetime, timezone

from backend.parsers.ugc_parser import UGCParser, UGCData


class TestUGCParser:
    """Tests for UGCParser class."""

    def test_parse_simple_ugc_line(self):
        """Test parsing a simple UGC line."""
        text = "OHC049-041-061-201530-"

        result = UGCParser.parse(text)

        assert result.is_valid
        assert "OHC049" in result.ugc_codes
        assert "OHC041" in result.ugc_codes
        assert "OHC061" in result.ugc_codes
        assert len(result.ugc_codes) == 3

    def test_parse_ugc_with_range(self):
        """Test parsing UGC line with range expansion."""
        text = "OHC001>005-201530-"

        result = UGCParser.parse(text)

        assert result.is_valid
        assert len(result.ugc_codes) == 5
        assert "OHC001" in result.ugc_codes
        assert "OHC002" in result.ugc_codes
        assert "OHC003" in result.ugc_codes
        assert "OHC004" in result.ugc_codes
        assert "OHC005" in result.ugc_codes

    def test_parse_ugc_mixed_codes_and_range(self):
        """Test parsing UGC with both individual codes and ranges."""
        text = "OHC049-001>003-061-201530-"

        result = UGCParser.parse(text)

        assert result.is_valid
        # Should have: 049, 001, 002, 003, 061
        assert len(result.ugc_codes) == 5
        assert "OHC049" in result.ugc_codes
        assert "OHC001" in result.ugc_codes
        assert "OHC002" in result.ugc_codes
        assert "OHC003" in result.ugc_codes
        assert "OHC061" in result.ugc_codes

    def test_parse_zone_codes(self):
        """Test parsing zone codes (Z instead of C)."""
        text = "OHZ001-002-003-201530-"

        result = UGCParser.parse(text)

        assert result.is_valid
        assert "OHZ001" in result.ugc_codes
        assert "OHZ002" in result.ugc_codes
        assert all(code[2] == 'Z' for code in result.ugc_codes)

    def test_parse_multiple_states(self):
        """Test parsing UGC codes from multiple states."""
        text = """
        OHC049-041-201530-
        INC001-002-201530-
        """

        result = UGCParser.parse(text)

        assert result.is_valid
        assert "OH" in result.states
        assert "IN" in result.states
        assert any(code.startswith("OH") for code in result.ugc_codes)
        assert any(code.startswith("IN") for code in result.ugc_codes)

    def test_parse_continuation_line(self):
        """Test parsing multi-line UGC with continuation."""
        text = """
        OHC049-041-061-
        081-085-201530-
        """

        result = UGCParser.parse(text)

        assert result.is_valid
        assert "OHC049" in result.ugc_codes
        assert "OHC081" in result.ugc_codes
        assert "OHC085" in result.ugc_codes

    def test_parse_expiration_time(self):
        """Test parsing UGC expiration timestamp."""
        text = "OHC049-201530-"  # 20th day, 15:30 UTC

        result = UGCParser.parse(text)

        assert result.expiration_time is not None
        assert result.expiration_time.day == 20
        assert result.expiration_time.hour == 15
        assert result.expiration_time.minute == 30

    def test_ugc_to_fips_county(self):
        """Test converting county UGC to FIPS code."""
        ugc_codes = ["OHC049", "OHC041"]

        fips_codes = UGCParser.ugc_to_fips(ugc_codes)

        # Ohio FIPS prefix is 39
        assert "39049" in fips_codes
        assert "39041" in fips_codes

    def test_ugc_to_fips_multiple_states(self):
        """Test FIPS conversion with multiple states."""
        ugc_codes = ["OHC049", "INC001", "MIC003"]

        fips_codes = UGCParser.ugc_to_fips(ugc_codes)

        assert "39049" in fips_codes  # Ohio
        assert "18001" in fips_codes  # Indiana
        assert "26003" in fips_codes  # Michigan

    def test_is_county_code(self):
        """Test county code detection."""
        assert UGCParser.is_county_code("OHC049")
        assert not UGCParser.is_county_code("OHZ049")

    def test_is_zone_code(self):
        """Test zone code detection."""
        assert UGCParser.is_zone_code("OHZ049")
        assert not UGCParser.is_zone_code("OHC049")

    def test_get_state_from_ugc(self):
        """Test extracting state from UGC code."""
        assert UGCParser.get_state_from_ugc("OHC049") == "OH"
        assert UGCParser.get_state_from_ugc("INC001") == "IN"

    def test_filter_by_states(self):
        """Test filtering UGC codes by state."""
        ugc_codes = ["OHC049", "INC001", "MIC003", "OHC041"]

        filtered = UGCParser.filter_by_states(ugc_codes, ["OH"])

        assert len(filtered) == 2
        assert all(code.startswith("OH") for code in filtered)

    def test_format_location_string(self):
        """Test generating human-readable location string."""
        ugc_codes = ["OHC049", "OHC041", "OHZ001", "INC001"]

        location_str = UGCParser.format_location_string(ugc_codes)

        assert "OH" in location_str
        assert "IN" in location_str
        assert "counties" in location_str.lower() or "county" in location_str.lower()

    def test_parse_range_reversed_order(self):
        """Test that reversed range (005>001) is handled correctly."""
        text = "OHC005>001-201530-"

        result = UGCParser.parse(text)

        assert result.is_valid
        # Should swap and still produce 001-005
        assert len(result.ugc_codes) == 5
        assert "OHC001" in result.ugc_codes
        assert "OHC005" in result.ugc_codes

    def test_parse_empty_text(self):
        """Test parsing empty text."""
        result = UGCParser.parse("")

        assert not result.is_valid
        assert len(result.ugc_codes) == 0

    def test_parse_no_ugc_in_text(self):
        """Test parsing text with no UGC codes."""
        text = "This is some random text without any UGC codes."

        result = UGCParser.parse(text)

        assert not result.is_valid
        assert len(result.ugc_codes) == 0


class TestUGCParserXMLFIPS:
    """Tests for XML/FIPS code parsing."""

    def test_parse_xml_fips_6digit(self):
        """Test parsing 6-digit FIPS from XML."""
        text = """
        <valueName>SAME</valueName>
        <value>039049</value>
        """

        fips_codes = UGCParser.parse_xml_fips(text)

        assert len(fips_codes) == 1
        assert fips_codes[0] == "39049"  # Normalized to 5 digits

    def test_parse_xml_fips_5digit(self):
        """Test parsing 5-digit FIPS from XML."""
        text = """
        <valueName>FIPS6</valueName>
        <value>39049</value>
        """

        fips_codes = UGCParser.parse_xml_fips(text)

        assert len(fips_codes) == 1
        assert fips_codes[0] == "39049"

    def test_parse_xml_fips_multiple(self):
        """Test parsing multiple FIPS codes from XML."""
        text = """
        <valueName>SAME</valueName>
        <value>039049</value>
        <valueName>SAME</valueName>
        <value>039041</value>
        <valueName>SAME</valueName>
        <value>018001</value>
        """

        fips_codes = UGCParser.parse_xml_fips(text)

        assert len(fips_codes) == 3
        assert "39049" in fips_codes
        assert "39041" in fips_codes
        assert "18001" in fips_codes
