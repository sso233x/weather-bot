"""
log.py — persists every scored prediction to trade_log.csv so outcomes
can be checked later and weights calibrated against real results.
"""

import csv
import os
from datetime import datetime, timezone

LOG_FILE = os.path.join(os.path.dirname(__file__), "trade_log.csv")

FIELDNAMES = [
    "logged_at", "city", "station", "target_date", "txn", "xnd",
    "market_bucket_label", "market_price", "confidence", "raw_score",
    "recommendation", "outcome_win", "actual_high", "notes",
]


def log_prediction(city_code, station, target_date, txn, xnd,
                    bucket_label, market_price, result) -> None:
    """
    Upserts by (city, target_date): if an UNRESOLVED row already exists
    for this city/target_date (e.g. the bot ran again the same day, or
    was manually re-triggered), that row is updated in place with the
    latest snapshot instead of appending a new row.

    This matters for calibration: without it, every re-run adds another
    "sample" for what is really the same underlying market, which
    silently inflates n and corrupts win-rate/confidence stats.

    Already-RESOLVED rows (outcome_win set) are never touched here --
    only check_outcomes.py should ever fill outcome_win/actual_high.
    """
    target_date_str = str(target_date)
    new_row = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "city": city_code,
        "station": station,
        "target_date": target_date_str,
        "txn": txn if txn is not None else "",
        "xnd": xnd if xnd is not None else "",
        "market_bucket_label": bucket_label or "",
        "market_price": market_price if market_price is not None else "",
        "confidence": result.confidence,
        "raw_score": result.raw_score,
        "recommendation": result.recommendation,
        "outcome_win": "",  # filled in later by check_outcomes.py
        "actual_high": "",  # filled in later by check_outcomes.py
        "notes": " | ".join(result.notes),
    }

    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerow(new_row)
        return

    with open(LOG_FILE, newline="") as f:
        rows = list(csv.DictReader(f))

    replaced = False
    for i, row in enumerate(rows):
        same_market = row.get("city") == city_code and row.get("target_date") == target_date_str
        unresolved = row.get("outcome_win") in ("", None)
        if same_market and unresolved:
            rows[i] = new_row
            replaced = True
            break  # only one unresolved row per city/target_date should exist

    if not replaced:
        rows.append(new_row)

    with open(LOG_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
