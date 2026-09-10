#!/usr/bin/env python3

"""
kmia_history.py

Simple historical same-morning KMIA analog engine.

Uses IEM historical ASOS observations to answer:

"When KMIA looked like this at roughly the same time of day,
what did the temperature eventually reach?"

This is intentionally simple:
- Current temperature
- Morning warming rate
- Dew point
- Time of day
- Season

No machine learning.
No market data.
No arbitrary dozens of variables.
"""

import csv
import io
import math
from collections import defaultdict
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import requests


ET = ZoneInfo("America/New_York")

IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

STATION = "MIA"
NETWORK = "FL_ASOS"

# Three years is enough history for a strong sample without
# making every GitHub Actions run unnecessarily huge.
YEARS_OF_HISTORY = 3

# Don't allow one very unusual historical day to dominate.
MAX_ANALOGS = 80


def safe_float(value):
    try:
        if value is None:
            return None

        value = str(value).strip()

        if not value or value in {"M", "T", "null", "None"}:
            return None

        return float(value)

    except (TypeError, ValueError):
        return None


def fahrenheit(value):
    return value


def fetch_iem_history(target_date):
    """
    Download historical hourly KMIA observations.

    IEM's ASOS archive supports station/date-range downloads.
    We request local Eastern timestamps.
    """

    start_date = target_date - timedelta(days=365 * YEARS_OF_HISTORY)
    end_date = target_date

    params = {
        "station": STATION,
        "network": NETWORK,
        "data": "all",

        "year1": start_date.year,
        "month1": start_date.month,
        "day1": start_date.day,

        "year2": end_date.year,
        "month2": end_date.month,
        "day2": end_date.day,

        "tz": "America/New_York",
        "format": "onlycomma",
        "latlon": "no",
        "elev": "no",
        "missing": "M",
        "trace": "T",
        "direct": "no",

        # Routine hourly + special observations.
        "report_type": ["3", "4"],
    }

    response = requests.get(
        IEM_URL,
        params=params,
        timeout=60,
    )

    response.raise_for_status()

    text = response.text

    if not text.strip():
        return []

    rows = []

    reader = csv.DictReader(io.StringIO(text))

    for row in reader:
        try:
            valid_raw = row.get("valid")

            if not valid_raw:
                continue

            # IEM returns local time when tz is specified.
            valid = datetime.fromisoformat(
                valid_raw.replace(" ", "T")
            )

            tmpf = safe_float(row.get("tmpf"))
            dwpf = safe_float(row.get("dwpf"))

            if tmpf is None:
                continue

            rows.append({
                "time": valid,
                "date": valid.date(),
                "hour": valid.hour,
                "minute": valid.minute,
                "temp": tmpf,
                "dewpoint": dwpf,
            })

        except Exception:
            continue

    return rows


def group_by_date(rows):
    grouped = defaultdict(list)

    for row in rows:
        grouped[row["date"]].append(row)

    for d in grouped:
        grouped[d].sort(key=lambda x: x["time"])

    return grouped


def observation_at_time(rows, target_hour, tolerance_minutes=75):
    """
    Find the observation closest to the requested time.
    """

    if not rows:
        return None

    target_minutes = target_hour * 60

    best = None
    best_distance = None

    for row in rows:
        minutes = row["hour"] * 60 + row["minute"]

        distance = abs(minutes - target_minutes)

        if distance > tolerance_minutes:
            continue

        if best is None or distance < best_distance:
            best = row
            best_distance = distance

    return best


def temperature_at_hour(rows, hour):
    return observation_at_time(
        rows,
        hour,
        tolerance_minutes=75,
    )


def morning_warming_rate(rows, current_hour):
    """
    Estimate the temperature change over the previous
    2-4 hours.

    We prefer a 3-hour window but accept a shorter one.
    """

    current = temperature_at_hour(
        rows,
        current_hour,
    )

    if current is None:
        return None

    candidates = []

    for row in rows:
        delta_hours = (
            current["time"] - row["time"]
        ).total_seconds() / 3600

        if 2.0 <= delta_hours <= 4.0:
            candidates.append(row)

    if not candidates:
        return None

    previous = min(
        candidates,
        key=lambda row: abs(
            (
                current["time"] - row["time"]
            ).total_seconds() / 3600 - 3.0
        ),
    )

    hours = (
        current["time"] - previous["time"]
    ).total_seconds() / 3600

    if hours <= 0:
        return None

    return (
        current["temp"] - previous["temp"]
    ) / hours


def historical_daily_high(rows):
    """
    Get the highest observed KMIA temperature for a historical day.
    """

    temps = [
        row["temp"]
        for row in rows
        if row.get("temp") is not None
    ]

    if not temps:
        return None

    return max(temps)


def circular_day_distance(doy1, doy2):
    difference = abs(doy1 - doy2)

    return min(
        difference,
        366 - difference,
    )


def build_same_morning_analogs(
    rows,
    target_date,
    current_hour,
    current_temp,
    current_dewpoint=None,
    current_trend=None,
):
    """
    Find historical days that looked like today
    at approximately the same point in the morning.

    Similarity is intentionally based on only a few
    physically meaningful variables.
    """

    grouped = group_by_date(rows)

    target_doy = target_date.timetuple().tm_yday

    candidates = []

    for historical_date, day_rows in grouped.items():

        if historical_date >= target_date:
            continue

        # Need enough of the day to calculate a meaningful
        # historical high.
        historical_high = historical_daily_high(day_rows)

        if historical_high is None:
            continue

        morning = temperature_at_hour(
            day_rows,
            current_hour,
        )

        if morning is None:
            continue

        # Temperature similarity is the strongest factor.
        temp_difference = abs(
            morning["temp"] - current_temp
        )

        # Skip completely dissimilar mornings.
        if temp_difference > 7.0:
            continue

        # Dew point is useful but deliberately weaker.
        dewpoint_difference = 0.0

        if (
            current_dewpoint is not None
            and morning.get("dewpoint") is not None
        ):
            dewpoint_difference = abs(
                morning["dewpoint"] - current_dewpoint
            )

        historical_trend = morning_warming_rate(
            day_rows,
            current_hour,
        )

        trend_difference = 0.0

        if (
            current_trend is not None
            and historical_trend is not None
        ):
            trend_difference = abs(
                historical_trend - current_trend
            )

        historical_doy = (
            historical_date.timetuple().tm_yday
        )

        seasonal_difference = circular_day_distance(
            target_doy,
            historical_doy,
        )

        # Simple distance score.
        #
        # Temperature is dominant.
        # Dewpoint and trend are supporting signals.
        # Seasonality prevents winter/summer mismatches.
        distance = (
            temp_difference
            + 0.35 * dewpoint_difference
            + 0.75 * trend_difference
            + 0.025 * seasonal_difference
        )

        candidates.append({
            "date": historical_date,
            "morning_temp": morning["temp"],
            "morning_dewpoint": morning.get("dewpoint"),
            "morning_trend": historical_trend,
            "actual_high": historical_high,
            "distance": distance,
        })

    candidates.sort(
        key=lambda x: x["distance"]
    )

    candidates = candidates[:MAX_ANALOGS]

    # Exponential weighting.
    #
    # Close matches matter substantially more than
    # mediocre matches.
    analogs = []

    for candidate in candidates:

        weight = math.exp(
            -candidate["distance"] / 3.0
        )

        candidate["weight"] = weight

        analogs.append(candidate)

    return analogs


def build_distribution(analogs):
    """
    Turn historical final highs into an empirical
    probability distribution.

    Includes a small smoothing factor so that a tiny
    analog sample cannot create an absurd 90%+ prediction.
    """

    if not analogs:
        return {}

    distribution = defaultdict(float)

    for analog in analogs:

        high = int(
            math.floor(
                analog["actual_high"] + 0.5
            )
        )

        distribution[high] += analog["weight"]

    total = sum(
        distribution.values()
    )

    if total <= 0:
        return {}

    distribution = {
        temp: weight / total
        for temp, weight in distribution.items()
    }

    # Simple reliability shrinkage.
    #
    # With a large number of close historical matches,
    # trust the empirical distribution more.
    #
    # With few matches, shrink toward a broad normal
    # distribution around the weighted historical mean.
    total_weight = sum(
        analog["weight"]
        for analog in analogs
    )

    if total_weight >= 15:
        historical_weight = 0.90

    elif total_weight >= 8:
        historical_weight = 0.75

    else:
        historical_weight = 0.55

    mean = sum(
        temp * probability
        for temp, probability
        in distribution.items()
    )

    # Broad 2.5°F uncertainty distribution.
    normal = {}

    for temp in range(
        int(mean) - 8,
        int(mean) + 9,
    ):
        z = (
            temp - mean
        ) / 2.5

        normal[temp] = math.exp(
            -0.5 * z * z
        )

    normal_total = sum(
        normal.values()
    )

    normal = {
        temp: value / normal_total
        for temp, value in normal.items()
    }

    final = defaultdict(float)

    for temp, probability in distribution.items():
        final[temp] += (
            probability * historical_weight
        )

    for temp, probability in normal.items():
        final[temp] += (
            probability
            * (1.0 - historical_weight)
        )

    total = sum(final.values())

    return {
        temp: probability / total
        for temp, probability in final.items()
    }


def get_historical_morning_model(
    target_date,
    current_time,
    current_temp,
    current_dewpoint=None,
    current_trend=None,
):
    """
    Main public function.

    Returns:
        {
            "distribution": {...},
            "analogs": [...],
            "analog_count": int
        }
    """

    rows = fetch_iem_history(
        target_date
    )

    if not rows:
        return {
            "distribution": {},
            "analogs": [],
            "analog_count": 0,
        }

    analogs = build_same_morning_analogs(
        rows=rows,
        target_date=target_date,
        current_hour=current_time.hour,
        current_temp=current_temp,
        current_dewpoint=current_dewpoint,
        current_trend=current_trend,
    )

    distribution = build_distribution(
        analogs
    )

    return {
        "distribution": distribution,
        "analogs": analogs,
        "analog_count": len(analogs),
    }