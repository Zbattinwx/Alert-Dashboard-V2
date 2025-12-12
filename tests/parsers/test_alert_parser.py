"""
Tests for main alert parser.
"""

import pytest
from datetime import datetime, timezone

from backend.parsers.alert_parser import AlertParser, parse_alert
from backend.models.alert import Alert, AlertStatus, AlertSignificance


class TestAlertParserAPI:
    """Tests for parsing NWS API alerts."""

    def test_parse_api_tornado_warning(self):
        """Test parsing tornado warning from API."""
        feature = {
            "properties": {
                "id": "urn:oid:2.49.0.1.840.0.abc123",
                "event": "Tornado Warning",
                "headline": "Tornado Warning issued for Franklin County",
                "description": """
                /O.NEW.KCLE.TO.W.0001.250120T1530Z-250120T1630Z/

                TORNADO WARNING FOR FRANKLIN COUNTY UNTIL 430 PM EST.

                AT 330 PM EST, A SEVERE THUNDERSTORM CAPABLE OF PRODUCING
                A TORNADO WAS LOCATED NEAR COLUMBUS.

                TORNADO...RADAR INDICATED
                HAIL...1.75 INCHES
                WIND...70 MPH

                TIME...MOT...LOC 2030Z 240DEG 35KT 3996 8299
                """,
                "instruction": "TAKE COVER NOW!",
                "sent": "2025-01-20T15:30:00-05:00",
                "effective": "2025-01-20T15:30:00-05:00",
                "ends": "2025-01-20T16:30:00-05:00",
                "senderName": "NWS Cleveland OH",
                "geocode": {
                    "UGC": ["OHC049"],
                    "SAME": ["039049"]
                },
                "parameters": {
                    "VTEC": ["/O.NEW.KCLE.TO.W.0001.250120T1530Z-250120T1630Z/"]
                }
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [-83.0, 40.0],
                    [-83.0, 40.1],
                    [-82.9, 40.1],
                    [-82.9, 40.0],
                    [-83.0, 40.0]
                ]]
            }
        }

        alert = AlertParser.parse_api_alert(feature, source="api")

        assert alert is not None
        assert alert.source == "api"
        assert alert.phenomenon == "TO"
        assert alert.significance == AlertSignificance.WARNING
        assert alert.event_name == "Tornado Warning"
        assert "Franklin" in alert.headline
        assert alert.vtec is not None
        assert alert.vtec.office == "KCLE"
        assert "OHC049" in alert.affected_areas
        assert "39049" in alert.fips_codes
        assert alert.expiration_time is not None
        assert len(alert.polygon) > 0
        assert alert.threat.tornado_detection == "RADAR INDICATED"
        assert alert.threat.max_hail_size_inches == 1.75
        assert alert.threat.max_wind_gust_mph == 70

    def test_parse_api_severe_thunderstorm_watch(self):
        """Test parsing severe thunderstorm watch from API."""
        feature = {
            "properties": {
                "id": "urn:oid:2.49.0.1.840.0.watch123",
                "event": "Severe Thunderstorm Watch",
                "headline": "Severe Thunderstorm Watch 150 in effect",
                "description": """
                /O.NEW.KWNS.SV.A.0150.250120T1800Z-250121T0000Z/

                SEVERE THUNDERSTORM WATCH 150 IS IN EFFECT UNTIL
                7 PM EST FOR PORTIONS OF OHIO AND INDIANA.
                """,
                "sent": "2025-01-20T13:00:00-05:00",
                "expires": "2025-01-21T00:00:00-05:00",
                "geocode": {
                    "UGC": ["OHC049", "OHC041", "INC001"],
                    "SAME": ["039049", "039041", "018001"]
                },
                "parameters": {
                    "VTEC": ["/O.NEW.KWNS.SV.A.0150.250120T1800Z-250121T0000Z/"]
                }
            }
        }

        alert = AlertParser.parse_api_alert(feature)

        assert alert is not None
        assert alert.phenomenon == "SV"
        assert alert.significance == AlertSignificance.WATCH
        assert alert.vtec.event_tracking_number == 150
        assert len(alert.affected_areas) == 3

    def test_parse_api_winter_storm_warning(self):
        """Test parsing winter storm warning from API."""
        feature = {
            "properties": {
                "event": "Winter Storm Warning",
                "description": """
                /O.NEW.KILN.WS.W.0005.250120T1200Z-250121T1200Z/

                WINTER STORM WARNING IN EFFECT.

                SNOW ACCUMULATION...8 TO 12 INCHES
                ICE ACCUMULATION...UP TO 0.25 INCHES
                """,
                "ends": "2025-01-21T12:00:00-05:00",
                "geocode": {
                    "UGC": ["OHZ049", "OHZ050"],
                    "SAME": ["039049"]
                },
                "parameters": {}
            }
        }

        alert = AlertParser.parse_api_alert(feature)

        assert alert is not None
        assert alert.phenomenon == "WS"
        assert alert.threat.snow_amount_min_inches == 8.0
        assert alert.threat.snow_amount_max_inches == 12.0
        assert alert.threat.ice_accumulation_inches == 0.25


class TestAlertParserText:
    """Tests for parsing NWWS text alerts."""

    def test_parse_text_tornado_warning(self):
        """Test parsing tornado warning from NWWS text."""
        text = """
        WFUS53 KCLE 201530
        TORCLE

        OHC049-201630-
        /O.NEW.KCLE.TO.W.0001.250120T1530Z-250120T1630Z/

        BULLETIN - EAS ACTIVATION REQUESTED
        TORNADO WARNING
        NATIONAL WEATHER SERVICE CLEVELAND OH
        330 PM EST MON JAN 20 2025

        ...TORNADO WARNING FOR FRANKLIN COUNTY...

        AT 330 PM EST, A SEVERE THUNDERSTORM CAPABLE OF PRODUCING A
        TORNADO WAS LOCATED NEAR COLUMBUS, MOVING NORTHEAST AT 35 MPH.

        HAZARD...TORNADO.

        SOURCE...RADAR INDICATED.

        TORNADO...RADAR INDICATED
        HAIL...1 INCHES

        LAT...LON 4005 8310 4015 8295 4010 8280 3995 8300

        TIME...MOT...LOC 2030Z 225DEG 30KT 4005 8300

        TAKE COVER NOW!
        """

        alert = AlertParser.parse_text_alert(text, source="nwws")

        assert alert is not None
        assert alert.source == "nwws"
        assert alert.phenomenon == "TO"
        assert alert.significance == AlertSignificance.WARNING
        assert alert.vtec is not None
        assert alert.vtec.office == "KCLE"
        assert alert.vtec.event_tracking_number == 1
        assert "OHC049" in alert.affected_areas
        assert len(alert.polygon) > 0
        assert alert.threat.tornado_detection == "RADAR INDICATED"
        assert alert.threat.storm_motion is not None

    def test_parse_text_severe_thunderstorm_warning(self):
        """Test parsing severe thunderstorm warning from text."""
        text = """
        WUUS53 KILN 201530
        SVRILN

        OHC049-041-201630-
        /O.NEW.KILN.SV.W.0025.250120T1530Z-250120T1630Z/

        BULLETIN - IMMEDIATE BROADCAST REQUESTED
        SEVERE THUNDERSTORM WARNING
        NATIONAL WEATHER SERVICE WILMINGTON OH
        330 PM EST MON JAN 20 2025

        HAZARD...60 MPH WIND GUSTS AND QUARTER SIZE HAIL.

        LAT...LON 3950 8400 3960 8380 3945 8370 3935 8390

        TIME...MOT...LOC 2030Z 250DEG 35KT 3950 8385
        """

        alert = AlertParser.parse_text_alert(text)

        assert alert is not None
        assert alert.phenomenon == "SV"
        assert alert.vtec.office == "KILN"
        assert len(alert.affected_areas) == 2
        assert alert.threat.max_wind_gust_mph == 60
        assert alert.threat.max_hail_size_inches == 1.0  # Quarter size

    def test_parse_text_cancellation(self):
        """Test parsing cancellation from text."""
        text = """
        /O.CAN.KCLE.TO.W.0001.000000T0000Z-250120T1630Z/

        THE TORNADO WARNING FOR FRANKLIN COUNTY HAS BEEN CANCELLED.
        """

        alert = AlertParser.parse_text_alert(text)

        assert alert is not None
        assert alert.status == AlertStatus.CANCELLED
        assert alert.vtec.action.value == "CAN"

    def test_parse_text_watch_product(self):
        """Test parsing watch product without standard VTEC."""
        text = """
        WWUS30 KWNS 201800

        TORNADO WATCH NUMBER 150

        EFFECTIVE THIS MONDAY AFTERNOON AND EVENING FROM 1 PM
        UNTIL 8 PM EST.

        PORTIONS OF OHIO AND INDIANA

        OHC049-041-061-INC001-002-210100-
        """

        alert = AlertParser.parse_text_alert(text)

        assert alert is not None
        assert alert.phenomenon == "TO"
        assert alert.significance == AlertSignificance.WATCH


class TestSPSFiltering:
    """Tests for Special Weather Statement filtering."""

    def test_sps_thunderstorm_included(self):
        """Test that thunderstorm-related SPS is included."""
        feature = {
            "properties": {
                "event": "Special Weather Statement",
                "description": """
                /O.NEW.KCLE.SPS.0001.250120T1530Z-250120T1630Z/

                A STRONG THUNDERSTORM IS APPROACHING THE AREA.
                GUSTY WINDS AND SMALL HAIL ARE POSSIBLE.
                """,
                "geocode": {"UGC": ["OHC049"]}
            }
        }

        alert = AlertParser.parse_api_alert(feature)

        # Should be included because it mentions thunderstorm
        assert alert is not None

    def test_sps_fire_excluded(self):
        """Test that fire-related SPS is excluded."""
        feature = {
            "properties": {
                "event": "Special Weather Statement",
                "description": """
                FIRE WEATHER CONDITIONS EXPECTED TODAY.
                ELEVATED FIRE DANGER DUE TO LOW HUMIDITY AND WIND.
                """,
                "geocode": {"UGC": ["OHC049"]}
            }
        }

        alert = AlertParser.parse_api_alert(feature)

        # Should be excluded because it's fire-related
        assert alert is None

    def test_sps_fog_excluded(self):
        """Test that fog-related SPS is excluded."""
        feature = {
            "properties": {
                "event": "Special Weather Statement",
                "description": """
                DENSE FOG ADVISORY IN EFFECT.
                VISIBILITY BELOW ONE QUARTER MILE.
                """,
                "geocode": {"UGC": ["OHC049"]}
            }
        }

        alert = AlertParser.parse_api_alert(feature)

        assert alert is None


class TestPolygonParsing:
    """Tests for polygon coordinate parsing."""

    def test_parse_text_polygon_coordinates(self):
        """Test parsing LAT...LON polygon from text."""
        text = """
        LAT...LON 4005 8310 4015 8295 4010 8280 3995 8300
                  4005 8310
        """

        alert = Alert()
        polygon = AlertParser._parse_text_polygon(text, is_xml=False)

        assert len(polygon) >= 4
        # Coordinates should be [lat, lon] format
        # Original: 4005 = 40.05, 8310 = -83.10
        assert all(20 <= p[0] <= 60 for p in polygon)  # Lat in US range
        assert all(-130 <= p[1] <= -60 for p in polygon)  # Lon in US range

    def test_parse_geojson_polygon(self):
        """Test parsing GeoJSON polygon."""
        geometry = {
            "type": "Polygon",
            "coordinates": [[
                [-83.0, 40.0],
                [-83.0, 40.1],
                [-82.9, 40.1],
                [-82.9, 40.0],
                [-83.0, 40.0]
            ]]
        }

        polygon = AlertParser._parse_geojson_geometry(geometry)

        assert len(polygon) == 5
        # Should be converted to [lat, lon] format
        assert polygon[0] == [40.0, -83.0]

    def test_calculate_centroid(self):
        """Test centroid calculation."""
        polygon = [
            [40.0, -83.0],
            [40.1, -83.0],
            [40.1, -82.9],
            [40.0, -82.9],
            [40.0, -83.0]
        ]

        centroid = AlertParser._calculate_centroid(polygon)

        assert centroid is not None
        assert 40.0 <= centroid[0] <= 40.1
        assert -83.0 <= centroid[1] <= -82.9


class TestConvenienceFunction:
    """Tests for the parse_alert convenience function."""

    def test_parse_alert_dict(self):
        """Test parse_alert with dict input."""
        feature = {
            "properties": {
                "event": "Tornado Warning",
                "description": "/O.NEW.KCLE.TO.W.0001.250120T1530Z-250120T1630Z/",
                "geocode": {"UGC": ["OHC049"]}
            }
        }

        alert = parse_alert(feature, source="api")

        assert alert is not None
        assert alert.source == "api"

    def test_parse_alert_string(self):
        """Test parse_alert with string input."""
        text = """
        /O.NEW.KCLE.TO.W.0001.250120T1530Z-250120T1630Z/
        OHC049-201630-
        TORNADO WARNING
        """

        alert = parse_alert(text, source="nwws")

        assert alert is not None
        assert alert.source == "nwws"
