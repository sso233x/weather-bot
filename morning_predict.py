#!/usr/bin/env python3
"""
morning_predict.py

Independent morning-of temperature predictor.

Purpose:
    Determine the most probable official daily high for KMIA using:
      - Latest available NBM TXN/XND
      - NWS hourly forecast
      - Current KMIA observation
      - Recent KMIA temperature trend
      - Nearby South Florida observations
      - Historical outcomes from trade_log.csv
      - Learned historical NBM bias/sigma
      - Current Polymarket website/app prices

IMPORTANT:
    The weather probability is calculated independently of market prices.
    Market prices are only used AFTER the weather forecast is produced
    to determine whether an apparent trading edge exists.

This is intentionally separate from the existing scoring.py system so
the original bot remains untouched while this model is tested.
"""

import csv
import html
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import requests

from config import CITIES, US_STATION_SLUG
from data_sources import (
    fetch_all_nbm,
    extract_max_for_date,
    parse_bulletin_issue_time,
    build_event_slug,
    fetch_market_by_slug,
    parse_outcomes,
    build_polymarket_us_slug,
    fetch_polymarket_us_event,
    parse_polymarket_us_outcomes,
)
from scoring import get_txn_bias, get_sigma


ET = ZoneInfo("America/New_York")

NWS_HEADERS = {
    "User-Agent": "weather-signal-bot morning predictor",
    "Accept": "application/geo+json",
}

AWC_METAR_URL = "https://aviationweather.gov/api/data/metar"

TRADE_LOG = os.path.join(os.path.dirname(__file__), "trade_log.csv")

# KMIA plus the most useful nearby South Florida airport observations.
# These are supporting observations, NOT settlement stations.
MIA_REGION_STATIONS = [
    "KMIA",
    "KFLL",
    "KHWO",
    "KOPF",
    "KTMB",
]

# Probability mixture.
# Historical analogs are intentionally the strongest component because
# they are based on actual outcomes from this bot.
HIST_WEIGHT = 0.55
NWS_WEIGHT = 0.30
NBM_WEIGHT = 0.15

# Minimum historical analog count before we trust the analog component fully.
MIN_ANALOGS = 8

# Number of historical observations to use at most.
MAX_ANALOGS = 30


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def clamp(value, low, high):
    return max(low, min(high, value))


def gaussian_pdf(x, mu, sigma):
    if sigma <= 0:
        return 1.0 if abs(x - mu) < 0.5 else 0.0
    z = (x - mu) / sigma
    return math.exp(-0.5 * z * z) / (sigma * math.sqrt(2 * math.pi))


def normalize_distribution(dist):
    total = sum(dist.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in dist.items()}


def nearest_int(value):
    return int(math.floor(value + 0.5))


def circular_day_distance(doy1, doy2):
    """
    Distance between two day-of-year values, accounting for Dec/Jan wrap.
    """
    a = abs(doy1 - doy2)
    return min(a, 366 - a)


def safe_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Historical data
# ---------------------------------------------------------------------------

def load_historical_mia():
    """
    Load historical KMIA predictions/outcomes.

    trade_log.csv contains repeated observations of the same target date
    because the bot can check a market multiple times.

    We deduplicate by target_date and keep the latest logged row for that
    date so a single day cannot artificially count 10x more than another.
    """
    if not os.path.exists(TRADE_LOG):
        return []

    latest_by_date = {}

    with open(TRADE_LOG, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            if row.get("city") != "MIA":
                continue

            target = row.get("target_date")
            actual = safe_float(row.get("actual_high"))
            txn = safe_float(row.get("txn"))
            xnd = safe_float(row.get("xnd"))

            if not target or actual is None or txn is None:
                continue

            try:
                target_date = date.fromisoformat(target)
            except ValueError:
                continue

            logged_at = row.get("logged_at", "")

            # Keep the latest record for each target date.
            existing = latest_by_date.get(target)

            if existing is None or logged_at > existing.get("logged_at", ""):
                latest_by_date[target] = {
                    "target_date": target_date,
                    "actual": actual,
                    "txn": txn,
                    "xnd": xnd,
                    "logged_at": logged_at,
                }

    return sorted(
        latest_by_date.values(),
        key=lambda x: x["target_date"]
    )


def build_historical_analogs(
    history,
    target_date,
    current_txn,
    current_xnd,
):
    """
    Find historical KMIA days that most resemble today's setup.

    Features currently available historically:
      - NBM TXN
      - XND
      - calendar position

    We deliberately do NOT use information that wasn't recorded historically.
    This avoids look-ahead/data leakage.

    Returns weighted historical actual highs.
    """
    if not history or current_txn is None:
        return []

    candidates = []

    target_doy = target_date.timetuple().tm_yday

    for row in history:
        hist_date = row["target_date"]

        # Never use future information.
        if hist_date >= target_date:
            continue

        txn_diff = abs(row["txn"] - current_txn)

        # XND is useful but much less important than TXN proximity.
        if current_xnd is None or row["xnd"] is None:
            xnd_diff = 1.0
        else:
            xnd_diff = abs(row["xnd"] - current_xnd)

        hist_doy = hist_date.timetuple().tm_yday
        seasonal_diff = circular_day_distance(target_doy, hist_doy)

        # Main similarity score.
        #
        # TXN gets the strongest weight because it is the direct NBM
        # forecast of the high.
        #
        # XND matters because it tells us how uncertain that forecast was.
        #
        # Seasonality prevents January-style situations from being treated
        # as identical to late-summer situations.
        distance = (
            txn_diff
            + 1.5 * xnd_diff
            + 0.035 * seasonal_diff
        )

        candidates.append((distance, row))

    candidates.sort(key=lambda x: x[0])
    candidates = candidates[:MAX_ANALOGS]

    analogs = []

    for distance, row in candidates:
        # Exponential weighting gives very close analogs much more influence.
        weight = math.exp(-distance / 2.0)

        analogs.append({
            "actual": row["actual"],
            "txn": row["txn"],
            "xnd": row["xnd"],
            "date": row["target_date"],
            "distance": distance,
            "weight": weight,
        })

    return analogs


def historical_distribution(analogs):
    """
    Convert historical analog outcomes into an empirical probability
    distribution.

    We use actual integer highs directly rather than forcing them through
    a Gaussian curve.
    """
    if not analogs:
        return {}

    dist = defaultdict(float)

    for analog in analogs:
        actual = nearest_int(analog["actual"])
        dist[actual] += analog["weight"]

    return normalize_distribution(dist)


# ---------------------------------------------------------------------------
# NWS
# ---------------------------------------------------------------------------

def nws_points(lat, lon):
    url = f"https://api.weather.gov/points/{lat},{lon}"

    resp = requests.get(
        url,
        headers=NWS_HEADERS,
        timeout=20,
    )
    resp.raise_for_status()

    return resp.json()["properties"]


def fetch_nws_hourly(lat, lon, target_date):
    """
    Fetch the NWS hourly forecast and return today's forecast temperatures.

    NWS provides hourly forecast data through the gridpoint forecast/hourly
    endpoint discovered from /points.
    """
    props = nws_points(lat, lon)

    url = props.get("forecastHourly")

    if not url:
        return []

    resp = requests.get(
        url,
        headers=NWS_HEADERS,
        timeout=20,
    )
    resp.raise_for_status()

    periods = resp.json()["properties"]["periods"]

    values = []

    for p in periods:
        try:
            start = datetime.fromisoformat(
                p["startTime"].replace("Z", "+00:00")
            ).astimezone(ET)

            if start.date() != target_date:
                continue

            temp = safe_float(p.get("temperature"))

            if temp is None:
                continue

            values.append({
                "time": start,
                "temp": temp,
                "wind": p.get("windSpeed"),
                "wind_direction": p.get("windDirection"),
                "short_forecast": p.get("shortForecast"),
                "dewpoint": p.get("dewpoint"),
                "relative_humidity": p.get("relativeHumidity"),
            })

        except Exception:
            continue

    return values


def nws_forecast_high(hourly):
    """
    Highest NWS hourly temperature forecast remaining for the day.
    """
    if not hourly:
        return None

    return max(x["temp"] for x in hourly)


# ---------------------------------------------------------------------------
# Current / nearby observations
# ---------------------------------------------------------------------------

def fetch_recent_metars():
    """
    Fetch the latest several hours of METAR observations for KMIA and nearby
    South Florida airports.

    AviationWeather provides current METAR observations in JSON.
    """
    params = {
        "ids": ",".join(MIA_REGION_STATIONS),
        "hours": 6,
        "format": "json",
        "taf": "false",
    }

    resp = requests.get(
        AWC_METAR_URL,
        params=params,
        timeout=30,
    )
    resp.raise_for_status()

    observations = resp.json()

    by_station = defaultdict(list)

    for ob in observations:
        station = ob.get("icaoId") or ob.get("station_id")

        if station not in MIA_REGION_STATIONS:
            continue

        temp_c = safe_float(ob.get("temp"))

        if temp_c is None:
            continue

        temp_f = temp_c * 9 / 5 + 32

        dewp_c = safe_float(ob.get("dewp"))
        dewp_f = (
            dewp_c * 9 / 5 + 32
            if dewp_c is not None
            else None
        )

        obs_time = ob.get("obsTime")

        by_station[station].append({
            "time": obs_time,
            "temp": temp_f,
            "dewpoint": dewp_f,
            "wind_dir": ob.get("wdir"),
            "wind_speed": ob.get("wspd"),
            "gust": ob.get("wgst"),
            "weather": ob.get("wxString"),
            "clouds": ob.get("clouds"),
        })

    for station in by_station:
        by_station[station].sort(
            key=lambda x: x.get("time") or "",
            reverse=True
        )

    return by_station


def current_kmia_observation(metars):
    rows = metars.get("KMIA", [])

    if not rows:
        return None

    return rows[0]


def kmia_recent_trend(metars):
    """
    Estimate recent KMIA warming/cooling trend in °F per hour.

    We only use observations from the current day where possible.
    """
    rows = metars.get("KMIA", [])

    if len(rows) < 2:
        return None

    parsed = []

    for row in rows:
        if not row.get("time"):
            continue

        try:
            t = datetime.fromisoformat(
                row["time"].replace("Z", "+00:00")
            )

            parsed.append((t, row["temp"]))

        except Exception:
            continue

    if len(parsed) < 2:
        return None

    parsed.sort()

    t1, temp1 = parsed[-1]
    t0, temp0 = parsed[0]

    hours = (t1 - t0).total_seconds() / 3600

    if hours <= 0:
        return None

    return (temp1 - temp0) / hours


def nearby_temperature_signal(metars):
    """
    Compare current nearby South Florida temperatures with KMIA.

    This is a weak supporting signal, not a direct forecast.
    """
    mia = current_kmia_observation(metars)

    if mia is None:
        return None

    nearby = []

    for station, rows in metars.items():
        if station == "KMIA" or not rows:
            continue

        nearby.append(rows[0]["temp"])

    if not nearby:
        return None

    avg = sum(nearby) / len(nearby)

    return {
        "avg": avg,
        "delta_vs_kmia": avg - mia["temp"],
        "stations": len(nearby),
    }


# ---------------------------------------------------------------------------
# Probability distributions
# ---------------------------------------------------------------------------

def normal_integer_distribution(mu, sigma, low=70, high=110):
    """
    Convert a continuous normal distribution into integer-degree probabilities.

    Each integer gets probability mass from [T-0.5, T+0.5].
    """
    if mu is None:
        return {}

    sigma = max(float(sigma), 0.75)

    raw = {}

    for temp in range(low, high + 1):
        upper = (temp + 0.5 - mu) / sigma
        lower = (temp - 0.5 - mu) / sigma

        cdf_upper = 0.5 * (1 + math.erf(upper / math.sqrt(2)))
        cdf_lower = 0.5 * (1 + math.erf(lower / math.sqrt(2)))

        raw[temp] = max(0.0, cdf_upper - cdf_lower)

    return normalize_distribution(raw)


def shift_distribution(dist, shift):
    """
    Shift an integer distribution by a fractional number of degrees using
    linear interpolation.
    """
    if not dist:
        return {}

    result = defaultdict(float)

    for temp, prob in dist.items():
        target = temp + shift

        low = math.floor(target)
        high = low + 1

        frac = target - low

        result[low] += prob * (1 - frac)
        result[high] += prob * frac

    return normalize_distribution(result)


def combine_distributions(distributions):
    """
    Combine weighted probability distributions.
    """
    result = defaultdict(float)

    for dist, weight in distributions:
        if not dist or weight <= 0:
            continue

        for temp, prob in dist.items():
            result[temp] += weight * prob

    return normalize_distribution(result)


def apply_current_temp_constraint(dist, current_temp):
    """
    The official daily high cannot be lower than the current observed
    temperature.

    We don't force a hard 100% cutoff because observation data can have
    small timing/QC differences, but temperatures substantially below the
    current observation are removed.
    """
    if not dist or current_temp is None:
        return dist

    result = {}

    floor_temp = math.floor(current_temp)

    for temp, prob in dist.items():
        if temp >= floor_temp:
            result[temp] = prob
        else:
            # Keep a tiny amount rather than creating a mathematical
            # impossibility from potentially delayed observations.
            result[temp] = prob * 0.01

    return normalize_distribution(result)


# ---------------------------------------------------------------------------
# Main weather model
# ---------------------------------------------------------------------------

def build_weather_prediction(
    target_date,
    raw_txn,
    xnd,
    nws_high,
    current_obs,
    trend,
    nearby_signal,
    analogs,
    sigma,
):
    """
    Build the final independent weather distribution.

    Components:

      1. Historical analog distribution
         Actual outcomes from similar historical NBM situations.

      2. NWS distribution
         Deterministic NWS hourly forecast treated as a forecast center
         with moderate uncertainty.

      3. NBM distribution
         Existing learned NBM error distribution.

    Current observations are used as constraints and a weak regional
    adjustment, rather than being allowed to overwhelm the forecast.
    """

    bias = get_txn_bias("MIA")

    corrected_txn = None

    if raw_txn is not None:
        corrected_txn = raw_txn - bias

    # Weak regional adjustment.
    #
    # Example:
    # KMIA = 88
    # nearby average = 89
    # regional signal = +1
    #
    # We only apply 25% of the difference so a single regional snapshot
    # cannot overpower NWS/NBM.
    regional_adjustment = 0.0

    if nearby_signal is not None:
        regional_adjustment = clamp(
            nearby_signal["delta_vs_kmia"] * 0.25,
            -0.75,
            0.75,
        )

    # A rapidly warming KMIA morning can justify a small upward adjustment,
    # but only when NWS itself isn't already accounting for it.
    trend_adjustment = 0.0

    if trend is not None:
        if trend > 1.5:
            trend_adjustment = 0.35
        elif trend > 0.75:
            trend_adjustment = 0.15
        elif trend < -1.0:
            trend_adjustment = -0.20

    observation_adjustment = regional_adjustment + trend_adjustment

    # Historical analog distribution.
    hist_dist = historical_distribution(analogs)

    # If there aren't many analogs, reduce their weight and let the
    # established NBM/NWS information dominate.
    if len(analogs) >= MIN_ANALOGS:
        hist_weight = HIST_WEIGHT
        nws_weight = NWS_WEIGHT
        nbm_weight = NBM_WEIGHT
    else:
        hist_weight = 0.35
        nws_weight = 0.40
        nbm_weight = 0.25

    distributions = []

    if hist_dist:
        hist_dist = shift_distribution(
            hist_dist,
            observation_adjustment,
        )
        distributions.append((hist_dist, hist_weight))

    if nws_high is not None:
        nws_sigma = max(1.5, sigma * 0.75)

        nws_dist = normal_integer_distribution(
            nws_high + observation_adjustment,
            nws_sigma,
        )

        distributions.append((nws_dist, nws_weight))

    if corrected_txn is not None:
        nbm_dist = normal_integer_distribution(
            corrected_txn + observation_adjustment,
            sigma,
        )

        distributions.append((nbm_dist, nbm_weight))

    final_dist = combine_distributions(distributions)

    # Current temperature is an important day-of constraint.
    if current_obs is not None:
        final_dist = apply_current_temp_constraint(
            final_dist,
            current_obs.get("temp"),
        )

    return {
        "distribution": final_dist,
        "corrected_txn": corrected_txn,
        "regional_adjustment": regional_adjustment,
        "trend_adjustment": trend_adjustment,
        "analog_count": len(analogs),
        "hist_weight": hist_weight,
        "nws_weight": nws_weight,
        "nbm_weight": nbm_weight,
    }


# ---------------------------------------------------------------------------
# Polymarket
# ---------------------------------------------------------------------------

def bucket_probability_from_distribution(
    distribution,
    lo,
    hi,
):
    total = 0.0

    for temp, probability in distribution.items():
        if lo <= temp <= hi:
            total += probability

    return total


def market_analysis(distribution, target_date):
    """
    Compare the independent weather model with current website/app prices.

    IMPORTANT:
        Market prices do not modify the weather distribution.
    """
    result = {
        "website": [],
        "app": [],
    }

    city = CITIES["MIA"]

    # Website
    try:
        slug = build_event_slug(
            city["slug"],
            target_date,
        )

        event = fetch_market_by_slug(slug)
        outcomes = parse_outcomes(event) if event else []

        for label, lo, hi, price in outcomes:
            model_prob = bucket_probability_from_distribution(
                distribution,
                lo,
                hi,
            )

            result["website"].append({
                "label": label,
                "price": price,
                "model_prob": model_prob,
                "edge": model_prob - price,
            })

    except Exception as e:
        print(f"Website market analysis failed: {e}")

    # App
    try:
        app_slug = build_polymarket_us_slug(
            US_STATION_SLUG["MIA"],
            target_date,
        )

        app_event = fetch_polymarket_us_event(app_slug)
        app_outcomes = (
            parse_polymarket_us_outcomes(app_event)
            if app_event
            else []
        )

        for label, lo, hi, price in app_outcomes:
            model_prob = bucket_probability_from_distribution(
                distribution,
                lo,
                hi,
            )

            result["app"].append({
                "label": label,
                "price": price,
                "model_prob": model_prob,
                "edge": model_prob - price,
            })

    except Exception as e:
        print(f"App market analysis failed: {e}")

    return result


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def top_predictions(distribution, n=5):
    return sorted(
        distribution.items(),
        key=lambda x: x[1],
        reverse=True,
    )[:n]


def best_market_edge(rows):
    valid = [
        r for r in rows
        if r["price"] is not None
        and 0.05 <= r["price"] <= 0.95
    ]

    if not valid:
        return None

    return max(
        valid,
        key=lambda r: r["edge"],
    )


def confidence_label(probability, analog_count):
    if probability >= 0.60 and analog_count >= MIN_ANALOGS:
        return "STRONG"

    if probability >= 0.50:
        return "GOOD"

    if probability >= 0.40:
        return "LEAN"

    return "LOW"


def format_report(
    target_date,
    now_et,
    raw_txn,
    xnd,
    nws_high,
    current_obs,
    trend,
    nearby_signal,
    analogs,
    model,
    market,
):
    dist = model["distribution"]

    top = top_predictions(dist, 5)

    if not top:
        return "KMIA MORNING PREDICTION\n\nUnable to build a probability distribution."

    best_temp, best_prob = top[0]

    label = confidence_label(
        best_prob,
        len(analogs),
    )

    lines = []

    lines.append(
        f"KMIA MORNING WEATHER PREDICTION — {target_date}"
    )
    lines.append(
        f"Run: {now_et.strftime('%I:%M %p ET').lstrip('0')}"
    )
    lines.append("")

    lines.append(
        f"MOST PROBABLE HIGH: {best_temp}°F ({best_prob:.0%})"
    )
    lines.append(
        f"Confidence: {label}"
    )

    if best_prob >= 0.50:
        lines.append("Strongest outcome is above 50%.")
    else:
        lines.append(
            "No outcome exceeds 50% — do not treat this as a high-confidence call."
        )

    lines.append("")

    lines.append("TOP TEMPERATURES:")

    for temp, prob in top:
        lines.append(
            f"  {temp}°F — {prob:.1%}"
        )

    lines.append("")

    lines.append("WEATHER INPUTS:")

    if raw_txn is not None:
        lines.append(
            f"  NBM TXN: {raw_txn:.0f}°F"
        )

    if model["corrected_txn"] is not None:
        lines.append(
            f"  Bias-corrected NBM: {model['corrected_txn']:.1f}°F"
        )

    if xnd is not None:
        lines.append(
            f"  NBM XND: {xnd}"
        )

    if nws_high is not None:
        lines.append(
            f"  NWS hourly forecast high: {nws_high:.0f}°F"
        )

    if current_obs is not None:
        lines.append(
            f"  KMIA now: {current_obs['temp']:.1f}°F"
        )

        if current_obs.get("dewpoint") is not None:
            lines.append(
                f"  KMIA dewpoint: {current_obs['dewpoint']:.1f}°F"
            )

        if current_obs.get("wind_speed") is not None:
            lines.append(
                f"  KMIA wind: {current_obs['wind_speed']} kt"
            )

    if trend is not None:
        lines.append(
            f"  KMIA recent temp trend: {trend:+.2f}°F/hr"
        )

    if nearby_signal is not None:
        lines.append(
            f"  Nearby station average: {nearby_signal['avg']:.1f}°F "
            f"({nearby_signal['delta_vs_kmia']:+.1f}° vs KMIA)"
        )

    lines.append("")

    lines.append(
        f"HISTORICAL ANALOGS: {len(analogs)}"
    )

    if len(analogs) >= MIN_ANALOGS:
        lines.append(
            "  Historical analog component: FULL WEIGHT"
        )
    else:
        lines.append(
            "  Historical analog component: REDUCED WEIGHT "
            "(not enough similar days)"
        )

    if analogs:
        analog_outcomes = defaultdict(float)

        for a in analogs:
            analog_outcomes[
                nearest_int(a["actual"])
            ] += a["weight"]

        analog_outcomes = normalize_distribution(
            analog_outcomes
        )

        analog_top = sorted(
            analog_outcomes.items(),
            key=lambda x: x[1],
            reverse=True,
        )[:3]

        lines.append("  Closest historical outcomes:")

        for temp, prob in analog_top:
            lines.append(
                f"    {temp}°F — {prob:.0%}"
            )

    lines.append("")

    lines.append("MODEL MIX:")

    lines.append(
        f"  Historical analogs: {model['hist_weight']:.0%}"
    )
    lines.append(
        f"  NWS hourly: {model['nws_weight']:.0%}"
    )
    lines.append(
        f"  NBM: {model['nbm_weight']:.0%}"
    )

    if model["regional_adjustment"] != 0:
        lines.append(
            f"  Regional adjustment: "
            f"{model['regional_adjustment']:+.2f}°F"
        )

    if model["trend_adjustment"] != 0:
        lines.append(
            f"  Trend adjustment: "
            f"{model['trend_adjustment']:+.2f}°F"
        )

    # Market section.
    lines.append("")
    lines.append("POLYMARKET — WEATHER MODEL VS MARKET:")

    website_best = best_market_edge(
        market["website"]
    )

    if website_best:
        lines.append(
            f"  Website best edge: "
            f"{website_best['label']} — "
            f"model {website_best['model_prob']:.1%} "
            f"vs market {website_best['price']:.1%} "
            f"({website_best['edge']:+.1%})"
        )
    else:
        lines.append(
            "  Website: no trustworthy priced bucket found."
        )

    app_best = best_market_edge(
        market["app"]
    )

    if app_best:
        lines.append(
            f"  App best edge: "
            f"{app_best['label']} — "
            f"model {app_best['model_prob']:.1%} "
            f"vs market {app_best['price']:.1%} "
            f"({app_best['edge']:+.1%})"
        )
    else:
        lines.append(
            "  App: no trustworthy priced bucket found."
        )

    lines.append("")
    lines.append(
        "IMPORTANT: market prices do NOT influence the weather probability."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print(message)
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # Telegram maximum is 4096 characters.
    for i in range(0, len(message), 3500):
        chunk = message[i:i + 3500]

        response = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": chunk,
            },
            timeout=20,
        )

        response.raise_for_status()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def get_latest_nbm_for_today(target_date):
    """
    Try the newest NBM cycles first.

    13Z is generally the most useful morning cycle once available.
    07Z is the preferred fallback for an early-morning run.
    01Z is the previous overnight cycle.
    """
    cycles = ["13", "07", "01"]

    for cycle in cycles:
        try:
            nbm_data = fetch_all_nbm(cycle)

            mia_block = nbm_data.get("KMIA", {}).get("block")

            if not mia_block:
                continue

            txn, xnd = extract_max_for_date(
                mia_block,
                target_date,
            )

            if txn is not None:
                issue = parse_bulletin_issue_time(
                    mia_block
                )

                return {
                    "txn": float(txn),
                    "xnd": xnd,
                    "cycle": cycle,
                    "issue": issue,
                }

        except Exception as e:
            print(
                f"NBM {cycle}Z unavailable: {e}"
            )

    return {
        "txn": None,
        "xnd": None,
        "cycle": None,
        "issue": None,
    }


def main():
    now_et = datetime.now(ET)
    target_date = now_et.date()

    print(
        f"Building KMIA morning prediction for {target_date}..."
    )

    # ------------------------------------------------------------
    # NBM
    # ------------------------------------------------------------

    nbm = get_latest_nbm_for_today(target_date)

    raw_txn = nbm["txn"]
    xnd = nbm["xnd"]

    print(
        f"NBM: TXN={raw_txn}, XND={xnd}, cycle={nbm['cycle']}"
    )

    # ------------------------------------------------------------
    # NWS hourly
    # ------------------------------------------------------------

    city = CITIES["MIA"]

    try:
        nws_hourly = fetch_nws_hourly(
            city["lat"],
            city["lon"],
            target_date,
        )

        nws_high = nws_forecast_high(
            nws_hourly
        )

    except Exception as e:
        print(
            f"NWS hourly forecast failed: {e}"
        )
        nws_hourly = []
        nws_high = None

    # ------------------------------------------------------------
    # Current + nearby observations
    # ------------------------------------------------------------

    try:
        metars = fetch_recent_metars()

        current_obs = current_kmia_observation(
            metars
        )

        trend = kmia_recent_trend(
            metars
        )

        nearby_signal = nearby_temperature_signal(
            metars
        )

    except Exception as e:
        print(
            f"METAR observation pull failed: {e}"
        )

        metars = {}
        current_obs = None
        trend = None
        nearby_signal = None

    # ------------------------------------------------------------
    # Historical analogs
    # ------------------------------------------------------------

    history = load_historical_mia()

    analogs = build_historical_analogs(
        history=history,
        target_date=target_date,
        current_txn=raw_txn,
        current_xnd=xnd,
    )

    print(
        f"Historical KMIA analogs: {len(analogs)}"
    )

    # ------------------------------------------------------------
    # Learned NBM sigma
    # ------------------------------------------------------------

    sigma, sigma_source = get_sigma(
        "MIA",
        xnd,
    )

    print(
        f"NBM sigma: {sigma:.2f}°F ({sigma_source})"
    )

    # ------------------------------------------------------------
    # Weather model
    # ------------------------------------------------------------

    model = build_weather_prediction(
        target_date=target_date,
        raw_txn=raw_txn,
        xnd=xnd,
        nws_high=nws_high,
        current_obs=current_obs,
        trend=trend,
        nearby_signal=nearby_signal,
        analogs=analogs,
        sigma=sigma,
    )

    distribution = model["distribution"]

    if not distribution:
        print(
            "ERROR: could not build weather probability distribution."
        )
        sys.exit(1)

    # ------------------------------------------------------------
    # Polymarket
    # ------------------------------------------------------------

    market = market_analysis(
        distribution,
        target_date,
    )

    # ------------------------------------------------------------
    # Final report
    # ------------------------------------------------------------

    report = format_report(
        target_date=target_date,
        now_et=now_et,
        raw_txn=raw_txn,
        xnd=xnd,
        nws_high=nws_high,
        current_obs=current_obs,
        trend=trend,
        nearby_signal=nearby_signal,
        analogs=analogs,
        model=model,
        market=market,
    )

    send_telegram(report)


if __name__ == "__main__":
    main()