#!/usr/bin/env python3
"""
prediction_tracker.py

Tracks KMIA morning Polymarket app-bucket predictions and resolves them
against the settled Polymarket US temperature bucket.

Tracking is completely passive:
- It does NOT influence the weather model.
- It does NOT influence bucket probabilities.
- It does NOT influence confidence.
- It records only the original prediction for each date.

Resolution uses the Polymarket US market itself so the tracker measures
whether the predicted app bucket actually won.

Each prediction also stores a snapshot of the major weather inputs used
to create that prediction. This allows future calibration and
performance analysis without changing the model itself.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

HISTORY_FILE = Path(
    os.environ.get(
        "PREDICTION_HISTORY_FILE",
        "prediction_history.json",
    )
)

# Predictions can be recorded from 5:00 AM through 2:59 PM ET.
TRACKING_START_HOUR = 5
TRACKING_END_HOUR = 15

# Polymarket US station slug from config.py.
KMIA_US_STATION_SLUG = "mia"


def load_history(path=HISTORY_FILE):
    path = Path(path)

    if not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, list):
            return []

        return data

    except (OSError, json.JSONDecodeError):
        return []


def save_history(history, path=HISTORY_FILE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = path.with_suffix(path.suffix + ".tmp")

    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(history, file, indent=2)

    temp_path.replace(path)


def is_tracking_time(now_et):
    if now_et is None:
        return False

    if now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=ET)

    hour = now_et.astimezone(ET).hour

    return (
        TRACKING_START_HOUR
        <= hour
        < TRACKING_END_HOUR
    )


def prediction_exists_for_date(history, target_date):
    date_string = target_date.isoformat()

    return any(
        row.get("date") == date_string
        for row in history
    )


def record_prediction(
    target_date,
    now_et,
    predicted_bucket,
    lo,
    hi,
    model_probability,
    confidence,
    market_price=None,
    analog_count=0,
    weather_inputs=None,
    path=HISTORY_FILE,
):
    if not is_tracking_time(now_et):
        return {
            "recorded": False,
            "reason": "outside_morning_tracking_window",
        }

    history = load_history(path)

    if prediction_exists_for_date(history, target_date):
        return {
            "recorded": False,
            "reason": "prediction_already_exists",
        }

    record = {
        "date": target_date.isoformat(),
        "predicted_bucket": predicted_bucket,
        "lo": lo,
        "hi": hi,
        "model_probability": round(
            float(model_probability),
            6,
        ),
        "confidence": confidence,
        "market_price": (
            round(float(market_price), 6)
            if market_price is not None
            else None
        ),
        "analog_count": int(
            analog_count or 0
        ),
        "weather_inputs": (
            weather_inputs
            if isinstance(weather_inputs, dict)
            else {}
        ),
        "actual_high": None,
        "settled_bucket": None,
        "result": None,
        "recorded_at": now_et.isoformat(),
    }

    history.append(record)
    history.sort(
        key=lambda row: row.get("date", "")
    )

    save_history(history, path)

    return {
        "recorded": True,
        "reason": "new_prediction",
        "record": record,
    }


def bucket_won(actual_high, lo, hi):
    """
    Retained for compatibility.

    This is not used for Polymarket US settlement resolution because
    settlement is now determined by the actual winning app bucket.
    """
    if actual_high is None:
        return False

    if lo is not None and actual_high < lo:
        return False

    if hi is not None and actual_high > hi:
        return False

    return True


def _predicted_bucket_matches_settled(
    record,
    settled_lo,
    settled_hi,
):
    """
    Determine whether the prediction's bucket is the exact bucket that
    settled.

    Open-ended buckets are represented internally with sentinel bounds
    by parse_polymarket_us_outcomes(), so comparing both bounds works
    for:
        <=83
        84-85
        ...
        >=92
    """

    predicted_lo = record.get("lo")
    predicted_hi = record.get("hi")

    if predicted_lo is None or predicted_hi is None:
        return False

    try:
        return (
            float(predicted_lo) == float(settled_lo)
            and float(predicted_hi) == float(settled_hi)
        )

    except (TypeError, ValueError):
        return False


def resolve_pending_predictions(
    actual_high_fetcher,
    today,
    path=HISTORY_FILE,
):
    """
    Resolve every prior pending prediction whose Polymarket US event
    has settled.

    actual_high_fetcher is retained in the function signature so the
    existing morning_predict.py does not need to change.

    For KMIA, it is intentionally NOT used for resolution.

    The tracker instead:
        1. Builds the KMIA Polymarket US event slug.
        2. Fetches the event.
        3. Reads the app buckets.
        4. Finds the bucket whose YES price settled at 1.
        5. Compares that bucket with the original prediction.
    """

    history = load_history(path)
    changed = False

    try:
        from data_sources import (
            build_polymarket_us_slug,
            fetch_polymarket_us_event,
            parse_polymarket_us_outcomes,
        )

    except Exception as e:
        print(
            f"Polymarket US tracker import failed: {e}"
        )
        return history

    for record in history:
        if record.get("result") is not None:
            continue

        date_string = record.get("date")

        if not date_string:
            continue

        try:
            target_date = datetime.strptime(
                date_string,
                "%Y-%m-%d",
            ).date()

        except ValueError:
            continue

        # Never resolve today's prediction.
        if target_date >= today:
            continue

        try:
            slug = build_polymarket_us_slug(
                KMIA_US_STATION_SLUG,
                target_date,
            )

            print(
                f"Checking Polymarket US settlement "
                f"for KMIA {target_date}: {slug}"
            )

            event = fetch_polymarket_us_event(
                slug
            )

            if not event:
                print(
                    f"Polymarket US event not found "
                    f"for KMIA {target_date}."
                )
                continue

            outcomes = parse_polymarket_us_outcomes(
                event
            )

            if not outcomes:
                print(
                    f"No Polymarket US outcomes found "
                    f"for KMIA {target_date}."
                )
                continue

            settled_bucket = None

            for label, lo, hi, price in outcomes:
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    continue

                # A fully settled YES market should be 1.0.
                if price >= 0.999:
                    settled_bucket = {
                        "label": label,
                        "lo": lo,
                        "hi": hi,
                        "price": price,
                    }
                    break

            if settled_bucket is None:
                print(
                    f"Polymarket US market for KMIA "
                    f"{target_date} is not settled yet."
                )
                continue

            won = _predicted_bucket_matches_settled(
                record,
                settled_bucket["lo"],
                settled_bucket["hi"],
            )

            record["settled_bucket"] = (
                settled_bucket["label"]
            )

            # Do not invent an exact temperature from an open-ended
            # Polymarket bucket. Leave actual_high as None unless an
            # exact finite bucket represents a single temperature.
            record["actual_high"] = None

            record["result"] = (
                "WIN"
                if won
                else "LOSS"
            )

            record["resolved_at"] = (
                datetime.now(ET).isoformat()
            )

            changed = True

            print(
                f"Resolved KMIA {target_date}: "
                f"predicted={record.get('predicted_bucket')} "
                f"settled={settled_bucket['label']} "
                f"result={record['result']}"
            )

        except Exception as e:
            print(
                f"Polymarket US resolution failed "
                f"for KMIA {target_date}: {e}"
            )

    if changed:
        save_history(history, path)

    return history


def calculate_stats(history):
    resolved = [
        row
        for row in history
        if row.get("result") in ("WIN", "LOSS")
    ]

    wins = sum(
        1
        for row in resolved
        if row.get("result") == "WIN"
    )

    losses = sum(
        1
        for row in resolved
        if row.get("result") == "LOSS"
    )

    total = wins + losses

    win_rate = (
        wins / total
        if total > 0
        else None
    )

    pending = sum(
        1
        for row in history
        if row.get("result") is None
    )

    confidence_stats = {}

    for record in resolved:
        confidence = record.get(
            "confidence",
            "UNKNOWN",
        )

        if confidence not in confidence_stats:
            confidence_stats[confidence] = {
                "wins": 0,
                "losses": 0,
                "total": 0,
                "win_rate": None,
            }

        confidence_stats[confidence]["total"] += 1

        if record.get("result") == "WIN":
            confidence_stats[confidence]["wins"] += 1
        else:
            confidence_stats[confidence]["losses"] += 1

    for stats in confidence_stats.values():
        if stats["total"] > 0:
            stats["win_rate"] = (
                stats["wins"]
                / stats["total"]
            )

    return {
        "total_predictions": len(history),
        "resolved": total,
        "wins": wins,
        "losses": losses,
        "pending": pending,
        "win_rate": win_rate,
        "confidence": confidence_stats,
    }


def format_tracking_summary(history):
    stats = calculate_stats(history)
    resolved = stats["resolved"]

    if resolved == 0:
        return (
            "PREDICTION TRACKING:\n"
            "  No completed predictions yet."
        )

    win_rate = stats["win_rate"]

    lines = [
        "PREDICTION TRACKING:",
        (
            f"  Record: "
            f"{stats['wins']}-{stats['losses']} "
            f"({win_rate:.1%})"
        ),
        f"  Resolved: {resolved}",
        f"  Pending: {stats['pending']}",
    ]

    confidence_stats = stats.get(
        "confidence",
        {},
    )

    if confidence_stats:
        lines.append("  By confidence:")

        confidence_order = [
            "STRONG",
            "GOOD",
            "LEAN",
            "LOW",
        ]

        for confidence in confidence_order:
            row = confidence_stats.get(
                confidence
            )

            if not row:
                continue

            lines.append(
                f"    {confidence}: "
                f"{row['wins']}-{row['losses']} "
                f"({row['win_rate']:.1%})"
            )

    return "\n".join(lines)