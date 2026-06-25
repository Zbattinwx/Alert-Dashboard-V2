"""
NEXRAD WSR-88D radar site locations.
Contains ICAO codes, coordinates, and metadata for all operational NEXRAD sites.
"""

NEXRAD_SITES = {
    # Alabama
    "KBMX": {"name": "Birmingham, AL", "lat": 33.1722, "lon": -86.7697, "state": "AL"},
    "KEOX": {"name": "Fort Rucker, AL", "lat": 31.4606, "lon": -85.4597, "state": "AL"},
    "KHTX": {"name": "Huntsville, AL", "lat": 34.9306, "lon": -86.0833, "state": "AL"},
    "KMOB": {"name": "Mobile, AL", "lat": 30.6794, "lon": -88.2397, "state": "AL"},
    "KMXX": {"name": "Maxwell AFB, AL", "lat": 32.5367, "lon": -85.7897, "state": "AL"},

    # Alaska
    "PABC": {"name": "Bethel, AK", "lat": 60.7919, "lon": -161.8764, "state": "AK"},
    "PACG": {"name": "Sitka, AK", "lat": 56.8528, "lon": -135.5292, "state": "AK"},
    "PAEC": {"name": "Nome, AK", "lat": 64.5114, "lon": -165.2950, "state": "AK"},
    "PAHG": {"name": "Anchorage, AK", "lat": 60.7258, "lon": -151.3514, "state": "AK"},
    "PAIH": {"name": "Middleton Island, AK", "lat": 59.4614, "lon": -146.3031, "state": "AK"},
    "PAKC": {"name": "King Salmon, AK", "lat": 58.6794, "lon": -156.6294, "state": "AK"},
    "PAPD": {"name": "Fairbanks, AK", "lat": 65.0356, "lon": -147.5014, "state": "AK"},

    # Arizona
    "KEMX": {"name": "Tucson, AZ", "lat": 31.8936, "lon": -110.6303, "state": "AZ"},
    "KFSX": {"name": "Flagstaff, AZ", "lat": 34.5744, "lon": -111.1983, "state": "AZ"},
    "KIWA": {"name": "Phoenix, AZ", "lat": 33.2892, "lon": -111.6700, "state": "AZ"},
    "KYUX": {"name": "Yuma, AZ", "lat": 32.4953, "lon": -114.6567, "state": "AZ"},

    # Arkansas
    "KLZK": {"name": "Little Rock, AR", "lat": 34.8364, "lon": -92.2622, "state": "AR"},
    "KSRX": {"name": "Fort Smith, AR", "lat": 35.2906, "lon": -94.3619, "state": "AR"},

    # California
    "KBBX": {"name": "Beale AFB, CA", "lat": 39.4961, "lon": -121.6317, "state": "CA"},
    "KDAX": {"name": "Sacramento, CA", "lat": 38.5011, "lon": -121.6778, "state": "CA"},
    "KEYX": {"name": "Edwards AFB, CA", "lat": 35.0978, "lon": -117.5608, "state": "CA"},
    "KHNX": {"name": "Hanford, CA", "lat": 36.3142, "lon": -119.6322, "state": "CA"},
    "KMUX": {"name": "San Francisco, CA", "lat": 37.1553, "lon": -121.8983, "state": "CA"},
    "KNKX": {"name": "San Diego, CA", "lat": 32.9189, "lon": -117.0419, "state": "CA"},
    "KSOX": {"name": "Santa Ana Mtns, CA", "lat": 33.8178, "lon": -117.6358, "state": "CA"},
    "KVBX": {"name": "Vandenberg AFB, CA", "lat": 34.8383, "lon": -120.3978, "state": "CA"},
    "KVTX": {"name": "Los Angeles, CA", "lat": 34.4117, "lon": -119.1792, "state": "CA"},

    # Colorado
    "KFTG": {"name": "Denver, CO", "lat": 39.7867, "lon": -104.5458, "state": "CO"},
    "KGJX": {"name": "Grand Junction, CO", "lat": 39.0622, "lon": -108.2139, "state": "CO"},
    "KPUX": {"name": "Pueblo, CO", "lat": 38.4597, "lon": -104.1814, "state": "CO"},

    # Connecticut
    "KOKX": {"name": "New York City, NY", "lat": 40.8656, "lon": -72.8639, "state": "NY"},

    # Delaware / Mid-Atlantic
    "KDOX": {"name": "Dover AFB, DE", "lat": 38.8256, "lon": -75.4400, "state": "DE"},

    # Florida
    "KAMX": {"name": "Miami, FL", "lat": 25.6111, "lon": -80.4128, "state": "FL"},
    "KBYX": {"name": "Key West, FL", "lat": 24.5975, "lon": -81.7033, "state": "FL"},
    "KEVX": {"name": "Eglin AFB, FL", "lat": 30.5644, "lon": -85.9214, "state": "FL"},
    "KJAX": {"name": "Jacksonville, FL", "lat": 30.4847, "lon": -81.7019, "state": "FL"},
    "KMLB": {"name": "Melbourne, FL", "lat": 28.1131, "lon": -80.6542, "state": "FL"},
    "KTBW": {"name": "Tampa Bay, FL", "lat": 27.7056, "lon": -82.4017, "state": "FL"},
    "KTLH": {"name": "Tallahassee, FL", "lat": 30.3975, "lon": -84.3289, "state": "FL"},

    # Georgia
    "KFFC": {"name": "Atlanta, GA", "lat": 33.3636, "lon": -84.5658, "state": "GA"},
    "KJGX": {"name": "Robins AFB, GA", "lat": 32.6753, "lon": -83.3511, "state": "GA"},
    "KVAX": {"name": "Moody AFB, GA", "lat": 30.8903, "lon": -83.0019, "state": "GA"},

    # Hawaii
    "PHKI": {"name": "South Kauai, HI", "lat": 21.8939, "lon": -159.5522, "state": "HI"},
    "PHKM": {"name": "Kamuela, HI", "lat": 20.1256, "lon": -155.7781, "state": "HI"},
    "PHMO": {"name": "Molokai, HI", "lat": 21.1328, "lon": -157.1803, "state": "HI"},
    "PHWA": {"name": "South Shore, HI", "lat": 19.0950, "lon": -155.5686, "state": "HI"},

    # Idaho
    "KCBX": {"name": "Boise, ID", "lat": 43.4908, "lon": -116.2356, "state": "ID"},
    "KSFX": {"name": "Pocatello, ID", "lat": 43.1058, "lon": -112.6861, "state": "ID"},

    # Illinois
    "KILX": {"name": "Lincoln, IL", "lat": 40.1506, "lon": -89.3369, "state": "IL"},
    "KLOT": {"name": "Chicago, IL", "lat": 41.6044, "lon": -88.0847, "state": "IL"},

    # Indiana
    "KIND": {"name": "Indianapolis, IN", "lat": 39.7075, "lon": -86.2803, "state": "IN"},
    "KIWX": {"name": "North Webster, IN", "lat": 41.3586, "lon": -85.7000, "state": "IN"},
    "KVWX": {"name": "Evansville, IN", "lat": 38.2603, "lon": -87.7247, "state": "IN"},

    # Iowa
    "KDMX": {"name": "Des Moines, IA", "lat": 41.7311, "lon": -93.7228, "state": "IA"},
    "KDVN": {"name": "Davenport, IA", "lat": 41.6117, "lon": -90.5808, "state": "IA"},

    # Kansas
    "KDDC": {"name": "Dodge City, KS", "lat": 37.7608, "lon": -99.9689, "state": "KS"},
    "KGLD": {"name": "Goodland, KS", "lat": 39.3667, "lon": -101.7006, "state": "KS"},
    "KICT": {"name": "Wichita, KS", "lat": 37.6544, "lon": -97.4431, "state": "KS"},
    "KTWX": {"name": "Topeka, KS", "lat": 38.9969, "lon": -96.2325, "state": "KS"},

    # Kentucky
    "KHPX": {"name": "Fort Campbell, KY", "lat": 36.7369, "lon": -87.2850, "state": "KY"},
    "KJKL": {"name": "Jackson, KY", "lat": 37.5908, "lon": -83.3131, "state": "KY"},
    "KLVX": {"name": "Louisville, KY", "lat": 37.9753, "lon": -85.9439, "state": "KY"},
    "KPAH": {"name": "Paducah, KY", "lat": 37.0683, "lon": -88.7719, "state": "KY"},

    # Louisiana
    "KHDL": {"name": "Baton Rouge, LA", "lat": 30.5197, "lon": -91.2150, "state": "LA"},
    "KLCH": {"name": "Lake Charles, LA", "lat": 30.1253, "lon": -93.2156, "state": "LA"},
    "KLIX": {"name": "New Orleans, LA", "lat": 30.3367, "lon": -89.8256, "state": "LA"},
    "KPOE": {"name": "Fort Polk, LA", "lat": 31.1556, "lon": -92.9758, "state": "LA"},
    "KSHV": {"name": "Shreveport, LA", "lat": 32.4508, "lon": -93.8414, "state": "LA"},

    # Maine
    "KCBW": {"name": "Caribou, ME", "lat": 46.0392, "lon": -67.8067, "state": "ME"},
    "KGYX": {"name": "Portland, ME", "lat": 43.8914, "lon": -70.2564, "state": "ME"},

    # Maryland
    "KLWX": {"name": "Sterling, VA", "lat": 38.9753, "lon": -77.4778, "state": "VA"},

    # Massachusetts
    "KBOX": {"name": "Boston, MA", "lat": 41.9558, "lon": -71.1369, "state": "MA"},
    "KCXX": {"name": "Burlington, VT", "lat": 44.5111, "lon": -73.1669, "state": "VT"},

    # Michigan
    "KAPX": {"name": "Gaylord, MI", "lat": 44.9072, "lon": -84.7197, "state": "MI"},
    "KDTX": {"name": "Detroit, MI", "lat": 42.6997, "lon": -83.4717, "state": "MI"},
    "KGRR": {"name": "Grand Rapids, MI", "lat": 42.8939, "lon": -85.5447, "state": "MI"},
    "KMQT": {"name": "Marquette, MI", "lat": 46.5311, "lon": -87.5486, "state": "MI"},

    # Minnesota
    "KDLH": {"name": "Duluth, MN", "lat": 46.8369, "lon": -92.2097, "state": "MN"},
    "KMPX": {"name": "Minneapolis, MN", "lat": 44.8489, "lon": -93.5656, "state": "MN"},

    # Mississippi
    "KDGX": {"name": "Brandon, MS", "lat": 32.2797, "lon": -89.9844, "state": "MS"},
    "KGWX": {"name": "Columbus AFB, MS", "lat": 33.8967, "lon": -88.3292, "state": "MS"},

    # Missouri
    "KEAX": {"name": "Kansas City, MO", "lat": 38.8103, "lon": -94.2644, "state": "MO"},
    "KLSX": {"name": "St. Louis, MO", "lat": 38.6986, "lon": -90.6828, "state": "MO"},
    "KSGF": {"name": "Springfield, MO", "lat": 37.2353, "lon": -93.4006, "state": "MO"},

    # Montana
    "KBLX": {"name": "Billings, MT", "lat": 45.8536, "lon": -108.6069, "state": "MT"},
    "KGGW": {"name": "Glasgow, MT", "lat": 48.2064, "lon": -106.6253, "state": "MT"},
    "KMSX": {"name": "Missoula, MT", "lat": 47.0411, "lon": -113.9864, "state": "MT"},
    "KTFX": {"name": "Great Falls, MT", "lat": 47.4597, "lon": -111.3856, "state": "MT"},

    # Nebraska
    "KLNX": {"name": "North Platte, NE", "lat": 41.9578, "lon": -100.5761, "state": "NE"},
    "KOAX": {"name": "Omaha, NE", "lat": 41.3203, "lon": -96.3667, "state": "NE"},
    "KUEX": {"name": "Hastings, NE", "lat": 40.3208, "lon": -98.4419, "state": "NE"},

    # Nevada
    "KESX": {"name": "Las Vegas, NV", "lat": 35.7011, "lon": -114.8917, "state": "NV"},
    "KLRX": {"name": "Elko, NV", "lat": 40.7397, "lon": -116.8025, "state": "NV"},
    "KRGX": {"name": "Reno, NV", "lat": 39.7542, "lon": -119.4622, "state": "NV"},

    # New Mexico
    "KABX": {"name": "Albuquerque, NM", "lat": 35.1497, "lon": -106.8239, "state": "NM"},
    "KFDX": {"name": "Cannon AFB, NM", "lat": 34.6342, "lon": -103.6186, "state": "NM"},
    "KHDX": {"name": "Holloman AFB, NM", "lat": 33.0764, "lon": -106.1200, "state": "NM"},

    # New York
    "KBGM": {"name": "Binghamton, NY", "lat": 42.1997, "lon": -75.9847, "state": "NY"},
    "KBUF": {"name": "Buffalo, NY", "lat": 42.9489, "lon": -78.7369, "state": "NY"},
    "KENX": {"name": "Albany, NY", "lat": 42.5864, "lon": -74.0639, "state": "NY"},
    "KTYX": {"name": "Montague, NY", "lat": 43.7558, "lon": -75.6800, "state": "NY"},

    # North Carolina
    "KLTX": {"name": "Wilmington, NC", "lat": 33.9892, "lon": -78.4292, "state": "NC"},
    "KMHX": {"name": "Morehead City, NC", "lat": 34.7758, "lon": -76.8764, "state": "NC"},
    "KRAX": {"name": "Raleigh, NC", "lat": 35.6656, "lon": -78.4903, "state": "NC"},

    # North Dakota
    "KBIS": {"name": "Bismarck, ND", "lat": 46.7708, "lon": -100.7603, "state": "ND"},
    "KMVX": {"name": "Grand Forks, ND", "lat": 47.5278, "lon": -97.3256, "state": "ND"},
    "KMBX": {"name": "Minot AFB, ND", "lat": 48.3925, "lon": -100.8644, "state": "ND"},

    # Ohio
    "KCLE": {"name": "Cleveland, OH", "lat": 41.4131, "lon": -81.8597, "state": "OH"},
    "KILN": {"name": "Wilmington, OH", "lat": 39.4203, "lon": -83.8217, "state": "OH"},

    # Oklahoma
    "KFDR": {"name": "Frederick, OK", "lat": 34.3622, "lon": -98.9764, "state": "OK"},
    "KINX": {"name": "Tulsa, OK", "lat": 36.1750, "lon": -95.5644, "state": "OK"},
    "KTLX": {"name": "Oklahoma City, OK", "lat": 35.3331, "lon": -97.2778, "state": "OK"},
    "KVNX": {"name": "Vance AFB, OK", "lat": 36.7406, "lon": -98.1278, "state": "OK"},

    # Oregon
    "KMAX": {"name": "Medford, OR", "lat": 42.0811, "lon": -122.7167, "state": "OR"},
    "KPDT": {"name": "Pendleton, OR", "lat": 45.6906, "lon": -118.8531, "state": "OR"},
    "KRTX": {"name": "Portland, OR", "lat": 45.7150, "lon": -122.9656, "state": "OR"},

    # Pennsylvania
    "KCCX": {"name": "State College, PA", "lat": 40.9231, "lon": -78.0036, "state": "PA"},
    "KDIX": {"name": "Philadelphia, PA", "lat": 39.9469, "lon": -74.4108, "state": "PA"},
    "KPBZ": {"name": "Pittsburgh, PA", "lat": 40.5317, "lon": -80.2179, "state": "PA"},

    # Puerto Rico
    "TJUA": {"name": "San Juan, PR", "lat": 18.1156, "lon": -66.0781, "state": "PR"},

    # South Carolina
    "KCAE": {"name": "Columbia, SC", "lat": 33.9486, "lon": -81.1186, "state": "SC"},
    "KCLX": {"name": "Charleston, SC", "lat": 32.6556, "lon": -81.0422, "state": "SC"},
    "KGSP": {"name": "Greenville, SC", "lat": 34.8833, "lon": -82.2200, "state": "SC"},

    # South Dakota
    "KABR": {"name": "Aberdeen, SD", "lat": 45.4558, "lon": -98.4131, "state": "SD"},
    "KUDX": {"name": "Rapid City, SD", "lat": 44.1250, "lon": -102.8297, "state": "SD"},
    "KFSD": {"name": "Sioux Falls, SD", "lat": 43.5878, "lon": -96.7292, "state": "SD"},

    # Tennessee
    "KMRX": {"name": "Knoxville, TN", "lat": 36.1686, "lon": -83.4017, "state": "TN"},
    "KNQA": {"name": "Memphis, TN", "lat": 35.3447, "lon": -89.8733, "state": "TN"},
    "KOHX": {"name": "Nashville, TN", "lat": 36.2472, "lon": -86.5625, "state": "TN"},

    # Texas
    "KAMA": {"name": "Amarillo, TX", "lat": 35.2333, "lon": -101.7092, "state": "TX"},
    "KBRO": {"name": "Brownsville, TX", "lat": 25.9158, "lon": -97.4189, "state": "TX"},
    "KCRP": {"name": "Corpus Christi, TX", "lat": 27.7842, "lon": -97.5108, "state": "TX"},
    "KDFX": {"name": "Laughlin AFB, TX", "lat": 29.2725, "lon": -100.2803, "state": "TX"},
    "KDYX": {"name": "Dyess AFB, TX", "lat": 32.5386, "lon": -99.2542, "state": "TX"},
    "KEPZ": {"name": "El Paso, TX", "lat": 31.8731, "lon": -106.6981, "state": "TX"},
    "KEWX": {"name": "Austin/San Antonio, TX", "lat": 29.7039, "lon": -98.0286, "state": "TX"},
    "KFWS": {"name": "Dallas/Fort Worth, TX", "lat": 32.5731, "lon": -97.3031, "state": "TX"},
    "KGRK": {"name": "Fort Hood, TX", "lat": 30.7219, "lon": -97.3828, "state": "TX"},
    "KHGX": {"name": "Houston, TX", "lat": 29.4719, "lon": -95.0792, "state": "TX"},
    "KLBB": {"name": "Lubbock, TX", "lat": 33.6542, "lon": -101.8142, "state": "TX"},
    "KMAF": {"name": "Midland/Odessa, TX", "lat": 31.9433, "lon": -102.1894, "state": "TX"},
    "KSJT": {"name": "San Angelo, TX", "lat": 31.3711, "lon": -100.4925, "state": "TX"},

    # Utah
    "KICX": {"name": "Cedar City, UT", "lat": 37.5908, "lon": -112.8622, "state": "UT"},
    "KMTX": {"name": "Salt Lake City, UT", "lat": 41.2628, "lon": -112.4481, "state": "UT"},

    # Virginia
    "KAKQ": {"name": "Wakefield, VA", "lat": 36.9839, "lon": -77.0072, "state": "VA"},
    "KFCX": {"name": "Roanoke, VA", "lat": 37.0242, "lon": -80.2742, "state": "VA"},

    # Washington
    "KATX": {"name": "Seattle, WA", "lat": 48.1944, "lon": -122.4958, "state": "WA"},
    "KLGX": {"name": "Langley Hill, WA", "lat": 47.1167, "lon": -124.1069, "state": "WA"},
    "KOTX": {"name": "Spokane, WA", "lat": 47.6803, "lon": -117.6267, "state": "WA"},

    # West Virginia
    "KRLX": {"name": "Charleston, WV", "lat": 38.3111, "lon": -81.7228, "state": "WV"},

    # Wisconsin
    "KARX": {"name": "La Crosse, WI", "lat": 43.8228, "lon": -91.1911, "state": "WI"},
    "KGRB": {"name": "Green Bay, WI", "lat": 44.4986, "lon": -88.1111, "state": "WI"},
    "KMKX": {"name": "Milwaukee, WI", "lat": 42.9678, "lon": -88.5506, "state": "WI"},

    # Wyoming
    "KCYS": {"name": "Cheyenne, WY", "lat": 41.1519, "lon": -104.8061, "state": "WY"},
    "KRIW": {"name": "Riverton, WY", "lat": 43.0661, "lon": -108.4772, "state": "WY"},

    # Guam
    "PGUA": {"name": "Andersen AFB, GU", "lat": 13.4544, "lon": 144.8111, "state": "GU"},
}


def get_nearest_sites(lat: float, lon: float, count: int = 10) -> list[dict]:
    """Return the nearest NEXRAD sites to a given lat/lon, sorted by distance."""
    import math

    results = []
    for site_id, info in NEXRAD_SITES.items():
        dlat = math.radians(info["lat"] - lat)
        dlon = math.radians(info["lon"] - lon)
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(math.radians(lat))
            * math.cos(math.radians(info["lat"]))
            * math.sin(dlon / 2) ** 2
        )
        dist_km = 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        results.append({
            "id": site_id,
            "name": info["name"],
            "lat": info["lat"],
            "lon": info["lon"],
            "state": info["state"],
            "distance_km": round(dist_km, 1),
        })

    results.sort(key=lambda x: x["distance_km"])
    return results[:count]
