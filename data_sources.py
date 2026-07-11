"""
data_sources.py — all external data pulls, kept separate from scoring logic
so each fetcher can be tested/swapped independently.
"""

import json
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
    Polymarket structures a multi-bucket temperature event as ONE event
    containing MULTIPLE markets -- one market per bucket, each phrased as
    its own Yes/No question (e.g. "Will LA's high be 76-77F? Yes/No").
    This is the same pattern Polymarket uses for any grouped/multi-outcome
    event (e.g. "who wins the election" = one market per candidate).

    Returns a list of (label, low_f, high_f, yes_price) for each bucket
    market found in the event.
    """
    if not event or not event.get("markets"):
        return []

    parsed = []
    for market in event["markets"]:
        label_source = market.get("groupItemTitle") or market.get("question", "")
        m = BUCKET_RE.search(label_source)
        if not m:
            continue
        lo, hi = float(m.group(1)), float(m.group(2))

        outcomes = market.get("outcomes")
        prices = market.get("outcomePrices")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(prices, str):
            prices = json.loads(prices)
        if not outcomes or not prices:
            continue

        yes_price = None
        for o, p in zip(outcomes, prices):
            if str(o).strip().lower() == "yes":
                yes_price = float(p)
                break
        if yes_price is None:
            yes_price = float(prices[0])

        parsed.append((label_source, lo, hi, yes_price))
    return parsed


def find_bucket_for_temp(outcomes, temp_f: float):
    for label, lo, hi, price in outcomes:
        if lo <= temp_f <= hi:
            return label, lo, hi, price
    return None