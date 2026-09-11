#!/usr/bin/env python3
"""
prediction_tracker.py

Tracks KMIA morning Polymarket app-bucket predictions and resolves them
against the actual KMIA daily high.

Tracking is completely passive:
- It does NOT influence the weather model.
- It does NOT influence bucket probabilities.
- It does NOT influence confidence.
- It records only the original morning prediction for each date.

Actual highs come from the existing data_sources.fetch_actual_high()
function, which uses the IEM ASOS daily summary as a practical proxy
for the official daily high.
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

# The bot is designed to make its prediction in the morning.
# This prevents accidental evening/manual test runs from being counted.
TRACKING_START_HOUR = 5
TRACKING_END_HOUR = 12


def load_history(path=HISTORY_FILE):
    """Load prediction history from JSON."""

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
    """Save prediction history to JSON."""

    path = Path(path)

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp_path = path.with_suffix(
        path.suffix + ".tmp"
    )

    with temp_path.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            history,
            file,
            indent=2,
        )

    temp_path.replace(path)


def is_tracking_time(now_et):
    """
    Return True only during the normal morning prediction window.

    This prevents evening test runs from entering the win-rate history.
    """

    if now_et is None:
        return False

    if now_et.tzinfo is None:
        now_et = now_et.replace(
            tzinfo=ET
        )

    hour = now_et.astimezone(ET).hour

    return (
        TRACKING_START_HOUR
        <= hour
        < TRACKING_END_HOUR
    )


def prediction_exists_for_date(
    history,
    target_date,
):
    """Check whether a prediction has already been recorded for a date."""

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
    path=HISTORY_FILE,
):
    """
    Record the original morning prediction.

    Only the first prediction for a date is recorded.
    Evening/manual runs are ignored.
    """

    if not is_tracking_time(now_et):
        return {
            "recorded": False,
            "reason": "outside_morning_tracking_window",
        }

    history = load_history(path)

    if prediction_exists_for_date(
        history,
        target_date,
    ):
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
        "actual_high": None,
        "result": None,
        "recorded_at": now_et.isoformat(),
    }

    history.append(record)

    history.sort(
        key=lambda row: row.get("date", "")
    )

    save_history(
        history,
        path,
    )

    return {
        "recorded": True,
        "reason": "new_prediction",
        "record": record,
    }


def bucket_won(
    actual_high,
    lo,
    hi,
):
    """Determine whether the actual high landed inside the predicted bucket."""

    if actual_high is None:
        return False

    if lo is not None and actual_high < lo:
        return False

    if hi is not None and actual_high > hi:
        return False

    return True


def resolve_pending_predictions(
    actual_high_fetcher,
    today,
    path=HISTORY_FILE,
):
    """
    Resolve completed predictions whose dates are before today.

    Today's prediction is intentionally never resolved because the daily
    high may still change.
    """

    history = load_history(path)

    changed = False

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

        if target_date >= today:
            continue

        try:
            actual_high = actual_high_fetcher(
                "KMIA",
                target_date,
            )
        except Exception:
            actual_high = None

        if actual_high is None:
            continue

        lo = record.get("lo")
        hi = record.get("hi")

        won = bucket_won(
            actual_high,
            lo,
            hi,
        )

        record["actual_high"] = float(
            actual_high
        )

        record["result"] = (
            "WIN"
            if won
            else "LOSS"
        )

        record["resolved_at"] = (
            datetime.now(ET).isoformat()
        )

        changed = True

    if changed:
        save_history(
            history,
            path,
        )

    return history


def calculate_stats(history):
    """Calculate overall tracking statistics."""

    resolved = [
        row
        for row in history
        if row.get("result")
        in ("WIN", "LOSS")
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
    """Return a short human-readable tracking summary."""

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
        lines.append(
            "  By confidence:"
        )

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