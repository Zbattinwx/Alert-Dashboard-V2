"""
Tests for threat data parser.
"""

import pytest

from backend.parsers.threat_parser import ThreatParser
from backend.models.alert import ThreatData, StormMotion


class TestTornadoParsing:
    """Tests for tornado threat parsing."""

    def test_parse_tornado_radar_indicated(self):
        """Test parsing radar indicated tornado."""
        text = "TORNADO...RADAR INDICATED"

        detection = ThreatParser.parse_tornado_detection(text)

        assert detection == "RADAR INDICATED"

    def test_parse_tornado_observed(self):
        """Test parsing observed tornado."""
        text = "TORNADO...OBSERVED"

        detection = ThreatParser.parse_tornado_detection(text)

        assert detection == "OBSERVED"

    def test_parse_tornado_possible(self):
        """Test parsing possible tornado."""
        text = "TORNADO...POSSIBLE"

        detection = ThreatParser.parse_tornado_detection(text)

        assert detection == "POSSIBLE"

    def test_parse_tornado_damage_considerable(self):
        """Test parsing considerable tornado damage threat."""
        text = "TORNADO DAMAGE THREAT...CONSIDERABLE"

        damage = ThreatParser.parse_tornado_damage(text)

        assert damage == "CONSIDERABLE"

    def test_parse_tornado_damage_catastrophic(self):
        """Test parsing catastrophic tornado damage threat."""
        text = "TORNADO DAMAGE THREAT...CATASTROPHIC"

        damage = ThreatParser.parse_tornado_damage(text)

        assert damage == "CATASTROPHIC"

    def test_no_tornado_detection(self):
        """Test text without tornado detection."""
        text = "A severe thunderstorm is approaching."

        detection = ThreatParser.parse_tornado_detection(text)

        assert detection is None


class TestWindParsing:
    """Tests for wind threat parsing."""

    def test_parse_wind_mph(self):
        """Test parsing wind in MPH."""
        text = "WIND...70 MPH"

        mph, kts = ThreatParser.parse_wind_gust(text)

        assert mph == 70
        assert kts is not None

    def test_parse_wind_with_max(self):
        """Test parsing MAX WIND GUST format."""
        text = "MAX WIND GUST...80 MPH"

        mph, kts = ThreatParser.parse_wind_gust(text)

        assert mph == 80

    def test_parse_wind_up_to(self):
        """Test parsing 'up to' wind format."""
        text = "WIND GUSTS UP TO 65 MPH"

        mph, kts = ThreatParser.parse_wind_gust(text)

        assert mph == 65

    def test_parse_wind_knots(self):
        """Test parsing wind in knots."""
        text = "WIND...60 KT"

        mph, kts = ThreatParser.parse_wind_gust(text)

        assert kts == 60
        assert mph is not None
        assert mph > kts  # MPH should be higher than knots

    def test_parse_wind_damage_destructive(self):
        """Test parsing destructive wind damage."""
        text = "WIND DAMAGE THREAT...DESTRUCTIVE"

        damage = ThreatParser.parse_wind_damage(text)

        assert damage == "DESTRUCTIVE"

    def test_wind_outside_range_rejected(self):
        """Test that unreasonable wind values are rejected."""
        text = "WIND...5 MPH"  # Too low to be valid

        mph, kts = ThreatParser.parse_wind_gust(text)

        assert mph is None


class TestHailParsing:
    """Tests for hail threat parsing."""

    def test_parse_hail_numeric(self):
        """Test parsing numeric hail size."""
        text = "HAIL...1.75 INCHES"

        size = ThreatParser.parse_hail_size(text)

        assert size == 1.75

    def test_parse_hail_up_to(self):
        """Test parsing 'up to' hail format."""
        text = "HAIL SIZE...UP TO 2 INCHES"

        size = ThreatParser.parse_hail_size(text)

        assert size == 2.0

    def test_parse_hail_golf_ball(self):
        """Test parsing golf ball size hail."""
        text = "GOLF BALL SIZE HAIL"

        size = ThreatParser.parse_hail_size(text)

        assert size == 1.75

    def test_parse_hail_quarter(self):
        """Test parsing quarter size hail."""
        text = "QUARTER SIZE HAIL POSSIBLE"

        size = ThreatParser.parse_hail_size(text)

        assert size == 1.0

    def test_parse_hail_tennis_ball(self):
        """Test parsing tennis ball size hail."""
        text = "UP TO TENNIS BALL SIZE HAIL"

        size = ThreatParser.parse_hail_size(text)

        assert size == 2.5

    def test_parse_hail_damage_considerable(self):
        """Test parsing considerable hail damage."""
        text = "HAIL DAMAGE THREAT...CONSIDERABLE"

        damage = ThreatParser.parse_hail_damage(text)

        assert damage == "CONSIDERABLE"


class TestSnowParsing:
    """Tests for snow/winter weather parsing."""

    def test_parse_snow_single_amount(self):
        """Test parsing single snow amount."""
        text = "SNOW ACCUMULATION...UP TO 6 INCHES"

        min_amt, max_amt = ThreatParser.parse_snow_amount(text)

        assert min_amt == 6.0
        assert max_amt == 6.0

    def test_parse_snow_range(self):
        """Test parsing snow range."""
        text = "SNOW ACCUMULATION...4 TO 8 INCHES"

        min_amt, max_amt = ThreatParser.parse_snow_amount(text)

        assert min_amt == 4.0
        assert max_amt == 8.0

    def test_parse_snow_no_context(self):
        """Test that snow not parsed without snow context."""
        text = "WIND GUSTS UP TO 6 INCHES"  # No snow keyword

        min_amt, max_amt = ThreatParser.parse_snow_amount(text)

        assert min_amt is None
        assert max_amt is None

    def test_parse_ice_accumulation(self):
        """Test parsing ice accumulation."""
        text = "ICE ACCUMULATION...UP TO 0.5 INCHES"

        ice = ThreatParser.parse_ice_amount(text)

        assert ice == 0.5


class TestStormMotion:
    """Tests for storm motion parsing."""

    def test_parse_motion_standard_format(self):
        """Test parsing standard TIME...MOT...LOC format."""
        text = "TIME...MOT...LOC 1845Z 245DEG 35KT 4105 8132"

        motion = ThreatParser.parse_storm_motion(text)

        assert motion is not None
        assert motion.direction_degrees == 245
        assert motion.speed_kts == 35
        assert motion.speed_mph is not None

    def test_parse_motion_cardinal_format(self):
        """Test parsing cardinal direction format."""
        text = "MOVING TO THE NE AT 40 MPH"

        motion = ThreatParser.parse_storm_motion(text)

        assert motion is not None
        assert motion.speed_mph == 40
        assert motion.direction_degrees is not None

    def test_parse_motion_sw_direction(self):
        """Test parsing SW direction."""
        text = "MOVING SW AT 25 MPH"

        motion = ThreatParser.parse_storm_motion(text)

        assert motion is not None
        assert motion.speed_mph == 25

    def test_no_motion_found(self):
        """Test text without motion information."""
        text = "A tornado warning is in effect."

        motion = ThreatParser.parse_storm_motion(text)

        assert motion is None or not motion.is_valid


class TestFloodParsing:
    """Tests for flood threat parsing."""

    def test_parse_flood_radar_indicated(self):
        """Test parsing radar indicated flash flood."""
        text = "FLASH FLOOD...RADAR INDICATED"

        detection = ThreatParser.parse_flood_detection(text)

        assert detection == "RADAR INDICATED"

    def test_parse_flood_observed(self):
        """Test parsing observed flash flood."""
        text = "FLASH FLOODING...OBSERVED"

        detection = ThreatParser.parse_flood_detection(text)

        assert detection == "OBSERVED"

    def test_parse_flood_damage_catastrophic(self):
        """Test parsing catastrophic flood damage."""
        text = "FLASH FLOOD DAMAGE THREAT...CATASTROPHIC"

        damage = ThreatParser.parse_flood_damage(text)

        assert damage == "CATASTROPHIC"


class TestFullThreatParsing:
    """Tests for complete threat data parsing."""

    def test_parse_severe_thunderstorm_full(self):
        """Test parsing complete severe thunderstorm threat data."""
        text = """
        SEVERE THUNDERSTORM WARNING

        HAZARD...60 MPH WIND GUSTS AND QUARTER SIZE HAIL.

        SOURCE...RADAR INDICATED.

        IMPACT...HAIL DAMAGE TO VEHICLES IS EXPECTED.

        TIME...MOT...LOC 1830Z 250DEG 30KT 4105 8140
        """

        threat = ThreatParser.parse(text)

        assert threat.max_wind_gust_mph == 60
        assert threat.max_hail_size_inches == 1.0
        assert threat.storm_motion is not None
        assert threat.storm_motion.direction_degrees == 250
        assert threat.storm_motion.speed_kts == 30

    def test_parse_tornado_warning_full(self):
        """Test parsing complete tornado warning threat data."""
        text = """
        TORNADO WARNING

        TORNADO...RADAR INDICATED
        HAIL...1.75 INCHES

        TORNADO DAMAGE THREAT...CONSIDERABLE

        TIME...MOT...LOC 1900Z 220DEG 45KT 4110 8120
        """

        threat = ThreatParser.parse(text)

        assert threat.tornado_detection == "RADAR INDICATED"
        assert threat.tornado_damage_threat == "CONSIDERABLE"
        assert threat.max_hail_size_inches == 1.75
        assert threat.storm_motion is not None

    def test_threat_data_is_pds(self):
        """Test PDS (Particularly Dangerous Situation) detection."""
        text = "TORNADO DAMAGE THREAT...CATASTROPHIC"
        threat = ThreatParser.parse(text)
        assert threat.is_pds

        text = "WIND DAMAGE THREAT...DESTRUCTIVE"
        threat = ThreatParser.parse(text)
        assert threat.is_pds

        text = "WIND...60 MPH"
        threat = ThreatParser.parse(text)
        assert not threat.is_pds

    def test_threat_has_significant_wind(self):
        """Test significant wind detection (>=70 mph)."""
        threat = ThreatData(max_wind_gust_mph=75)
        assert threat.has_significant_wind

        threat = ThreatData(max_wind_gust_mph=60)
        assert not threat.has_significant_wind

    def test_threat_has_significant_hail(self):
        """Test significant hail detection (>=1 inch)."""
        threat = ThreatData(max_hail_size_inches=1.5)
        assert threat.has_significant_hail

        threat = ThreatData(max_hail_size_inches=0.75)
        assert not threat.has_significant_hail
