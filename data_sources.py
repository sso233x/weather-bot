"""
data_sources.py — all external data pulls, kept separate from scoring logic
so each fetcher can be tested/swapped independently.
"""

import re
from datetime import datetime, timezone, timedelta

import requests

from config import ALL_STATIONS

NBM_TEXT_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod"
METAR_URL = "https://aviationweather.gov/api/data/metar"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"

HEADER_RE = re.compile(r"^\s*(\S+)\s+NBM\s+V[\d.]+\s+NBS\s+GUIDANCE", re.MULTILINE)


# ---------------------------------------------------------------------------
# NBM
# ---------------------------------------------------------------------------

def fetch_nbm_raw(cycle: str) -> str:
    now = datetime.now(timezone.utc)
    ymd = now.strftime("%Y%m%d")
    url = f"{NBM_TEXT_BASE}/blend.{ymd}/{cycle}/text/blend_nbstx.t{cycle}z"
    resp = requests.get(url, timeout=60)
    if resp.status_code == 404:
        ymd_prev = (now - timedelta(days=1)).strftime("%Y%m%d")
        url = f"{NBM_TEXT_BASE}/blend.{ymd_prev}/{cycle}/text/blend_nbstx.t{cycle}z"
        resp = requests.get(url, timeout=60)
    print(f"Fetched NBM data from: {url} (status {resp.status_code})")
    resp.raise_for_status()
    return resp.text


def split_by_station(raw_text: str, stations) -> dict:
    stations_set = set(stations)
    matches = list(HEADER_RE.finditer(raw_text))
    blocks = {}
    for i, m in enumerate(matches):
        ident = m.group(1)
        if ident in stations_set:
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(raw_text)
            blocks[ident] = raw_text[start:end]
    return blocks


def extract_row(block_text: str, row_label: str):
    for line in block_text.splitlines():
        if line.strip().startswith(row_label):
            tokens = line.strip().split()[1:]
            values = []
            for t in tokens:
                try:
                    values.append(int(t))
                except ValueError:
                    continue
            return values
    return []


def fetch_all_nbm(cycle: str) -> dict:
    """Returns {station: {"TXN": [...], "XND": [...]}} for ALL_STATIONS."""
    raw = fetch_nbm_raw(cycle)
    blocks = split_by_station(raw, ALL_STATIONS)
    result = {}
    for station in ALL_STATIONS:
        block = blocks.get(station)
        if block:
            result[station] = {
                "TXN": extract_row(block, "TXN"),
                "XND": extract_row(block, "XND"),
            }
        else:
            result[station] = {"TXN": [], "XND": []}
    return result


# ---------------------------------------------------------------------------
# METAR
# ---------------------------------------------------------------------------

def fetch_all_metar() -> dict:
    params = {"ids": ",".join(ALL_STATIONS), "format": "json", "taf": "false"}
    resp = requests.get(METAR_URL, params=params, timeout=30)
    resp.raise_for_status()
    by_station = {}
    for ob in resp.json():
        icao = ob.get("icaoId") or ob.get("station_id")
        temp_c = ob.get("temp")
        if icao and temp_c is not None:
            by_station[icao] = round(temp_c * 9 / 5 + 32, 1)
    return by_station


# ---------------------------------------------------------------------------
# NWS gridpoint forecast — used as an HRRR-adjacent day-of confirmation
# signal. NOTE: this is the NWS forecaster/model-blended gridpoint value,
# NOT the raw HRRR grib field. Raw HRRR requires GRIB2 parsing (pygrib/
# cfgrib + multi-hundred-MB downloads) which isn't practical in a light
# GitHub Actions runner. This is a reasonable proxy, not the literal thing.
# ---------------------------------------------------------------------------

def fetch_gridpoint_max_temp_f(lat: float, lon: float) -> float | None:
    try:
        points_resp = requests.get(
            f"https://api.weather.gov/points/{lat},{lon}",
            headers={"User-Agent": "weather-signal-bot (personal use)"},
            timeout=20,
        )
        points_resp.raise_for_status()
        forecast_url = points_resp.json()["properties"]["forecast"]

        fc_resp = requests.get(
            forecast_url,
            headers={"User-Agent": "weather-signal-bot (personal use)"},
            timeout=20,
        )
        fc_resp.raise_for_status()
        periods = fc_resp.json()["properties"]["periods"]
        for p in periods:
            if p.get("isDaytime"):
                return float(p["temperature"])
        return None
    except Exception as e:
        print(f"Gridpoint fetch failed for ({lat},{lon}): {e}")
        return None


# ---------------------------------------------------------------------------
# Polymarket (Gamma API) — website-resolution market
# ---------------------------------------------------------------------------

def build_event_slug(city_slug: str, target_date: datetime) -> str:
    month = target_date.strftime("%B").lower()
    day = target_date.day
    year = target_date.year
    return f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"


def fetch_market_by_slug(slug: str) -> dict | None:
    try:
        resp = requests.get(GAMMA_EVENTS_URL, params={"slug": slug}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        return None
    except Exception as e:
        print(f"Gamma fetch failed for slug={slug}: {e}")
        return None


BUCKET_RE = re.compile(r"(\d+)\s*-\s*(\d+)")


def parse_outcomes(event: dict):
    """
    Returns a list of (label, low_f, high_f, price) for each outcome bucket
    in the event's first market. Skips "or below"/"or above" catch-all bins
    since they don't have a clean two-sided numeric range.
    """
    if not event or not event.get("markets"):
        return []
    market = event["markets"][0]
    outcomes = market.get("outcomes")
    prices = market.get("outcomePrices")
    if isinstance(outcomes, str):
        import json as _json
        outcomes = _json.loads(outcomes)
    if isinstance(prices, str):
        import json as _json
        prices = _json.loads(prices)
    if not outcomes or not prices:
        return []

    parsed = []
    for label, price in zip(outcomes, prices):
        m = BUCKET_RE.search(label)
        if m:
            lo, hi = float(m.group(1)), float(m.group(2))
            parsed.append((label, lo, hi, float(price)))
    return parsed


def find_bucket_for_temp(outcomes, temp_f: float):
    for label, lo, hi, price in outcomes:
        if lo <= temp_f <= hi:
            return label, lo, hi, price
    return None