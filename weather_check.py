#!/usr/bin/env python3
"""
weather_check.py

Pulls NBM (National Blend of Models) text bulletins and current METAR
observations for a set of stations, extracts the signals you use for
Polymarket temperature markets (TXN, XND, current conditions), and
sends a summary to Telegram.

Free data sources, no API keys needed for weather data:
  - NBM text bulletins: https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod/
  - METAR obs:          https://aviationweather.gov/data/api/

Only Telegram needs credentials (also free) — see README.md.
"""

import os
import re
import sys
import requests

# ---------------------------------------------------------------------------
# CONFIG — edit this section for your cities and thresholds
# ---------------------------------------------------------------------------

STATIONS = {
    "KLAX": "LA",
    "KSFO": "SF",
    "KMIA": "Miami",
    "KNYC": "NYC",
    "KMDW": "Chicago",
}

# Which NBM cycle to pull. Public text bulletins are only reliably
# archived/served for 01, 07, 13, 19 UTC cycles.
NBM_CYCLE = os.environ.get("NBM_CYCLE", "01")

# Signal thresholds — tune these to match your methodology
XND_SKIP_THRESHOLD = 3       # skip / low-confidence if spread >= this
XND_GOOD_MAX = 2             # XND of 1-2 treated as a green light

NBM_TEXT_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod"
METAR_URL = "https://aviationweather.gov/api/data/metar"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


# ---------------------------------------------------------------------------
# NBM text bulletin fetch + parse
# ---------------------------------------------------------------------------

def fetch_nbm_bulletins(cycle):
    """
    Fetches the full NBM 'NBS' (short-range) bulk text file for the given
    UTC cycle from NOAA's public NOMADS server. This file contains ALL
    ~9000 NBM stations in one plain-text document — we filter down to our
    stations after downloading.

    URL pattern (confirmed against NOAA's own production data):
    https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod/
        blend.YYYYMMDD/HH/text/blend_nbstx.tHHz
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    ymd = now.strftime("%Y%m%d")
    url = f"{NBM_TEXT_BASE}/blend.{ymd}/{cycle}/text/blend_nbstx.t{cycle}z"

    resp = requests.get(url, timeout=60)
    if resp.status_code == 404:
        # Cycle for today may not be posted yet (e.g. running early, or
        # clock skew across UTC midnight) — fall back to yesterday's date.
        from datetime import timedelta
        ymd_prev = (now - timedelta(days=1)).strftime("%Y%m%d")
        url = f"{NBM_TEXT_BASE}/blend.{ymd_prev}/{cycle}/text/blend_nbstx.t{cycle}z"
        resp = requests.get(url, timeout=60)

    print(f"Fetched NBM data from: {url} (status {resp.status_code})")
    resp.raise_for_status()
    return resp.text


def split_by_station(raw_text, stations):
    """
    NBM bulk text responses concatenate one block per station, each
    starting with a header line like:
        KLAX NBM V4.1 NBS GUIDANCE ...
    This splits the raw text into {station: block_text}.
    """
    blocks = {}
    pattern = re.compile(
        r"^(" + "|".join(stations) + r")\s+NBM.*?(?=^(?:" + "|".join(stations) + r")\s+NBM|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    for match in pattern.finditer(raw_text):
        station = match.group(1)
        blocks[station] = match.group(0)
    return blocks


def extract_row(block_text, row_label):
    """
    Pulls the numeric tokens off a labeled row (e.g. 'TXN', 'XND') in
    an NBM text block. Returns a list of ints in left-to-right order
    (i.e. chronological). Missing values in NBM text are usually
    blank/space-padded, so only real values are returned.
    """
    for line in block_text.splitlines():
        if line.strip().startswith(row_label):
            tokens = line.strip().split()[1:]  # drop the row label itself
            values = []
            for t in tokens:
                try:
                    values.append(int(t))
                except ValueError:
                    continue
            return values
    return []


def parse_station_signals(block_text):
    return {
        "TXN": extract_row(block_text, "TXN"),
        "XND": extract_row(block_text, "XND"),
    }


# ---------------------------------------------------------------------------
# METAR fetch
# ---------------------------------------------------------------------------

def fetch_metar(stations):
    params = {
        "ids": ",".join(stations),
        "format": "json",
        "taf": "false",
    }
    resp = requests.get(METAR_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    by_station = {}
    for ob in data:
        icao = ob.get("icaoId") or ob.get("station_id")
        temp_c = ob.get("temp")
        if icao and temp_c is not None:
            by_station[icao] = round(temp_c * 9 / 5 + 32, 1)  # C -> F
    return by_station


# ---------------------------------------------------------------------------
# Signal evaluation
# ---------------------------------------------------------------------------

def evaluate_station(station, signals, metar_temp_f):
    txn = signals["TXN"]
    xnd = signals["XND"]

    next_txn = txn[0] if txn else None
    next_xnd = xnd[0] if xnd else None

    flags = []
    if next_xnd is not None:
        if next_xnd >= XND_SKIP_THRESHOLD:
            flags.append(f"⚠️ XND={next_xnd} → SKIP (low confidence)")
        elif next_xnd <= XND_GOOD_MAX:
            flags.append(f"✅ XND={next_xnd} (tight spread)")

    return {
        "station": station,
        "next_txn": next_txn,
        "next_xnd": next_xnd,
        "metar_temp_f": metar_temp_f,
        "flags": flags,
    }


# ---------------------------------------------------------------------------
# Telegram notification
# ---------------------------------------------------------------------------

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials not set — printing to console instead.\n")
        print(message)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        },
        timeout=15,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    try:
        raw = fetch_nbm_bulletins(NBM_CYCLE)
        blocks = split_by_station(raw, STATIONS.keys())
    except Exception as e:
        send_telegram(f"⚠️ NBM pull failed: {e}")
        sys.exit(1)

    if not blocks:
        # Nothing matched — send back a preview of what we actually got
        # so we can diagnose (wrong format, error page, empty file, etc.)
        preview = raw[:600].replace("<", "&lt;").replace(">", "&gt;")
        send_telegram(
            f"⚠️ NBM fetch succeeded ({len(raw)} chars) but no station "
            f"blocks matched.\n\nFirst 600 chars:\n<pre>{preview}</pre>"
        )
        sys.exit(1)

    try:
        metar_temps = fetch_metar(STATIONS.keys())
    except Exception as e:
        print(f"METAR pull failed (non-fatal): {e}")
        metar_temps = {}

    lines = [f"<b>Weather check — NBM {NBM_CYCLE}Z cycle</b>"]
    for station, city in STATIONS.items():
        block = blocks.get(station)
        if not block:
            lines.append(f"\n<b>{city} ({station})</b>: no NBM data found")
            continue

        signals = parse_station_signals(block)
        result = evaluate_station(station, signals, metar_temps.get(station))

        lines.append(f"\n<b>{city} ({station})</b>")
        lines.append(f"  Next TXN (forecast high): {result['next_txn']}°F")
        lines.append(f"  XND (spread): {result['next_xnd']}")
        if result["metar_temp_f"] is not None:
            lines.append(f"  Current METAR temp: {result['metar_temp_f']}°F")
        for flag in result["flags"]:
            lines.append(f"  {flag}")

    message = "\n".join(lines)
    send_telegram(message)
    print(message)


if __name__ == "__main__":
    main()
