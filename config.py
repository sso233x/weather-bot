"""
config.py — station and city configuration for the signal bot.

Website-resolution only for now (resolves against Wunderground per your
research). App-side price/station tracking is left out on purpose --
cross-reference the app manually for now, and we can wire it in later
once you're ready.
"""

CITIES = {
    "LAX": {"name": "Los Angeles", "station": "KLAX",
            "lat": 33.9425, "lon": -118.4081, "slug": "los-angeles"},
    "SFO": {"name": "San Francisco", "station": "KSFO",
            "lat": 37.6213, "lon": -122.3790, "slug": "san-francisco"},
    "MIA": {"name": "Miami", "station": "KMIA",
            "lat": 25.7959, "lon": -80.2870, "slug": "miami"},
    "ORD": {"name": "Chicago", "station": "KORD",
            "lat": 41.9742, "lon": -87.9073, "slug": "chicago"},
    "LGA": {"name": "New York City", "station": "KLGA",
            "lat": 40.7769, "lon": -73.8740, "slug": "nyc"},
}

ALL_STATIONS = sorted({c["station"] for c in CITIES.values()})

# IEM ASOS network code per station -- needed to pull actual observed daily
# highs from the Iowa Environmental Mesonet archive (used to check forecast
# bias per city, e.g. "does MIA run hot"). Station must belong to exactly
# one state ASOS network for this API.
STATION_NETWORK = {
    "KLAX": "CA_ASOS",
    "KSFO": "CA_ASOS",
    "KMIA": "FL_ASOS",
    "KORD": "IL_ASOS",
    "KLGA": "NY_ASOS",
}

# Polymarket US (the app) station-slug codes, confirmed via their public
# gateway API on 2026-07-14. CONFIRMED DIFFERENT from the website/Gamma
# side for Chicago and NYC -- this is the actual structural edge Merritt
# identified early on, now verified directly rather than inferred:
#   Chicago -> "mdw" (Midway/KMDW), NOT "ord" (O'Hare/KORD, the website side)
#   NYC     -> "nyc" (Central Park/KNYC), NOT "lga" (LaGuardia, the website side)
# LA/SF/Miami use the same airport on both platforms.
US_STATION_SLUG = {
    "LAX": "lax",
    "SFO": "sfo",
    "MIA": "mia",
    "ORD": "mdw",
    "LGA": "nyc",
}

XND_SKIP_THRESHOLD = 3
MIN_CONFIDENCE_TO_ACT = 0.70
