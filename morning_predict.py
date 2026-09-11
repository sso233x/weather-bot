#!/usr/bin/env python3
"""
morning_predict.py

Independent morning-of KMIA temperature predictor.

The weather model is built from:
- same-morning historical KMIA analogs from kmia_history.py
- the latest NWS hourly forecast
- the latest NBM TXN/XND
- the current KMIA observation

Market prices are checked only after the weather distribution is built.
They never influence the weather probabilities.

For the primary betting prediction, the bot uses ONLY the bucket
options actually available through the Polymarket app.

Prediction tracking is passive and does not influence the weather model.
"""

import html
import math
import os
import sys
from collections import defaultdict
from datetime import datetime
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
    fetch_actual_high,
)
from kmia_history import get_historical_morning_model
from scoring import get_txn_bias, get_sigma
from prediction_tracker import (
    record_prediction,
    resolve_pending_predictions,
    format_tracking_summary,
    load_history,
)


ET = ZoneInfo("America/New_York")

NWS_HEADERS = {
    "User-Agent": "weather-signal-bot morning predictor",
    "Accept": "application/geo+json",
}

AWC_METAR_URL = "https://aviationweather.gov/api/data/metar"

MIA_REGION_STATIONS = [
    "KMIA",
    "KFLL",
    "KHWO",
    "KOPF",
    "KTMB",
]

# Simple model weights. Historical same-morning outcomes get the most weight
# because they condition directly on what KMIA is actually doing this morning.
HIST_WEIGHT = 0.50
NWS_WEIGHT = 0.30
NBM_WEIGHT = 0.20

MIN_ANALOGS = 8


def clamp(value, low, high):
    return max(low, min(high, value))


def safe_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_distribution(dist):
    total = sum(dist.values())

    if total <= 0:
        return {}

    return {
        int(k): v / total
        for k, v in dist.items()
        if v > 0
    }


def nearest_int(value):
    return int(math.floor(value + 0.5))


# ---------------------------------------------------------------------------
# NWS
# ---------------------------------------------------------------------------

def nws_points(lat, lon):
    url = f"https://api.weather.gov/points/{lat},{lon}"

    response = requests.get(
        url,
        headers=NWS_HEADERS,
        timeout=20,
    )

    response.raise_for_status()

    return response.json()["properties"]


def fetch_nws_hourly(
    lat,
    lon,
    target_date,
    now_et=None,
):
    """Return today's NWS hourly forecast using only future hours."""

    props = nws_points(lat, lon)

    url = props.get("forecastHourly")

    if not url:
        return []

    response = requests.get(
        url,
        headers=NWS_HEADERS,
        timeout=20,
    )

    response.raise_for_status()

    periods = response.json()["properties"]["periods"]

    values = []

    for period in periods:
        try:
            start = datetime.fromisoformat(
                period["startTime"].replace("Z", "+00:00")
            ).astimezone(ET)

            if start.date() != target_date:
                continue

            if now_et is not None and start < now_et:
                continue

            temp = safe_float(
                period.get("temperature")
            )

            if temp is None:
                continue

            values.append({
                "time": start,
                "temp": temp,
                "wind": period.get("windSpeed"),
                "wind_direction": period.get("windDirection"),
                "short_forecast": period.get("shortForecast"),
                "dewpoint": period.get("dewpoint"),
                "relative_humidity": period.get("relativeHumidity"),
            })

        except Exception:
            continue

    return values


def nws_forecast_high(hourly):
    if not hourly:
        return None

    return max(
        row["temp"]
        for row in hourly
    )


# ---------------------------------------------------------------------------
# Current KMIA observations
# ---------------------------------------------------------------------------

def fetch_recent_metars():
    params = {
        "ids": ",".join(MIA_REGION_STATIONS),
        "hours": 6,
        "format": "json",
        "taf": "false",
    }

    response = requests.get(
        AWC_METAR_URL,
        params=params,
        timeout=30,
    )

    response.raise_for_status()

    observations = response.json()

    by_station = defaultdict(list)

    for observation in observations:
        station = (
            observation.get("icaoId")
            or observation.get("station_id")
        )

        if station not in MIA_REGION_STATIONS:
            continue

        temp_c = safe_float(
            observation.get("temp")
        )

        if temp_c is None:
            continue

        temp_f = temp_c * 9 / 5 + 32

        dewpoint_c = safe_float(
            observation.get("dewp")
        )

        dewpoint_f = (
            dewpoint_c * 9 / 5 + 32
            if dewpoint_c is not None
            else None
        )

        by_station[station].append({
            "time": observation.get("obsTime"),
            "temp": temp_f,
            "dewpoint": dewpoint_f,
            "wind_dir": observation.get("wdir"),
            "wind_speed": observation.get("wspd"),
            "gust": observation.get("wgst"),
            "weather": observation.get("wxString"),
            "clouds": observation.get("clouds"),
        })

    for station in by_station:
        by_station[station].sort(
            key=lambda row: row.get("time") or "",
            reverse=True,
        )

    return by_station


def current_kmia_observation(metars):
    rows = metars.get("KMIA", [])

    if not rows:
        return None

    return rows[0]


def kmia_recent_trend(metars):
    """Estimate recent KMIA temperature change in °F/hour."""

    rows = metars.get("KMIA", [])

    if len(rows) < 2:
        return None

    parsed = []

    for row in rows:
        if not row.get("time"):
            continue

        try:
            timestamp = datetime.fromisoformat(
                row["time"].replace("Z", "+00:00")
            )

            parsed.append(
                (
                    timestamp,
                    row["temp"],
                )
            )

        except Exception:
            continue

    if len(parsed) < 2:
        return None

    parsed.sort()

    first_time, first_temp = parsed[0]
    last_time, last_temp = parsed[-1]

    hours = (
        last_time - first_time
    ).total_seconds() / 3600

    if hours <= 0:
        return None

    return (
        last_temp - first_temp
    ) / hours


def nearby_temperature_signal(metars):
    mia = current_kmia_observation(metars)

    if mia is None:
        return None

    nearby = []

    for station, rows in metars.items():
        if station == "KMIA" or not rows:
            continue

        nearby.append(
            rows[0]["temp"]
        )

    if not nearby:
        return None

    average = sum(nearby) / len(nearby)

    return {
        "avg": average,
        "delta_vs_kmia": average - mia["temp"],
        "stations": len(nearby),
    }


# ---------------------------------------------------------------------------
# Probability distributions
# ---------------------------------------------------------------------------

def normal_integer_distribution(
    mu,
    sigma,
    low=70,
    high=110,
):
    """Convert a normal distribution into integer-degree probability mass."""

    if mu is None:
        return {}

    sigma = max(
        float(sigma),
        0.75,
    )

    raw = {}

    for temp in range(
        low,
        high + 1,
    ):
        upper = (
            temp + 0.5 - mu
        ) / sigma

        lower = (
            temp - 0.5 - mu
        ) / sigma

        cdf_upper = 0.5 * (
            1 + math.erf(
                upper / math.sqrt(2)
            )
        )

        cdf_lower = 0.5 * (
            1 + math.erf(
                lower / math.sqrt(2)
            )
        )

        raw[temp] = max(
            0.0,
            cdf_upper - cdf_lower,
        )

    return normalize_distribution(raw)


def combine_distributions(distributions):
    result = defaultdict(float)

    for distribution, weight in distributions:
        if not distribution or weight <= 0:
            continue

        for temp, probability in distribution.items():
            result[temp] += (
                probability * weight
            )

    return normalize_distribution(result)


def apply_current_temp_constraint(
    distribution,
    current_temp,
):
    """The final high should not normally be below an already observed temp."""

    if not distribution or current_temp is None:
        return distribution

    floor_temp = math.floor(
        current_temp
    )

    result = {}

    for temp, probability in distribution.items():
        if temp >= floor_temp:
            result[temp] = probability
        else:
            result[temp] = probability * 0.01

    return normalize_distribution(result)


# ---------------------------------------------------------------------------
# Weather model
# ---------------------------------------------------------------------------

def build_weather_prediction(
    raw_txn,
    xnd,
    nws_high,
    current_obs,
    historical_model,
    sigma,
    nearby_signal=None,
):
    """Build the final weather distribution without using market prices."""

    bias = (
        get_txn_bias("MIA")
        if raw_txn is not None
        else 0.0
    )

    corrected_txn = (
        raw_txn - bias
        if raw_txn is not None
        else None
    )

    historical_distribution = (
        historical_model.get(
            "distribution",
            {},
        )
    )

    analog_count = historical_model.get(
        "analog_count",
        0,
    )

    if (
        analog_count >= MIN_ANALOGS
        and historical_distribution
    ):
        hist_weight = HIST_WEIGHT
        nws_weight = NWS_WEIGHT
        nbm_weight = NBM_WEIGHT

    elif historical_distribution:
        hist_weight = 0.35
        nws_weight = 0.40
        nbm_weight = 0.25

    else:
        hist_weight = 0.0
        nws_weight = 0.60
        nbm_weight = 0.40

    distributions = []

    if historical_distribution:
        distributions.append(
            (
                historical_distribution,
                hist_weight,
            )
        )

    if nws_high is not None:
        nws_distribution = (
            normal_integer_distribution(
                nws_high,
                max(
                    1.5,
                    sigma * 0.75,
                ),
            )
        )

        distributions.append(
            (
                nws_distribution,
                nws_weight,
            )
        )

    if corrected_txn is not None:
        nbm_distribution = (
            normal_integer_distribution(
                corrected_txn,
                sigma,
            )
        )

        distributions.append(
            (
                nbm_distribution,
                nbm_weight,
            )
        )

    final_distribution = (
        combine_distributions(
            distributions
        )
    )

    if current_obs is not None:
        final_distribution = (
            apply_current_temp_constraint(
                final_distribution,
                current_obs.get("temp"),
            )
        )

    return {
        "distribution": final_distribution,
        "corrected_txn": corrected_txn,
        "analog_count": analog_count,
        "hist_weight": hist_weight,
        "nws_weight": nws_weight,
        "nbm_weight": nbm_weight,
        "nearby_delta": (
            nearby_signal["delta_vs_kmia"]
            if nearby_signal is not None
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Polymarket comparison
# ---------------------------------------------------------------------------

def bucket_probability_from_distribution(
    distribution,
    lo=None,
    hi=None,
):
    """
    Calculate probability for a market bucket.

    Supports both normal bounded buckets:
        90-91
        92-93

    and open-ended buckets:
        85 or below
        94 or above

    lo=None means no lower bound.
    hi=None means no upper bound.
    """

    total = 0.0

    for temp, probability in distribution.items():

        if lo is not None and temp < lo:
            continue

        if hi is not None and temp > hi:
            continue

        total += probability

    return total


def market_analysis(
    distribution,
    target_date,
):
    """Compare independent weather model with market prices."""

    result = {
        "website": [],
        "app": [],
    }

    city = CITIES["MIA"]

    # ---------------------------------------------------------
    # Website market
    # ---------------------------------------------------------
    try:
        slug = build_event_slug(
            city["slug"],
            target_date,
        )

        event = fetch_market_by_slug(
            slug
        )

        outcomes = (
            parse_outcomes(event)
            if event
            else []
        )

        for label, lo, hi, price in outcomes:

            model_prob = (
                bucket_probability_from_distribution(
                    distribution,
                    lo,
                    hi,
                )
            )

            result["website"].append({
                "label": label,
                "price": price,
                "model_prob": model_prob,
                "edge": (
                    model_prob - price
                    if price is not None
                    else None
                ),
                "lo": lo,
                "hi": hi,
            })

    except Exception as exc:
        print(
            f"Website market analysis failed: {exc}"
        )

    # ---------------------------------------------------------
    # Polymarket app market
    # ---------------------------------------------------------
    try:
        station_slug = US_STATION_SLUG["MIA"]

        app_slug = build_polymarket_us_slug(
            station_slug,
            target_date,
        )

        app_event = fetch_polymarket_us_event(
            app_slug
        )

        app_outcomes = (
            parse_polymarket_us_outcomes(
                app_event
            )
            if app_event
            else []
        )

        for label, lo, hi, price in app_outcomes:

            model_prob = (
                bucket_probability_from_distribution(
                    distribution,
                    lo,
                    hi,
                )
            )

            result["app"].append({
                "label": label,
                "price": price,
                "model_prob": model_prob,
                "edge": (
                    model_prob - price
                    if price is not None
                    else None
                ),
                "lo": lo,
                "hi": hi,
            })

    except Exception as exc:
        print(
            f"App market analysis failed: {exc}"
        )

    return result


def best_market_edge(rows):
    valid = [
        row
        for row in rows
        if row["price"] is not None
        and 0.05 <= row["price"] <= 0.95
        and row["edge"] is not None
    ]

    if not valid:
        return None

    return max(
        valid,
        key=lambda row: row["edge"],
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def top_predictions(
    distribution,
    count=5,
):
    return sorted(
        distribution.items(),
        key=lambda item: item[1],
        reverse=True,
    )[:count]


def confidence_label(
    probability,
    analog_count,
):
    if (
        probability >= 0.60
        and analog_count >= MIN_ANALOGS
    ):
        return "STRONG"

    if probability >= 0.50:
        return "GOOD"

    if probability >= 0.40:
        return "LEAN"

    return "LOW"


def format_report(
    target_date,
    now_et,
    nbm,
    nws_high,
    current_obs,
    trend,
    nearby_signal,
    historical_model,
    model,
    market,
    sigma,
    sigma_source,
    tracking_summary=None,
):
    distribution = model["distribution"]

    top = top_predictions(
        distribution
    )

    if not top:
        return (
            "KMIA MORNING PREDICTION\n\n"
            "Unable to build a probability distribution."
        )

    # ---------------------------------------------------------
    # Exact-temperature prediction
    # ---------------------------------------------------------
    best_temp, best_temp_prob = top[0]

    analogs = historical_model.get(
        "analogs",
        [],
    )

    # ---------------------------------------------------------
    # APP BUCKET PREDICTION
    # ---------------------------------------------------------
    app_buckets = []

    for row in market.get("app", []):

        lo = row.get("lo")
        hi = row.get("hi")

        if lo is None and hi is None:
            continue

        bucket_prob = (
            bucket_probability_from_distribution(
                distribution,
                lo,
                hi,
            )
        )

        app_buckets.append({
            "label": row["label"],
            "lo": lo,
            "hi": hi,
            "model_prob": bucket_prob,
            "price": row.get("price"),
            "edge": row.get("edge"),
        })

    if app_buckets:

        best_app_bucket = max(
            app_buckets,
            key=lambda row: row["model_prob"],
        )

        best_app_probability = (
            best_app_bucket["model_prob"]
        )

        confidence = confidence_label(
            best_app_probability,
            len(analogs),
        )

    else:
        best_app_bucket = None
        best_app_probability = None
        confidence = None

    # ---------------------------------------------------------
    # Header
    # ---------------------------------------------------------
    lines = [
        f"KMIA MORNING WEATHER PREDICTION — {target_date}",
        (
            f"Run: "
            f"{now_et.strftime('%I:%M %p ET').lstrip('0')}"
        ),
        "",
    ]

    # ---------------------------------------------------------
    # Primary betting prediction
    # ---------------------------------------------------------
    if best_app_bucket is not None:

        lines.extend([
            (
                f"MOST PROBABLE APP BUCKET: "
                f"{best_app_bucket['label']} "
                f"({best_app_probability:.0%})"
            ),
            f"Confidence: {confidence}",
        ])

        if best_app_probability >= 0.50:
            lines.append(
                "Most probable app bucket exceeds 50%."
            )
        else:
            lines.append(
                "No app bucket exceeds 50% — "
                "do not treat this as a high-confidence call."
            )

        lines.extend([
            "",
            "APP BUCKET PROBABILITIES:",
        ])

        for bucket in sorted(
            app_buckets,
            key=lambda row: row["model_prob"],
            reverse=True,
        ):
            lines.append(
                f"  {bucket['label']} — "
                f"{bucket['model_prob']:.1%}"
            )

    else:

        lines.extend([
            "NO APP BUCKETS AVAILABLE.",
            (
                f"MOST PROBABLE HIGH: "
                f"{best_temp}°F "
                f"({best_temp_prob:.0%})"
            ),
            (
                "The app market could not be read, "
                "so no betting-bucket prediction was made."
            ),
        ])

    # ---------------------------------------------------------
    # Exact temperature distribution
    # ---------------------------------------------------------
    lines.extend([
        "",
        "TOP EXACT TEMPERATURES:",
    ])

    for temp, probability in top:
        lines.append(
            f"  {temp}°F — {probability:.1%}"
        )

    # ---------------------------------------------------------
    # Weather inputs
    # ---------------------------------------------------------
    lines.extend([
        "",
        "WEATHER INPUTS:",
    ])

    raw_txn = nbm.get("txn")
    xnd = nbm.get("xnd")

    if raw_txn is not None:
        lines.append(
            f"  NBM TXN: {raw_txn:.0f}°F"
        )

    if model["corrected_txn"] is not None:
        lines.append(
            f"  Bias-corrected NBM: "
            f"{model['corrected_txn']:.1f}°F"
        )

    if xnd is not None:
        lines.append(
            f"  NBM XND: {xnd}"
        )

    if nbm.get("cycle"):
        lines.append(
            f"  NBM cycle used: "
            f"{nbm['cycle']}Z"
        )

    if nws_high is not None:
        lines.append(
            f"  NWS remaining-hourly high: "
            f"{nws_high:.0f}°F"
        )

    if current_obs is not None:

        lines.append(
            f"  KMIA now: "
            f"{current_obs['temp']:.1f}°F"
        )

        if current_obs.get("dewpoint") is not None:
            lines.append(
                f"  KMIA dewpoint: "
                f"{current_obs['dewpoint']:.1f}°F"
            )

        if current_obs.get("wind_speed") is not None:
            lines.append(
                f"  KMIA wind: "
                f"{current_obs['wind_speed']} kt"
            )

    if trend is not None:
        lines.append(
            f"  KMIA recent temp trend: "
            f"{trend:+.2f}°F/hr"
        )

    if nearby_signal is not None:
        lines.append(
            f"  Nearby station average: "
            f"{nearby_signal['avg']:.1f}°F "
            f"({nearby_signal['delta_vs_kmia']:+.1f}° "
            f"vs KMIA)"
        )

    # ---------------------------------------------------------
    # Historical analogs
    # ---------------------------------------------------------
    lines.extend([
        "",
        f"HISTORICAL SAME-MORNING ANALOGS: "
        f"{len(analogs)}",
    ])

    if len(analogs) >= MIN_ANALOGS:
        lines.append(
            "  Historical component: FULL WEIGHT"
        )
    else:
        lines.append(
            "  Historical component: REDUCED WEIGHT"
        )

    if analogs:

        analog_outcomes = defaultdict(float)

        for analog in analogs:

            high = nearest_int(
                analog["actual_high"]
            )

            analog_outcomes[high] += (
                analog["weight"]
            )

        analog_outcomes = normalize_distribution(
            analog_outcomes
        )

        analog_top = sorted(
            analog_outcomes.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:3]

        lines.append(
            "  Closest historical outcomes:"
        )

        for temp, probability in analog_top:
            lines.append(
                f"    {temp}°F — {probability:.0%}"
            )

    # ---------------------------------------------------------
    # Model mix
    # ---------------------------------------------------------
    lines.extend([
        "",
        "MODEL MIX:",
        (
            f"  Historical same-morning: "
            f"{model['hist_weight']:.0%}"
        ),
        (
            f"  NWS hourly: "
            f"{model['nws_weight']:.0%}"
        ),
        (
            f"  NBM: "
            f"{model['nbm_weight']:.0%}"
        ),
        (
            f"  NBM sigma: "
            f"{sigma:.2f}°F ({sigma_source})"
        ),
    ])

    # ---------------------------------------------------------
    # Tracking
    # ---------------------------------------------------------
    if tracking_summary:
        lines.extend([
            "",
            tracking_summary,
        ])

    # ---------------------------------------------------------
    # Polymarket comparison
    # ---------------------------------------------------------
    lines.extend([
        "",
        "POLYMARKET — WEATHER MODEL VS MARKET:",
    ])

    website_best = best_market_edge(
        market["website"]
    )

    if website_best:
        lines.append(
            f"  Website best edge: "
            f"{html.escape(website_best['label'])} — "
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
            f"{html.escape(app_best['label'])} — "
            f"model {app_best['model_prob']:.1%} "
            f"vs market {app_best['price']:.1%} "
            f"({app_best['edge']:+.1%})"
        )
    else:
        lines.append(
            "  App: no trustworthy priced bucket found."
        )

    lines.extend([
        "",
        "IMPORTANT: market prices do NOT "
        "influence the weather probability.",
    ])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(message):
    token = os.environ.get(
        "TELEGRAM_BOT_TOKEN"
    )

    chat_id = os.environ.get(
        "TELEGRAM_CHAT_ID"
    )

    if not token or not chat_id:
        print(message)
        return

    url = (
        f"https://api.telegram.org/"
        f"bot{token}/sendMessage"
    )

    for i in range(
        0,
        len(message),
        3500,
    ):
        response = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": message[i:i + 3500],
            },
            timeout=20,
        )

        response.raise_for_status()


# ---------------------------------------------------------------------------
# NBM
# ---------------------------------------------------------------------------

def get_latest_nbm_for_today(
    target_date,
):
    """Try the newest available NBM cycle first."""

    for cycle in (
        "13",
        "07",
        "01",
    ):
        try:
            nbm_data = fetch_all_nbm(
                cycle
            )

            mia_block = (
                nbm_data
                .get("KMIA", {})
                .get("block")
            )

            if not mia_block:
                continue

            txn, xnd = extract_max_for_date(
                mia_block,
                target_date,
            )

            if txn is not None:

                issue = (
                    parse_bulletin_issue_time(
                        mia_block
                    )
                )

                return {
                    "txn": float(txn),
                    "xnd": xnd,
                    "cycle": cycle,
                    "issue": issue,
                }

        except Exception as exc:
            print(
                f"NBM {cycle}Z unavailable: "
                f"{exc}"
            )

    return {
        "txn": None,
        "xnd": None,
        "cycle": None,
        "issue": None,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():

    now_et = datetime.now(ET)

    target_date = now_et.date()

    print(
        f"Building KMIA morning prediction "
        f"for {target_date}..."
    )

    # ---------------------------------------------------------
    # Resolve previous predictions first.
    #
    # This does not resolve today's prediction.
    # ---------------------------------------------------------
    try:
        tracking_history = resolve_pending_predictions(
            actual_high_fetcher=fetch_actual_high,
            today=target_date,
        )

        print(
            "Prediction tracking resolved: "
            f"{sum(1 for row in tracking_history if row.get('result') in ('WIN', 'LOSS'))}"
        )

    except Exception as exc:
        print(
            f"Prediction tracking resolution failed: "
            f"{exc}"
        )

        tracking_history = load_history()

    # ---------------------------------------------------------
    # NBM
    # ---------------------------------------------------------
    nbm = get_latest_nbm_for_today(
        target_date
    )

    raw_txn = nbm["txn"]
    xnd = nbm["xnd"]

    print(
        f"NBM: TXN={raw_txn}, "
        f"XND={xnd}, "
        f"cycle={nbm['cycle']}"
    )

    # ---------------------------------------------------------
    # NWS
    #
    # Only remaining hours are used.
    # ---------------------------------------------------------
    city = CITIES["MIA"]

    try:

        nws_hourly = fetch_nws_hourly(
            city["lat"],
            city["lon"],
            target_date,
            now_et=now_et,
        )

        nws_high = nws_forecast_high(
            nws_hourly
        )

    except Exception as exc:

        print(
            f"NWS hourly forecast failed: "
            f"{exc}"
        )

        nws_high = None

    # ---------------------------------------------------------
    # Current observations
    # ---------------------------------------------------------
    try:

        metars = fetch_recent_metars()

        current_obs = (
            current_kmia_observation(
                metars
            )
        )

        trend = kmia_recent_trend(
            metars
        )

        nearby_signal = (
            nearby_temperature_signal(
                metars
            )
        )

    except Exception as exc:

        print(
            f"METAR observation pull failed: "
            f"{exc}"
        )

        current_obs = None
        trend = None
        nearby_signal = None

    if current_obs is None:

        print(
            "ERROR: KMIA current observation "
            "unavailable."
        )

        sys.exit(1)

    # ---------------------------------------------------------
    # Same-morning historical model
    # ---------------------------------------------------------
    historical_model = (
        get_historical_morning_model(
            target_date=target_date,
            current_time=now_et,
            current_temp=current_obs["temp"],
            current_dewpoint=current_obs.get(
                "dewpoint"
            ),
            current_trend=trend,
        )
    )

    print(
        "Historical same-morning KMIA "
        "analogs: "
        f"{historical_model.get('analog_count', 0)}"
    )

    # ---------------------------------------------------------
    # Learned NBM uncertainty
    # ---------------------------------------------------------
    sigma, sigma_source = get_sigma(
        "MIA",
        xnd,
    )

    print(
        f"NBM sigma: {sigma:.2f}°F "
        f"({sigma_source})"
    )

    # ---------------------------------------------------------
    # Independent weather model
    # ---------------------------------------------------------
    model = build_weather_prediction(
        raw_txn=raw_txn,
        xnd=xnd,
        nws_high=nws_high,
        current_obs=current_obs,
        historical_model=historical_model,
        sigma=sigma,
        nearby_signal=nearby_signal,
    )

    distribution = model[
        "distribution"
    ]

    if not distribution:

        print(
            "ERROR: could not build weather "
            "probability distribution."
        )

        sys.exit(1)

    # ---------------------------------------------------------
    # Market comparison
    #
    # This happens ONLY after the weather model is complete.
    # ---------------------------------------------------------
    market = market_analysis(
        distribution,
        target_date,
    )

    # ---------------------------------------------------------
    # Determine the primary app bucket.
    #
    # Only live buckets returned by Polymarket are used.
    # ---------------------------------------------------------
    app_buckets = []

    for row in market.get("app", []):

        lo = row.get("lo")
        hi = row.get("hi")

        if lo is None and hi is None:
            continue

        bucket_prob = (
            bucket_probability_from_distribution(
                distribution,
                lo,
                hi,
            )
        )

        app_buckets.append({
            "label": row["label"],
            "lo": lo,
            "hi": hi,
            "model_prob": bucket_prob,
            "price": row.get("price"),
            "edge": row.get("edge"),
        })

    if app_buckets:

        best_app_bucket = max(
            app_buckets,
            key=lambda row: row["model_prob"],
        )

        best_app_probability = (
            best_app_bucket["model_prob"]
        )

        confidence = confidence_label(
            best_app_probability,
            historical_model.get(
                "analog_count",
                0,
            ),
        )

    else:

        best_app_bucket = None
        best_app_probability = None
        confidence = None

    # ---------------------------------------------------------
    # Record today's prediction.
    #
    # The tracker itself prevents evening/manual runs from
    # being recorded and prevents duplicate predictions.
    # ---------------------------------------------------------
    if best_app_bucket is not None:

        try:

            tracking_result = record_prediction(
                target_date=target_date,
                now_et=now_et,
                predicted_bucket=(
                    best_app_bucket["label"]
                ),
                lo=best_app_bucket["lo"],
                hi=best_app_bucket["hi"],
                model_probability=(
                    best_app_probability
                ),
                confidence=confidence,
                market_price=(
                    best_app_bucket.get("price")
                ),
                analog_count=(
                    historical_model.get(
                        "analog_count",
                        0,
                    )
                ),
            )

            print(
                "Prediction tracking: "
                f"{tracking_result['reason']}"
            )

        except Exception as exc:

            print(
                f"Prediction tracking record failed: "
                f"{exc}"
            )

    # Reload after today's prediction has potentially been recorded.
    try:
        tracking_history = load_history()
        tracking_summary = format_tracking_summary(
            tracking_history
        )
    except Exception as exc:
        print(
            f"Prediction tracking summary failed: "
            f"{exc}"
        )
        tracking_summary = None

    # ---------------------------------------------------------
    # Report
    # ---------------------------------------------------------
    report = format_report(
        target_date=target_date,
        now_et=now_et,
        nbm=nbm,
        nws_high=nws_high,
        current_obs=current_obs,
        trend=trend,
        nearby_signal=nearby_signal,
        historical_model=historical_model,
        model=model,
        market=market,
        sigma=sigma,
        sigma_source=sigma_source,
        tracking_summary=tracking_summary,
    )

    send_telegram(report)

    print(report)


if __name__ == "__main__":
    main()