"""
data_sources.py — all external data pulls, kept separate from scoring logic
so each fetcher can be tested/swapped independently.
"""

import json
import re
from datetime import datetime, timezone, timedelta

import requests

from config import ALL_STATIONS, STATION_NETWORK

NBM_TEXT_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod"
METAR_URL = "https://aviationweather.gov/api/data/metar"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
IEM_DAILY_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/daily.py"

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
# Actual observed daily high (IEM ASOS archive) -- used to check forecast
# bias per city (e.g. "does MIA's TXN run hot vs. what actually happens").
# This is INDEPENDENT of how Polymarket resolves the bucket -- it's the
# real observed max temp at the station for that calendar day, sourced
# from Iowa State's public ASOS daily-summary service. No API key needed.
# NOTE: this is a proxy for the "official" NWS CLI daily high -- in
# practice they agree the vast majority of the time, but on rare days
# they can differ by a degree due to rounding/QC differences. Treat this
# as "close enough for bias-trend analysis," not as ground truth for
# individual settlement disputes.
# ---------------------------------------------------------------------------

def fetch_actual_high(station: str, target_date) -> float | None:
    network = STATION_NETWORK.get(station)
    if not network:
        print(f"No IEM network mapping for station {station}, skipping actual-high fetch.")
        return None
    # IEM's station tables use the 3-char FAA identifier (e.g. "LAX"), not
    # the 4-char ICAO identifier with the "K" prefix used everywhere else
    # in this bot (METAR, NBM, gridpoint). Strip it for this one call.
    iem_station = station[1:] if len(station) == 4 and station.startswith("K") else station
    date_str = target_date.isoformat() if hasattr(target_date, "isoformat") else str(target_date)
    params = {
        "stations": iem_station,
        "network": network,
        "sts": date_str,
        "ets": date_str,
        "var": "max_temp_f",
        "format": "csv",
    }
    try:
        resp = requests.get(IEM_DAILY_URL, params=params, timeout=30)
        resp.raise_for_status()
        lines = [l for l in resp.text.strip().splitlines() if l and not l.startswith("#")]
        if len(lines) < 2:
            print(f"IEM returned no data row for {iem_station} ({network}) on {date_str}. "
                  f"Raw response: {resp.text[:200]!r}")
            return None
        header = lines[0].split(",")
        row = lines[1].split(",")
        idx = header.index("max_temp_f")
        val = row[idx].strip()
        if val in ("", "M", "None"):
            print(f"IEM has no max_temp_f value for {iem_station} on {date_str} (got {val!r}).")
            return None
        return float(val)
    except Exception as e:
        print(f"Actual-high fetch failed for {iem_station} {date_str}: {e}")
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
    """Returns the NARROWEST matching bucket, not just the first one.
    Open-ended buckets (e.g. "<=73F") use a deliberately wide sentinel
    range internally so this lo<=temp<=hi check still works -- but that
    means a wide catch-all bucket can accidentally match before a
    tighter, more specific bucket does, if it happens to appear earlier
    in the list. Preferring the narrowest match fixes that."""
    matches = [(label, lo, hi, price) for label, lo, hi, price in outcomes if lo <= temp_f <= hi]
    if not matches:
        return None
    return min(matches, key=lambda m: m[2] - m[1])


# ---------------------------------------------------------------------------
# Polymarket US (the app) — gateway.polymarket.us, PUBLIC, no auth needed.
# Confirmed working 2026-07-14 via manual diagnostic. Settlement source is
# the NWS Daily Climate Report (CLI) per docs.polymarket.us/faqs/weather-
# faqs, at 8am ET the day after the contract date (same timing already
# used for check-outcomes.yml).
# ---------------------------------------------------------------------------

POLYMARKET_US_GATEWAY = "https://gateway.polymarket.us"

# Matches phrasing like "less than or equal to 89F" or "between 92F and 93F"
# in each market's description field -- this is the only place bucket
# bounds are stated in this API (unlike Gamma, there's no separate
# groupItemTitle to parse instead).
_US_RANGE_RE = re.compile(r"between (\d+)F and (\d+)F")
_US_LTE_RE = re.compile(r"less than or equal to (\d+)F")
_US_GTE_RE = re.compile(r"greater than or equal to (\d+)F")


def build_polymarket_us_slug(us_station_slug: str, target_date) -> str:
    date_str = target_date.isoformat() if hasattr(target_date, "isoformat") else str(target_date)
    return f"temp-{us_station_slug}high-{date_str}"


def fetch_polymarket_us_event(slug: str) -> dict | None:
    try:
        resp = requests.get(f"{POLYMARKET_US_GATEWAY}/v1/events/slug/{slug}", timeout=20)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("event")
    except Exception as e:
        print(f"Polymarket US event fetch failed for slug={slug}: {e}")
        return None


def parse_polymarket_us_outcomes(event: dict):
    """
    Same return shape as parse_outcomes() (Gamma/website side): a list of
    (label, low_f, high_f, yes_price) tuples, so both sources can share
    find_bucket_for_temp() and downstream scoring/checking logic.

    Open-ended buckets ("less than or equal to X", "greater than or equal
    to X") get a wide sentinel bound on the open side so find_bucket_for_temp
    still works with a simple lo <= temp <= hi check.
    """
    if not event or not event.get("markets"):
        return []

    parsed = []
    for market in event["markets"]:
        desc = market.get("description", "")

        m = _US_RANGE_RE.search(desc)
        if m:
            lo, hi = float(m.group(1)), float(m.group(2))
        else:
            m = _US_LTE_RE.search(desc)
            if m:
                lo, hi = -200.0, float(m.group(1))
            else:
                m = _US_GTE_RE.search(desc)
                if m:
                    lo, hi = float(m.group(1)), 300.0
                else:
                    continue  # unrecognized description format, skip

        yes_price = None
        for side in market.get("marketSides", []):
            if side.get("description", "").strip().lower() == "yes":
                try:
                    yes_price = float(side["price"])
                except (KeyError, ValueError, TypeError):
                    pass
                break
        if yes_price is None:
            continue

        # Use a clean label: normal range for closed buckets, readable
        # threshold notation for open-ended ones -- not a truncated raw
        # description, which used to cut off mid-sentence.
        if lo > -200 and hi < 300:
            label = f"{lo:.0f}-{hi:.0f}°F"
        elif hi < 300:
            label = f"<={hi:.0f}°F"
        else:
            label = f">={lo:.0f}°F"
        parsed.append((label, lo, hi, yes_price))
    return parsed
